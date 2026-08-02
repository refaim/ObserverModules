"""Independent UMDH leak scenarios composed over x64 Release binaries."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from core.graph import Graph, Node, Result
from core.paths import BuildPaths
from graphs.common import (
    BUILD_ROOT, canonical_artifact, extend_pools, python_action, recipe_factory, required_audit_gates,
    require_positive_integers, require_tool,
)


LEAK_MODES = ("operations", "lifecycle")
LEAK_SCENARIOS = ("small-success", "malformed", "cancellation", "read-failure", "write-failure", "large-metadata", "sparse-metadata")
_BINARIES = {"leak-probe": "leak-probe.exe", "renpy": "renpy.so", "rpgmaker": "rpgmaker.so", "zanzarah": "zanzarah.so"}


def leak_graph(
    repository: Path,
    upstream: Graph,
    *,
    umdh: Path,
    umdh_identity: Mapping[str, str],
    run_nonce: str,
    warmup: int = 8,
    iterations: int = 100,
    windows: int = 3,
    tolerance_bytes: int = 0,
    jobs: int = 4,
) -> Graph:
    """Add one demand target per leak mode/scenario without a monolithic gate."""

    capacities = (jobs,)
    counts = (warmup, iterations, windows)
    require_positive_integers(capacities, "leak pool capacities must be positive integers")
    require_positive_integers(counts, "leak measurement counts must be positive integers")
    if windows < 3:
        raise ValueError("leak measurement requires at least three windows")
    if isinstance(tolerance_bytes, bool) or not isinstance(tolerance_bytes, int) or tolerance_bytes < 0:
        raise ValueError("leak tolerance must be a non-negative integer")
    if not isinstance(run_nonce, str) or not run_nonce or "\0" in run_nonce:
        raise ValueError("run nonce must be a non-empty string without NUL")

    root = repository.resolve(strict=True)
    paths = BuildPaths(root)
    umdh = require_tool(umdh, "UMDH")
    selected = {
        name: canonical_artifact(
            paths, upstream, f"build-{name}-x64-release", filename
        )
        for name, filename in _BINARIES.items()
    }
    factory = recipe_factory(BUILD_ROOT, {})
    umdh_signature = {f"umdh.{key}": value for key, value in umdh_identity.items()} | {"umdh.path": str(umdh)}

    def action(name: str, arguments: tuple[str, ...], dependencies: tuple[Node, ...],
               *, identity: Mapping[str, str] | None = None,
               config: Mapping[str, str] | None = None,
               results: tuple[Result, ...] = ()) -> Node:
        return python_action(
            factory, name, "core.leak", arguments, dependencies,
            pool="slot", identity=identity, config=config, results=results,
        )

    ordered = tuple(selected[name] for name in _BINARIES)
    setup_dependencies = (selected["leak-probe"][0],) + tuple(
        dependency for name, (producer, _path) in zip(_BINARIES, ordered, strict=True)
        if name != "leak-probe"
        for dependency in (producer, *required_audit_gates(upstream, producer, "x64", name))
    )
    setup = action(
        "leak-setup-x64-release", ("setup", *(str(path) for _producer, path in ordered)), setup_dependencies,
        config={"architecture": "x64", "configuration": "Release"},
    )
    setup_output = paths.cas(setup.uid).output
    nodes: list[Node] = [setup]
    targets: list[str] = []

    for mode in LEAK_MODES:
        for scenario in LEAK_SCENARIOS:
            stem = f"{mode}-{scenario}"
            result_root = f"reports/leak/x64/{mode}/{scenario}"
            preflight = action(f"leak-preflight-{stem}",
                               ("preflight", str(setup_output), mode, scenario), (setup,),
                               results=(Result(
                                   f"{result_root}/preflight.json", "leak",
                                   "application/json", "preflight.json",
                               ),))
            capture = action(f"leak-capture-{stem}",
                             ("capture", str(setup_output), str(umdh), mode, scenario,
                              str(warmup), str(iterations), str(windows)),
                             (preflight,), identity=umdh_signature, config={"run_nonce": run_nonce},
                             results=(
                                 Result(f"{result_root}/capture.json", "leak", "application/json", "capture.json"),
                                 Result(f"{result_root}/snapshots", "leak-snapshots", "application/octet-stream", "snapshots"),
                                 Result(f"{result_root}/probe.stderr.log", "log", "text/plain", "probe.stderr.log"),
                             ))
            capture_output = paths.cas(capture.uid).output
            snapshots = capture_output / "snapshots"
            specs = [(f"window-{index}", snapshots / f"window-{index}.txt", snapshots / f"window-{index + 1}.txt")
                     for index in range(1, windows)]
            specs.append(("overall", snapshots / "window-1.txt", snapshots / f"window-{windows}.txt"))
            diffs = tuple(
                action(f"leak-diff-{stem}-{label}",
                       ("diff", str(umdh), str(setup_output), label, str(before), str(after)),
                       (capture,), identity=umdh_signature,
                       results=(
                           Result(f"{result_root}/diffs/{label}.json", "leak-diff", "application/json", "diff.json"),
                           Result(f"{result_root}/diffs/{label}.txt", "leak-diff", "text/plain", "report.txt"),
                       ))
                for label, before, after in specs
            )
            summary = action(f"leak-summary-{stem}",
                (
                    "summarize", mode, scenario, str(warmup), str(iterations), str(windows),
                    str(tolerance_bytes),
                    *(value for (label, _before, _after), item in zip(specs, diffs, strict=True)
                      for value in (label, str(paths.cas(item.uid).output / "diff.json"))),
                ), diffs, results=(Result(
                    f"{result_root}/summary.json", "leak-summary",
                    "application/json", "summary.json",
                ),))
            gate = action(
                f"leak-gate-{stem}",
                ("gate", str(paths.cas(summary.uid).output / "summary.json")),
                (summary,),
            )
            nodes.extend((preflight, capture, *diffs, summary, gate))
            targets.append(gate.name)

    pools = extend_pools(upstream, {"slot": jobs})
    return Graph(upstream.nodes + tuple(nodes), tuple(targets), pools)

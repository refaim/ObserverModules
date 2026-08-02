"""Independent UMDH leak scenarios composed over x64 Release binaries."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from core.graph import Graph, Node
from core.node import NodeFactory
from core.paths import BuildPaths
from core.render import TemplateRenderer
from graphs.audit import BinaryArtifact
from graphs.common import (
    BUILD_ROOT, extend_pools, produced_path, python_action, required_audit_gates,
    require_positive_integers, require_tool,
)


LEAK_MODES = ("operations", "lifecycle")
LEAK_SCENARIOS = ("small-success", "malformed", "cancellation", "read-failure", "write-failure", "large-metadata", "sparse-metadata")
_BINARIES = {"leak-probe": "leak-probe.exe", "renpy": "renpy.so", "rpgmaker": "rpgmaker.so", "zanzarah": "zanzarah.so"}


def _artifacts(paths: BuildPaths, upstream: Graph, artifacts: Iterable[BinaryArtifact]) -> dict[str, BinaryArtifact]:
    selected: dict[str, BinaryArtifact] = {}
    for artifact in artifacts:
        if artifact.architecture != "x64" or artifact.module not in _BINARIES:
            raise ValueError(f"invalid leak binary artifact: {(artifact.architecture, artifact.module)}")
        if artifact.module in selected:
            raise ValueError(f"duplicate leak binary artifact: {artifact.module}")
        binary = produced_path(
            paths, upstream, artifact.producer, artifact.path,
            f"leak binary must be below its exact producer CAS: {artifact.path}",
        )
        if binary.name != _BINARIES[artifact.module]:
            raise ValueError(f"leak binary must be below its exact producer CAS: {binary}")
        selected[artifact.module] = artifact
    missing = _BINARIES.keys() - selected.keys()
    if missing:
        raise ValueError(f"missing leak binary artifacts: {', '.join(sorted(missing))}")
    return selected


def leak_graph(
    repository: Path,
    upstream: Graph,
    artifacts: Iterable[BinaryArtifact],
    *,
    umdh: Path,
    umdh_identity: Mapping[str, str],
    run_nonce: str,
    warmup: int = 8,
    iterations: int = 100,
    windows: int = 3,
    tolerance_bytes: int = 0,
    jobs: int = 4,
    session_jobs: int = 2,
    diff_jobs: int = 4,
) -> Graph:
    """Add one demand target per leak mode/scenario without a monolithic gate."""

    capacities = (jobs, session_jobs, diff_jobs)
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
    selected = _artifacts(paths, upstream, artifacts)
    renderer = TemplateRenderer(BUILD_ROOT / "templates")
    factory = NodeFactory(renderer, BUILD_ROOT, {})
    umdh_signature = {f"umdh.{key}": value for key, value in umdh_identity.items()} | {"umdh.path": str(umdh)}

    def action(name: str, pool: str, arguments: tuple[str, ...], dependencies: tuple[Node, ...],
               *, identity: Mapping[str, str] | None = None, config: Mapping[str, str] | None = None) -> Node:
        return python_action(
            factory, name, "core.leak", arguments, dependencies,
            pool=pool, identity=identity, config=config,
        )

    ordered = tuple(selected[name] for name in _BINARIES)
    setup_dependencies = (selected["leak-probe"].producer,) + tuple(
        dependency for item in ordered if item.module != "leak-probe"
        for dependency in (item.producer, *required_audit_gates(upstream, item.producer, "x64", item.module))
    )
    setup = action(
        "leak-setup-x64-release", "leak", ("setup", *(str(item.path) for item in ordered)), setup_dependencies,
        config={"architecture": "x64", "configuration": "Release"},
    )
    setup_output = paths.cas(setup.uid, setup.name).output
    nodes: list[Node] = [setup]
    targets: list[str] = []

    for mode in LEAK_MODES:
        for scenario in LEAK_SCENARIOS:
            stem = f"{mode}-{scenario}"
            preflight = action(f"leak-preflight-{stem}", "leak",
                               ("preflight", str(setup_output), mode, scenario), (setup,))
            capture = action(f"leak-capture-{stem}", "leak-session",
                             ("capture", str(setup_output), str(umdh), mode, scenario,
                              str(warmup), str(iterations), str(windows)),
                             (preflight,), identity=umdh_signature, config={"run_nonce": run_nonce})
            capture_output = paths.cas(capture.uid, capture.name).output
            snapshots = capture_output / "snapshots"
            specs = [(f"window-{index}", snapshots / f"window-{index}.txt", snapshots / f"window-{index + 1}.txt")
                     for index in range(1, windows)]
            specs.append(("overall", snapshots / "window-1.txt", snapshots / f"window-{windows}.txt"))
            diffs = tuple(
                action(f"leak-diff-{stem}-{label}", "leak-diff",
                       ("diff", str(umdh), str(setup_output), label, str(before), str(after)),
                       (capture,), identity=umdh_signature)
                for label, before, after in specs
            )
            judge = action(f"leak-judge-{stem}", "leak",
                (
                    "judge", mode, scenario, str(warmup), str(iterations), str(windows),
                    str(tolerance_bytes),
                    *(value for (label, _before, _after), item in zip(specs, diffs, strict=True)
                      for value in (label, str(paths.cas(item.uid, item.name).output / "diff.json"))),
                ), diffs)
            nodes.extend((preflight, capture, *diffs, judge))
            targets.append(judge.name)

    pools = extend_pools(upstream, {"leak": jobs, "leak-session": session_jobs, "leak-diff": diff_jobs})
    return Graph(upstream.nodes + tuple(nodes), tuple(targets), pools)

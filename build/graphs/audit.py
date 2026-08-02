"""Fine-grained PE and BinSkim audit nodes over explicit release artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from core.graph import Graph, Node, Result
from core.paths import BuildPaths
from graphs.common import BUILD_ROOT, canonical_artifact, extend_pools, python_action, recipe_factory, require_positive_integers, require_tool


_ARCHITECTURES = {"x86", "x64", "arm64"}
_MODULES = ("renpy", "rpgmaker", "zanzarah")


def audit_graph(
    repository: Path,
    upstream: Graph,
    *,
    architectures: tuple[str, ...] = ("x64",),
    include_leak_probe: bool = False,
    dumpbin: Path,
    dumpbin_identity: Mapping[str, str],
    binskim: Path,
    binskim_identity: Mapping[str, str],
    jobs: int = 2,
) -> Graph:
    """Compose independent release-binary audits over a validated upstream graph."""

    require_positive_integers((jobs,), "audit pool capacities must be positive integers")
    root = repository.resolve(strict=True)
    paths = BuildPaths(root)
    if (
        not architectures
        or len(architectures) != len(set(architectures))
        or any(item not in _ARCHITECTURES for item in architectures)
    ):
        raise ValueError("audit architectures must be a unique non-empty supported set")
    dumpbin = require_tool(dumpbin, "dumpbin")
    binskim = require_tool(binskim, "BinSkim")
    dump_factory = recipe_factory(root, dict(dumpbin_identity) | {"path": str(dumpbin)})
    python_factory = recipe_factory(BUILD_ROOT, {})
    audit_nodes: list[Node] = []
    targets: list[str] = []
    modules = (("leak-probe",) if include_leak_probe else ()) + _MODULES

    for architecture in sorted(architectures):
        for module in modules:
            producer, binary = canonical_artifact(
                paths,
                upstream,
                f"build-{module}-{architecture}-release",
                "leak-probe.exe" if module == "leak-probe" else f"{module}.so",
            )

            dump_nodes = []
            for mode in ("headers", "dependents", "exports"):
                current = dump_factory.make(
                    "argv.json",
                    f"audit-dumpbin-{mode}-{architecture}-{module}",
                    "slot",
                    {"argv": (str(dumpbin), f"/{mode}", str(binary))},
                    files={},
                    dependencies=(producer,),
                    config={"action": mode, "architecture": architecture, "module": module},
                )
                dump_nodes.append(current)
            pe_gate = python_action(
                python_factory,
                f"audit-pe-{architecture}-{module}",
                "core.binary_audit",
                (
                    "pe", architecture,
                    *(str(paths.cas(node.uid).log) for node in dump_nodes),
                ),
                tuple(dump_nodes),
                pool="slot",
            )
            binskim_run = python_action(
                python_factory,
                f"audit-binskim-run-{architecture}-{module}",
                "core.binary_audit",
                ("run-binskim", str(binskim), str(binary)),
                (producer,),
                pool="slot",
                identity=dict(binskim_identity) | {"path": str(binskim)},
                config={"action": "run-binskim", "architecture": architecture, "module": module},
                results=(Result(
                    f"reports/sarif/{architecture}/binskim-{module}.sarif",
                    "sarif",
                    "application/sarif+json",
                    "binskim.sarif",
                ),),
            )
            report = paths.cas(binskim_run.uid).output / "binskim.sarif"
            binskim_gate = python_action(
                python_factory,
                f"audit-binskim-{architecture}-{module}",
                "core.binary_audit",
                ("binskim", str(report)),
                (binskim_run,),
                pool="slot",
            )
            audit_nodes.extend((*dump_nodes, pe_gate, binskim_run, binskim_gate))
            targets.extend((pe_gate.name, binskim_gate.name))

    pools = extend_pools(upstream, {"slot": jobs})
    return Graph(upstream.nodes + tuple(audit_nodes), tuple(targets), pools)

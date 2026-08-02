"""Fine-grained PE and BinSkim audit nodes over explicit release artifacts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
import re

from core.graph import Graph, Node
from core.node import NodeFactory
from core.paths import BuildPaths
from core.render import TemplateRenderer
from graphs.common import BUILD_ROOT, extend_pools, produced_path, python_action, require_positive_integers, require_tool


_ARCHITECTURES = {"x86", "x64", "arm64"}
_MODULE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")


@dataclass(frozen=True, slots=True)
class BinaryArtifact:
    architecture: str
    module: str
    producer: Node
    path: Path


def audit_graph(
    repository: Path,
    upstream: Graph,
    artifacts: Iterable[BinaryArtifact],
    *,
    dumpbin: Path,
    dumpbin_identity: Mapping[str, str],
    binskim: Path,
    binskim_identity: Mapping[str, str],
    jobs: int = 2,
    binskim_jobs: int = 1,
) -> Graph:
    """Compose independent release-binary audits over a validated upstream graph."""

    require_positive_integers((jobs, binskim_jobs), "audit pool capacities must be positive integers")
    root = repository.resolve(strict=True)
    paths = BuildPaths(root)
    dumpbin = require_tool(dumpbin, "dumpbin")
    binskim = require_tool(binskim, "BinSkim")
    renderer = TemplateRenderer(BUILD_ROOT / "templates")
    dump_factory = NodeFactory(renderer, root, dict(dumpbin_identity) | {"path": str(dumpbin)})
    python_factory = NodeFactory(renderer, BUILD_ROOT, {})
    audit_nodes: list[Node] = []
    targets: list[str] = []
    seen: set[tuple[str, str]] = set()

    for artifact in sorted(tuple(artifacts), key=lambda item: (item.architecture, item.module)):
        key = (artifact.architecture, artifact.module)
        if key in seen:
            raise ValueError(f"duplicate binary artifact: {key}")
        seen.add(key)
        if artifact.architecture not in _ARCHITECTURES or _MODULE.fullmatch(artifact.module) is None:
            raise ValueError(f"invalid binary artifact identity: {key}")
        binary = produced_path(
            paths, upstream, artifact.producer, artifact.path,
            f"binary artifact must be below its exact producer CAS: {artifact.path}",
        )

        dump_nodes = []
        for mode in ("headers", "dependents", "exports"):
            current = dump_factory.make(
                "argv.json",
                f"audit-dumpbin-{mode}-{artifact.architecture}-{artifact.module}",
                "dumpbin",
                {"argv": (str(dumpbin), f"/{mode}", str(binary))},
                files={},
                dependencies=(artifact.producer,),
                config={"action": mode, "architecture": artifact.architecture, "module": artifact.module},
            )
            dump_nodes.append(current)
        pe_gate = python_action(
            python_factory,
            f"audit-pe-{artifact.architecture}-{artifact.module}",
            "core.binary_audit",
            (
                "pe", artifact.architecture,
                *(str(paths.cas(node.uid, node.name).log) for node in dump_nodes),
            ),
            tuple(dump_nodes),
            pool="audit",
        )
        binskim_run = python_action(
            python_factory,
            f"audit-binskim-run-{artifact.architecture}-{artifact.module}",
            "core.binary_audit",
            ("run-binskim", str(binskim), str(binary)),
            (artifact.producer,),
            pool="binskim",
            identity=dict(binskim_identity) | {"path": str(binskim)},
            config={"action": "run-binskim", "architecture": artifact.architecture, "module": artifact.module},
        )
        report = paths.cas(binskim_run.uid, binskim_run.name).output / "binskim.sarif"
        binskim_gate = python_action(
            python_factory,
            f"audit-binskim-{artifact.architecture}-{artifact.module}",
            "core.binary_audit",
            ("binskim", str(report)),
            (binskim_run,),
            pool="audit",
        )
        audit_nodes.extend((*dump_nodes, pe_gate, binskim_run, binskim_gate))
        targets.extend((pe_gate.name, binskim_gate.name))

    if not seen:
        raise ValueError("audit graph requires at least one binary artifact")
    pools = extend_pools(upstream, {"audit": jobs, "binskim": binskim_jobs, "dumpbin": jobs})
    return Graph(upstream.nodes + tuple(audit_nodes), tuple(targets), pools)

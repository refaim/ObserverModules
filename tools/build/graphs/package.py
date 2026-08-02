"""Fine-grained deterministic packaging over explicit release build artifacts."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from core.graph import Graph, Node
from core.node import NodeFactory
from core.package import ARCHITECTURES, LICENSES, MODULES
from core.paths import BuildPaths
from core.render import TemplateRenderer
from graphs.common import (
    BUILD_ROOT, extend_pools, produced_path, python_action, required_audit_gates, require_positive_integers,
)


@dataclass(frozen=True, slots=True)
class PackageArtifact:
    architecture: str
    module: str
    producer: Node
    binary: Path
    symbols: Path


@dataclass(frozen=True, slots=True)
class PackageSmokeArtifact:
    architecture: str
    producer: Node
    executable: Path


def package_outputs(repository: Path, graph: Graph) -> tuple[Path, ...]:
    """Return exact CAS ZIP paths from a composed package graph, without materializing copies."""

    paths, by_name, result = BuildPaths(repository.resolve(strict=True)), {node.name: node for node in graph.nodes}, []
    for architecture in sorted(ARCHITECTURES):
        symbols = by_name.get(f"package-symbols-{architecture}")
        if symbols is None:
            continue
        for module in MODULES:
            node = graph.node(f"package-archive-{architecture}-{module}")
            result.append(paths.cas(node.uid, node.name).output / f"{module}-{architecture}-dll.zip")
        result.append(paths.cas(symbols.uid, symbols.name).output / f"observer-modules-{architecture}-pdb.zip")
    if not result:
        raise ValueError("graph has no package outputs")
    return tuple(result)


def package_graph(
    repository: Path,
    upstream: Graph,
    artifacts: Iterable[PackageArtifact],
    *,
    smoke_tests: Iterable[PackageSmokeArtifact] = (),
    jobs: int = 4,
) -> Graph:
    """Compose module and symbol packages without artificial cross-architecture edges."""

    require_positive_integers((jobs,), "package jobs must be a positive integer")
    root = repository.resolve(strict=True)
    paths = BuildPaths(root)
    factory = NodeFactory(TemplateRenderer(BUILD_ROOT / "templates"), BUILD_ROOT, {})

    def action(name: str, arguments: tuple[str, ...], dependencies: tuple[Node, ...],
               files: dict[str, bytes] | None = None) -> Node:
        return python_action(factory, name, "core.package", arguments, dependencies,
                             pool="package", files=files)

    selected = sorted(tuple(artifacts), key=lambda item: (item.architecture, item.module))
    if not selected:
        raise ValueError("package graph requires at least one artifact")

    keys = [(item.architecture, item.module) for item in selected]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate package artifact")
    if any(architecture not in ARCHITECTURES or module not in MODULES for architecture, module in keys):
        raise ValueError("invalid package artifact identity")
    if len(keys) != len(MODULES) * len({architecture for architecture, _module in keys}):
        raise ValueError("each architecture requires the complete module set")

    nodes: list[Node] = []
    archives: dict[str, Node] = {}
    validations: dict[str, Node] = {}
    symbol_stages = {architecture: [] for architecture in sorted({item.architecture for item in selected})}

    def output(node: Node, name: str = "") -> Path:
        return paths.cas(node.uid, node.name).output / name

    def produced(producer: Node, path: Path, name: str, message: str) -> Path:
        current = produced_path(paths, upstream, producer, path, message)
        if current != output(producer, name):
            raise ValueError(message)
        return current

    for artifact in selected:
        architecture, module, producer = artifact.architecture, artifact.module, artifact.producer
        suffix = f"{architecture}-{module}"
        message = "package artifact must use its exact producer CAS"
        binary = produced(producer, artifact.binary, f"{module}.so", message)
        symbols = produced(producer, artifact.symbols, f"{module}.pdb", message)
        gates = required_audit_gates(upstream, producer, architecture, module)
        dependencies = (producer, *gates)

        repository_inputs = (
            f"src/modules/{module}/observer_user.ini",
            "LICENSE.txt",
            *(f"licenses/{name}" for name in LICENSES[module]),
        )
        stage = action(
            f"package-stage-{suffix}",
            ("stage-module", architecture, module, str(binary), str(root)),
            dependencies,
            {name: (root / name).read_bytes() for name in repository_inputs},
        )
        symbol_stage = action(
            f"package-symbol-stage-{suffix}",
            ("stage-symbol", architecture, module, str(symbols)),
            dependencies,
        )
        archive = action(
            f"package-archive-{suffix}",
            ("archive-module", architecture, module, str(output(stage))),
            (stage, *gates),
        )
        archive_name = f"{module}-{architecture}-dll.zip"
        validation = action(
            f"package-validate-{suffix}",
            ("validate-module", architecture, module, str(output(archive, archive_name)), str(output(stage))),
            (archive, stage),
        )
        nodes.extend((stage, symbol_stage, archive, validation))
        archives[archive_name] = archive
        validations[archive_name] = validation
        symbol_stages[architecture].append(symbol_stage)

    for architecture, stages in sorted(symbol_stages.items()):
        current = action(
            f"package-symbols-{architecture}",
            (
                "archive-symbols", architecture,
                *(str(output(stage)) for stage in stages),
            ),
            tuple(stages),
        )
        nodes.append(current)
        archive_name = f"observer-modules-{architecture}-pdb.zip"
        validation = action(
            f"package-symbols-validate-{architecture}",
            ("validate-symbols", architecture, str(output(current, archive_name)),
             *(str(output(stage)) for stage in stages)),
            (current, *stages),
        )
        nodes.append(validation)
        archives[archive_name] = current
        validations[archive_name] = validation

    smokes = sorted(tuple(smoke_tests), key=lambda item: item.architecture)
    if len(smokes) != len({smoke.architecture for smoke in smokes}):
        raise ValueError("duplicate package smoke architecture")
    smoke_nodes = []
    for smoke in smokes:
        message = "package smoke must use its exact test producer CAS"
        if smoke.architecture not in symbol_stages:
            raise ValueError(message)
        test_executable = produced(smoke.producer, smoke.executable, "tests.exe", message)
        for module in MODULES:
            archive_name = f"{module}-{smoke.architecture}-dll.zip"
            archive = archives[archive_name]
            validation = validations[archive_name]
            smoke_node = action(
                f"package-smoke-{smoke.architecture}-{module}",
                (
                    "smoke", smoke.architecture, module,
                    str(output(archive, archive_name)),
                    str(test_executable),
                ),
                (validation, smoke.producer),
            )
            nodes.append(smoke_node)
            smoke_nodes.append(smoke_node)

    aggregate = action(
        "package-manifest",
        (
            "aggregate",
            *(str(output(node, name)) for name, node in archives.items()),
        ),
        tuple(validations.values()) + tuple(smoke_nodes),
    )
    nodes.append(aggregate)
    return Graph(upstream.nodes + tuple(nodes), (aggregate.name,), extend_pools(upstream, {"package": jobs}))

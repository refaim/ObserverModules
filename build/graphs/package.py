"""Fine-grained deterministic packaging over explicit release build artifacts."""

from __future__ import annotations

from pathlib import Path

from core.graph import Graph, Node, Result
from core.package import ARCHITECTURES, LICENSES, MODULES
from core.paths import BuildPaths
from graphs.common import (
    BUILD_ROOT, canonical_artifact, extend_pools, python_action, recipe_factory, required_audit_gates, require_positive_integers,
)


def package_outputs(repository: Path, graph: Graph) -> tuple[Path, ...]:
    """Return gated ZIP paths from the final package-manifest producer."""

    paths = BuildPaths(repository.resolve(strict=True))
    try:
        aggregate = graph.node("package-manifest")
    except ValueError as error:
        raise ValueError("graph has no package outputs") from error
    output = paths.cas(aggregate.uid).output
    result = [output / item.relative_path for item in aggregate.results if item.kind == "package"]
    if not result:
        raise ValueError("graph has no package outputs")
    return tuple(result)


def package_graph(
    repository: Path,
    upstream: Graph,
    *,
    architectures: tuple[str, ...],
    smoke_architectures: tuple[str, ...] = (),
    jobs: int = 4,
) -> Graph:
    """Compose module and symbol packages without artificial cross-architecture edges."""

    require_positive_integers((jobs,), "package jobs must be a positive integer")
    root = repository.resolve(strict=True)
    paths = BuildPaths(root)
    factory = recipe_factory(BUILD_ROOT, {})

    def action(name: str, arguments: tuple[str, ...], dependencies: tuple[Node, ...],
               files: dict[str, bytes] | None = None,
               results: tuple[Result, ...] = ()) -> Node:
        return python_action(factory, name, "core.package", arguments, dependencies,
                             pool="package", files=files, results=results)

    if (
        not architectures
        or len(architectures) != len(set(architectures))
        or any(item not in ARCHITECTURES for item in architectures)
    ):
        raise ValueError("package architectures must be a unique non-empty supported set")
    if (
        len(smoke_architectures) != len(set(smoke_architectures))
        or any(item not in architectures for item in smoke_architectures)
    ):
        raise ValueError("package smoke architectures must be a unique package subset")
    selected_architectures = tuple(sorted(architectures))

    nodes: list[Node] = []
    archives: dict[str, Node] = {}
    validations: dict[str, Node] = {}
    symbol_stages = {architecture: [] for architecture in selected_architectures}

    def output(node: Node, name: str = "") -> Path:
        return paths.cas(node.uid).output / name

    for architecture in selected_architectures:
        for module in MODULES:
            producer, binary = canonical_artifact(
                paths, upstream, f"build-{module}-{architecture}-release", f"{module}.so"
            )
            suffix = f"{architecture}-{module}"
            symbols = paths.cas(producer.uid).output / f"{module}.pdb"
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
                results=(Result(
                    f"reports/package/{architecture}/{module}/validation.json",
                    "package-validation", "application/json", "validation.json",
                ),),
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
            results=(Result(
                f"reports/package/{architecture}/symbols/validation.json",
                "package-validation", "application/json", "validation.json",
            ),),
        )
        nodes.append(validation)
        archives[archive_name] = current
        validations[archive_name] = validation

    smoke_nodes = []
    for architecture in sorted(smoke_architectures):
        test_producer, test_executable = canonical_artifact(
            paths, upstream, f"build-tests-{architecture}-release", "tests.exe"
        )
        for module in MODULES:
            archive_name = f"{module}-{architecture}-dll.zip"
            archive = archives[archive_name]
            validation = validations[archive_name]
            smoke_node = action(
                f"package-smoke-{architecture}-{module}",
                (
                    "smoke", architecture, module,
                    str(output(archive, archive_name)),
                    str(test_executable),
                ),
                (validation, test_producer),
            )
            nodes.append(smoke_node)
            smoke_nodes.append(smoke_node)

    package_results = tuple(
        Result(f"packages/{architecture}/{name}", "package", "application/zip", name)
        for architecture in sorted(symbol_stages)
        for name in (
            *(f"{module}-{architecture}-dll.zip" for module in MODULES),
            f"observer-modules-{architecture}-pdb.zip",
        )
    )
    aggregate = action(
        "package-manifest",
        (
            "aggregate",
            *(str(output(node, name)) for name, node in archives.items()),
        ),
        tuple(validations.values()) + tuple(smoke_nodes),
        results=package_results + (Result(
            "packages/packages.json", "package-manifest", "application/json", "packages.json",
        ),),
    )
    nodes.append(aggregate)
    return Graph(upstream.nodes + tuple(nodes), (aggregate.name,), extend_pools(upstream, {"package": jobs}))

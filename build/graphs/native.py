"""Fine-grained native project build and Catch2 execution graph."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from core.graph import Graph, Node, Result, merge_graphs
from core.node import NodeFactory
from core.paths import BuildPaths
from core.toolchain import MsvcToolchain
from graphs.analysis import (
    dependency_discovery_slice,
    project_build,
    project_inventory,
)
from graphs.common import (
    BINARIES,
    PLATFORMS,
    PROJECTS,
    recipe_factory,
    require_positive_integers,
    tool_environment,
)


_CONFIGURATIONS = ("Debug", "Release")
def _validate_matrix(
    jobs: int, architectures: tuple[str, ...], configurations: tuple[str, ...]
) -> None:
    require_positive_integers((jobs,), "jobs must be a positive integer")
    unsupported = set(architectures) - PLATFORMS.keys()
    if unsupported:
        raise ValueError(f"unsupported architecture: {sorted(unsupported)[0]}")
    invalid = set(configurations) - set(_CONFIGURATIONS)
    if invalid:
        raise ValueError(f"unsupported configuration: {sorted(invalid)[0]}")


def native_dependency_discovery_slice(
    repository: Path,
    toolchain: MsvcToolchain,
    *,
    jobs: int = 2,
    architectures: tuple[str, ...] = ("x64",),
    configurations: tuple[str, ...] = ("Debug",),
    include_leak_probe: bool = False,
) -> Graph:
    """Discover exact native build dependencies per configuration and TU."""

    _validate_matrix(jobs, architectures, configurations)
    graphs = tuple(
        dependency_discovery_slice(
            repository,
            toolchain,
            project_names=PROJECTS + (
                ("leak-probe",) if include_leak_probe and config == "Release" else ()
            ),
            configuration=config,
            name_qualifier=config.lower(),
            jobs=jobs,
            architectures=architectures,
        )
        for config in configurations
    )
    return merge_graphs(*graphs)


def _test_shard(
    repository: Path,
    toolchain: MsvcToolchain,
    factory: NodeFactory,
    paths: BuildPaths,
    builds: tuple[Node, ...],
    architecture: str,
    configuration: str,
    shard_count: int,
    shard_index: int,
    corpus: Path | None,
    run_nonce: str,
) -> Node:
    artifacts = [
        {
            "name": BINARIES[project],
            "source": str(paths.cas(node.uid).output / BINARIES[project]),
        }
        for project, node in zip(PROJECTS, builds, strict=True)
    ]
    prefix = "corpus" if corpus is not None else "test"
    result_scope = "corpus" if corpus is not None else "unit"
    name = f"{prefix}-shard-{architecture}-{configuration.lower()}-{shard_index}"
    config = {
        "action": "test",
        "architecture": architecture,
        "configuration": configuration,
        "shard_count": str(shard_count),
        "shard_index": str(shard_index),
    }
    options = {}
    if corpus is not None:
        config |= {"corpus": str(corpus), "run_nonce": run_nonce}
        options["environment"] = tool_environment(
            toolchain, extra=(("OBSERVER_TEST_CORPUS", str(corpus)),)
        )
    return factory.make(
        "native-corpus-test.ps1" if corpus is not None else "native-test.ps1",
        name,
        "slot",
        {
            "pwsh": str(toolchain.pwsh),
            "artifacts": artifacts,
            "shard_count": shard_count,
            "shard_index": shard_index,
        },
        files={},
        dependencies=builds,
        results=(Result(
            f"reports/tests/{architecture}/{configuration.lower()}/{result_scope}/"
            f"shard-{shard_index}.xml",
            "test",
            "application/xml",
            "tests.xml",
        ),),
        config=config,
        **options,
    )


def native_graph(
    repository: Path,
    toolchain: MsvcToolchain,
    *,
    discovery: Graph | None = None,
    manifests: Mapping[str, bytes] | None = None,
    jobs: int = 2,
    architectures: tuple[str, ...] = ("x64",),
    configurations: tuple[str, ...] = ("Debug",),
    runnable_architectures: tuple[str, ...],
    test_shards: int = 4,
    include_leak_probe: bool = False,
    corpus: Path | None = None,
    run_nonce: str = "",
) -> Graph:
    """Return independently cacheable project builds and safe Catch2 shards."""

    _validate_matrix(jobs, architectures, configurations)
    require_positive_integers((test_shards,), "test_shards must be a positive integer")
    unsupported = set(runnable_architectures) - PLATFORMS.keys()
    if unsupported:
        raise ValueError(f"unsupported architecture: {sorted(unsupported)[0]}")
    corpus_path = None
    if corpus is not None:
        if not isinstance(run_nonce, str) or not run_nonce or "\0" in run_nonce:
            raise ValueError("corpus run nonce must be a non-empty string without NUL")
        corpus_path = Path(corpus).resolve(strict=True)
        if not corpus_path.is_dir():
            raise NotADirectoryError(corpus_path)
    if discovery is None or manifests is None:
        raise ValueError("native dependency discovery is required")

    root = repository.resolve(strict=True)
    factory = recipe_factory(root, dict(toolchain.identity), tool_environment(toolchain))
    paths = BuildPaths(root)
    nodes: list[Node] = list(discovery.nodes)
    targets: list[str] = []
    runnable = set(runnable_architectures)
    projects = project_inventory(root, PROJECTS)

    for architecture in architectures:
        restore = discovery.node(f"restore-vcpkg-{architecture}")
        restore_output = paths.cas(restore.uid).output
        for configuration in configurations:
            def build(project):
                return project_build(
                    root, factory, discovery, manifests, restore_output, project,
                    architecture, configuration.lower(), "native-build.ps1", {
                        "pwsh": str(toolchain.pwsh), "msbuild": str(toolchain.msbuild),
                        "project": str(project.path), "target": "Build",
                        "configuration": configuration, "platform": PLATFORMS[architecture],
                        "vcpkg_root": str(toolchain.vcpkg_root),
                        "vcpkg_installed": str(restore_output),
                    }, config={
                        "action": "build", "architecture": architecture,
                        "configuration": configuration, "project": project.name,
                    },
                )

            builds = tuple(build(project) for project in projects)
            nodes.extend(builds)
            leak_probe = None
            if include_leak_probe and architecture == "x64" and configuration == "Release":
                leak_probe = build(project_inventory(root, ("leak-probe",))[0])
                nodes.append(leak_probe)

            if architecture in runnable:
                runs = [(None, "")]
                if corpus_path is not None:
                    runs.append((corpus_path, run_nonce))
                for shard_corpus, nonce in runs:
                    shards = tuple(
                        _test_shard(
                            root,
                            toolchain,
                            factory,
                            paths,
                            builds,
                            architecture,
                            configuration,
                            test_shards,
                            index,
                            shard_corpus,
                            nonce,
                        )
                        for index in range(test_shards)
                    )
                    nodes.extend(shards)
                    targets.extend(node.name for node in shards)
            else:
                targets.extend(node.name for node in builds)
            if leak_probe is not None:
                targets.append(leak_probe.name)

    return Graph(tuple(nodes), tuple(targets), {"restore": 1, "slot": jobs})

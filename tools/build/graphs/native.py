"""Fine-grained native project build and Catch2 execution graph."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from core.graph import Graph, Node, merge_graphs
from core.node import NodeFactory
from core.paths import BuildPaths
from core.render import TemplateRenderer
from core.toolchain import MsvcToolchain
from graphs.analysis import (
    dependency_discovery_slice,
    dependency_inputs,
    dependency_node_name,
)
from graphs.common import (
    BINARIES,
    BUILD_ROOT,
    COMMON_PROJECT_INPUTS,
    PLATFORMS,
    PROJECTS,
    project_inputs,
    project_path,
    project_sources,
    require_positive_integers,
    tool_environment,
)


_CONFIGURATIONS = ("Debug", "Release")
_COMMON_INPUTS = COMMON_PROJECT_INPUTS
_project_inputs = project_inputs
_relative = project_path


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


def _build(
    repository: Path,
    toolchain: MsvcToolchain,
    factory: NodeFactory,
    discovery: Graph,
    manifests: Mapping[str, bytes],
    restore_output: Path,
    project_name: str,
    architecture: str,
    configuration: str,
    platform: str,
) -> Node:
    project = repository / "build/projects" / f"{project_name}.vcxproj"
    inputs = project_inputs(repository, project)
    files = {path: (repository / path).read_bytes() for path in inputs}
    dependencies = []
    for source in project_sources(repository, project):
        name = dependency_node_name(
            repository, architecture, project_name, source, configuration.lower()
        )
        dependencies.append(discovery.node(name))
        try:
            manifest = manifests[name]
        except KeyError as error:
            raise ValueError(f"missing dependency manifest: {name}") from error
        files.update(dependency_inputs(repository, restore_output, source, manifest))
    return factory.make(
        "native-build.ps1",
        f"build-{project_name}-{architecture}-{configuration.lower()}",
        "slot",
        {
            "pwsh": str(toolchain.pwsh),
            "msbuild": str(toolchain.msbuild),
            "project": str(project),
            "target": "Build",
            "configuration": configuration,
            "platform": platform,
            "vcpkg_root": str(toolchain.vcpkg_root),
            "vcpkg_installed": str(restore_output),
        },
        files=files,
        dependencies=tuple(dependencies),
        config={
            "action": "build",
            "architecture": architecture,
            "configuration": configuration,
            "project": project_name,
        },
    )


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
            "source": str(paths.cas(node.uid, node.name).output / BINARIES[project]),
        }
        for project, node in zip(PROJECTS, builds, strict=True)
    ]
    prefix = "corpus" if corpus is not None else "test"
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
    renderer = TemplateRenderer(BUILD_ROOT / "templates")
    factory = NodeFactory(
        renderer, root, dict(toolchain.identity), tool_environment(toolchain)
    )
    paths = BuildPaths(root)
    nodes: list[Node] = list(discovery.nodes)
    targets: list[str] = []
    runnable = set(runnable_architectures)

    for architecture in architectures:
        restore = discovery.node(f"restore-vcpkg-{architecture}")
        restore_output = paths.cas(restore.uid, restore.name).output
        for configuration in configurations:
            builds = tuple(
                _build(
                    root,
                    toolchain,
                    factory,
                    discovery,
                    manifests,
                    restore_output,
                    project,
                    architecture,
                    configuration,
                    PLATFORMS[architecture],
                )
                for project in PROJECTS
            )
            nodes.extend(builds)
            leak_probe = None
            if include_leak_probe and architecture == "x64" and configuration == "Release":
                leak_probe = _build(
                    root,
                    toolchain,
                    factory,
                    discovery,
                    manifests,
                    restore_output,
                    "leak-probe",
                    architecture,
                    configuration,
                    PLATFORMS[architecture],
                )
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

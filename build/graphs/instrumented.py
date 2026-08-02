"""Staged MSBuild producers for Coverage, ASan, and UBSan graphs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from core.graph import Graph, GraphError, Node, merge_graphs
from core.node import NodeFactory
from core.paths import BuildPaths
from core.quality_tools import UBSAN_LIBRARIES, resolve_llvm
from core.render import TemplateRenderer
from core.toolchain import MsvcToolchain
from graphs.analysis import (
    clang_dependency_discovery_slice,
    dependency_discovery_slice,
    dependency_inputs,
    dependency_node_name,
)
from graphs.common import (
    BINARIES,
    BUILD_ROOT,
    PLATFORMS,
    PROJECTS,
    project_inputs,
    project_sources,
    require_positive_integers,
    tool_environment,
)


_CONFIGURATIONS = {"coverage": "Coverage", "asan": "ASan", "ubsan": "UBSan"}


@dataclass(frozen=True, slots=True)
class InstrumentedVariant:
    kind: str
    architecture: str
    llvm_runtime: Path | None = None
    runtime_identity: Mapping[str, str] = field(default_factory=dict)

    @property
    def configuration(self) -> str:
        return _CONFIGURATIONS.get(self.kind, self.kind)


@dataclass(frozen=True, slots=True)
class InstrumentedArtifact:
    kind: str
    architecture: str
    name: str
    producer: Node
    path: Path


def _variants(
    variants: tuple[InstrumentedVariant, ...], jobs: int
) -> tuple[InstrumentedVariant, ...]:
    require_positive_integers((jobs,), "jobs must be a positive integer")
    if not variants:
        raise ValueError("at least one instrumented variant is required")
    keys = [(item.kind, item.architecture) for item in variants]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate instrumented variant")
    for item in variants:
        supported = (
            item.kind == "coverage" and item.architecture in PLATFORMS
            or item.kind == "asan" and item.architecture in {"x86", "x64"}
            or item.kind == "ubsan" and item.architecture == "x64"
        )
        if not supported:
            raise ValueError(f"unsupported instrumented variant: {(item.kind, item.architecture)}")
        if item.kind == "ubsan":
            runtime = item.llvm_runtime.resolve(strict=True) if item.llvm_runtime else None
            if runtime is None or not runtime.is_dir() or not item.runtime_identity:
                raise ValueError("UBSan runtime directory and exact identity are required")
            for name in UBSAN_LIBRARIES:
                if not (runtime / name).is_file():
                    raise FileNotFoundError(f"UBSan runtime library not found: {runtime / name}")
        elif item.llvm_runtime is not None or item.runtime_identity:
            raise ValueError(f"LLVM runtime is invalid for {item.kind} variant")
    return variants


def instrumented_dependency_discovery_slice(
    repository: Path,
    toolchain: MsvcToolchain,
    *,
    variants: tuple[InstrumentedVariant, ...],
    jobs: int = 2,
) -> Graph:
    """Discover compiler inputs per instrumented configuration and TU."""

    selected = _variants(variants, jobs)
    graphs = tuple(
        (
            clang_dependency_discovery_slice(
                repository,
                toolchain,
                project_names=PROJECTS,
                configuration=item.configuration,
                name_qualifier=item.kind,
                jobs=jobs,
                architectures=(item.architecture,),
            )
            if item.kind in {"coverage", "ubsan"}
            else dependency_discovery_slice(
                repository,
                toolchain,
                project_names=PROJECTS,
                configuration=item.configuration,
                restore_flavor="asan",
                name_qualifier=item.kind,
                jobs=jobs,
                architectures=(item.architecture,),
            )
        )
        for item in selected
    )
    try:
        return merge_graphs(*graphs)
    except GraphError as error:
        name = str(error).rsplit(": ", 1)[-1]
        raise ValueError(f"conflicting canonical node: {name}") from error


def _build(
    repository: Path,
    toolchain: MsvcToolchain,
    factory: NodeFactory,
    discovery: Graph,
    manifests: Mapping[str, bytes],
    variant: InstrumentedVariant,
    project_name: str,
    clang_identity: Mapping[str, str],
) -> Node:
    project = repository / "build/projects" / f"{project_name}.vcxproj"
    files = {
        name: (repository / name).read_bytes()
        for name in project_inputs(repository, project)
    }
    dependencies = []
    restore_name = (
        f"restore-vcpkg-asan-{variant.architecture}"
        if variant.kind == "asan"
        else f"restore-vcpkg-{variant.architecture}"
    )
    restore = discovery.node(restore_name)
    restore_output = BuildPaths(repository).cas(restore.uid, restore.name).output
    for source in project_sources(repository, project):
        name = dependency_node_name(
            repository, variant.architecture, project_name, source, variant.kind
        )
        discovered = discovery.node(name)
        dependencies.append(discovered)
        try:
            manifest = manifests[name]
        except KeyError as error:
            raise ValueError(f"missing dependency manifest: {name}") from error
        files.update(dependency_inputs(repository, restore_output, source, manifest))

    runtime = variant.llvm_runtime.resolve(strict=True) if variant.llvm_runtime else None
    identity = dict(toolchain.identity)
    if variant.kind in {"coverage", "ubsan"}:
        identity.update(clang_identity)
    if runtime is not None:
        identity.update(
            {f"llvm_runtime.{key}": value for key, value in variant.runtime_identity.items()}
        )
        identity["llvm_runtime.path"] = str(runtime)
    return factory.make(
        "instrumented-build.ps1",
        f"build-{project_name}-{variant.architecture}-{variant.kind}",
        "slot",
        {
            "pwsh": str(toolchain.pwsh), "msbuild": str(toolchain.msbuild),
            "project": str(project), "target": "Build",
            "configuration": variant.configuration,
            "platform": PLATFORMS[variant.architecture],
            "vcpkg_root": str(toolchain.vcpkg_root),
            "vcpkg_installed": str(restore_output),
            "llvm_dir": str(toolchain.llvm_dir) if variant.kind in {"coverage", "ubsan"} else "",
            "llvm_runtime": str(runtime) if runtime else "",
        },
        files=files,
        dependencies=tuple(dependencies),
        identity=identity,
        config={
            "action": "build", "kind": variant.kind,
            "architecture": variant.architecture, "project": project_name,
        },
    )


def instrumented_build_slice(
    repository: Path,
    toolchain: MsvcToolchain,
    *,
    discovery: Graph | None,
    manifests: Mapping[str, bytes],
    variants: tuple[InstrumentedVariant, ...],
    jobs: int = 2,
) -> tuple[Graph, tuple[InstrumentedArtifact, ...]]:
    """Build independently cacheable modules/tests from completed discovery."""

    selected = _variants(variants, jobs)
    if discovery is None:
        raise ValueError("instrumented dependency discovery is required")
    if discovery.pools.get("slot") != jobs:
        raise ValueError("conflicting slot pool capacity")
    root = repository.resolve(strict=True)
    factory = NodeFactory(
        TemplateRenderer(BUILD_ROOT / "templates"),
        root,
        dict(toolchain.identity),
        tool_environment(toolchain),
    )
    paths = BuildPaths(root)
    clang_identity: dict[str, str] = {}
    if any(item.kind in {"coverage", "ubsan"} for item in selected):
        clang = resolve_llvm(toolchain, "clang-cl")
        clang_identity = {
            f"clang_cl.{key}": value for key, value in clang.identity
        }
    builds, artifacts = [], []
    for variant in selected:
        for project in PROJECTS:
            build = _build(
                root,
                toolchain,
                factory,
                discovery,
                manifests,
                variant,
                project,
                clang_identity,
            )
            builds.append(build)
            artifacts.append(
                InstrumentedArtifact(
                    variant.kind,
                    variant.architecture,
                    project,
                    build,
                    paths.cas(build.uid, build.name).output / BINARIES[project],
                )
            )
    graph = Graph(
        discovery.nodes + tuple(builds),
        tuple(build.name for build in builds),
        discovery.pools,
    )
    return graph, tuple(artifacts)

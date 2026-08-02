"""Fine-grained ASan/UBSan tests over explicit sanitizer build artifacts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from core.graph import Graph, Node
from core.node import NodeFactory
from core.paths import BuildPaths
from core.render import TemplateRenderer
from core.toolchain import MsvcToolchain
from graphs.common import (
    BINARIES,
    BUILD_ROOT,
    extend_pools,
    prefixed_identity,
    produced_path,
    python_action,
    require_ancestor,
    require_positive_integers,
    require_tool,
    tool_environment,
)
from graphs.instrumented import (
    InstrumentedArtifact,
    InstrumentedVariant,
    instrumented_build_slice,
    instrumented_dependency_discovery_slice,
)


_SUPPORTED = {("asan", "x86"), ("asan", "x64"), ("ubsan", "x64")}
_RUNTIME_NAMES = {
    "x86": "clang_rt.asan_dynamic-i386.dll",
    "x64": "clang_rt.asan_dynamic-x86_64.dll",
}
_OPTIONS = {
    "asan": ("ASAN_OPTIONS", "halt_on_error=1:alloc_dealloc_mismatch=1"),
    "ubsan": ("UBSAN_OPTIONS", "halt_on_error=1:print_stacktrace=1"),
}


@dataclass(frozen=True, slots=True)
class SanitizerArtifact:
    sanitizer: str
    architecture: str
    name: str
    producer: Node
    path: Path


@dataclass(frozen=True, slots=True)
class AsanRuntime:
    architecture: str
    path: Path
    identity: Mapping[str, str]


def _artifacts(
    paths: BuildPaths,
    upstream: Graph,
    artifacts: Iterable[SanitizerArtifact | InstrumentedArtifact],
) -> dict[tuple[str, str], dict[str, SanitizerArtifact | InstrumentedArtifact]]:
    selected: dict[tuple[str, str], dict[str, SanitizerArtifact | InstrumentedArtifact]] = {}
    for artifact in artifacts:
        sanitizer = artifact.sanitizer if isinstance(artifact, SanitizerArtifact) else artifact.kind
        key = (sanitizer, artifact.architecture)
        if key not in _SUPPORTED or artifact.name not in BINARIES:
            raise ValueError(f"invalid sanitizer artifact identity: {(*key, artifact.name)}")
        group = selected.setdefault(key, {})
        if artifact.name in group:
            raise ValueError(f"duplicate sanitizer artifact: {(*key, artifact.name)}")
        expected = f"build-{artifact.name}-{artifact.architecture}-{sanitizer}"
        message = f"sanitizer artifact must use its exact producer CAS: {artifact.path}"
        if artifact.producer.name != expected:
            raise ValueError(message)
        binary = produced_path(paths, upstream, artifact.producer, artifact.path, message)
        producer_output = paths.cas(artifact.producer.uid, artifact.producer.name).output
        if binary != producer_output / BINARIES[artifact.name]:
            raise ValueError(f"sanitizer artifact must use its exact producer CAS: {binary}")
        restore = (
            f"restore-vcpkg-asan-{artifact.architecture}"
            if sanitizer == "asan"
            else f"restore-vcpkg-{artifact.architecture}"
        )
        require_ancestor(
            upstream, artifact.producer, restore,
            f"sanitizer build requires {restore} as a restore ancestor",
        )
        group[artifact.name] = artifact
    if not selected:
        raise ValueError("sanitizer graph requires at least one complete artifact set")
    for key, group in selected.items():
        if group.keys() != BINARIES.keys():
            raise ValueError(f"sanitizer {key} requires the complete artifact set")
    return selected


def _runtimes(
    selected: Mapping[tuple[str, str], object], runtimes: Iterable[AsanRuntime]
) -> dict[str, AsanRuntime]:
    result = {}
    for runtime in runtimes:
        path = require_tool(runtime.path, "ASan runtime")
        if (
            runtime.architecture not in _RUNTIME_NAMES
            or path.name != _RUNTIME_NAMES[runtime.architecture]
            or not runtime.identity
        ):
            raise ValueError(f"invalid ASan runtime identity: {runtime.architecture}")
        if runtime.architecture in result:
            raise ValueError(f"duplicate ASan runtime: {runtime.architecture}")
        result[runtime.architecture] = AsanRuntime(
            runtime.architecture, path, runtime.identity
        )
    required = {architecture for sanitizer, architecture in selected if sanitizer == "asan"}
    if result.keys() != required:
        raise ValueError("an exact ASan runtime is required for each ASan artifact set")
    return result


def sanitizer_artifact_graph(
    repository: Path,
    upstream: Graph,
    artifacts: Iterable[SanitizerArtifact | InstrumentedArtifact],
    *,
    pwsh: Path,
    pwsh_identity: Mapping[str, str],
    asan_runtimes: Iterable[AsanRuntime] = (),
    environment: tuple[tuple[str, str], ...] = (),
    test_shards: int = 4,
    jobs: int = 4,
) -> Graph:
    """Compose test-only sanitizer shards and fail-closed log gates."""

    require_positive_integers(
        (test_shards, jobs),
        "sanitizer counts and pool capacities must be positive integers",
    )
    root = repository.resolve(strict=True)
    paths = BuildPaths(root)
    pwsh = require_tool(pwsh, "PowerShell")
    selected = _artifacts(paths, upstream, artifacts)
    runtimes = _runtimes(selected, asan_runtimes)
    renderer = TemplateRenderer(BUILD_ROOT / "templates")
    pwsh_id = prefixed_identity("pwsh", pwsh, pwsh_identity)
    gate_factory = NodeFactory(renderer, BUILD_ROOT, {})
    nodes: list[Node] = []
    targets: list[str] = []

    for (sanitizer, architecture), group in sorted(selected.items()):
        builds = tuple(group[name].producer for name in BINARIES)
        runtime = runtimes.get(architecture) if sanitizer == "asan" else None
        identity = pwsh_id
        runtime_copy = None
        if runtime is not None:
            identity = identity | prefixed_identity(
                f"asan_runtime.{architecture}", runtime.path, runtime.identity
            )
            runtime_copy = {"name": runtime.path.name, "source": str(runtime.path)}
        factory = NodeFactory(renderer, root, identity, environment)
        copies = tuple(
            {"name": BINARIES[name], "source": str(group[name].path)}
            for name in BINARIES
        )
        options_name, options_value = _OPTIONS[sanitizer]
        for index in range(test_shards):
            shard = factory.make(
                "sanitizer-test.ps1",
                f"{sanitizer}-test-{architecture}-{index}",
                "sanitizer-shard",
                {
                    "pwsh": str(pwsh), "artifacts": copies, "runtime": runtime_copy,
                    "options_name": options_name, "options_value": options_value,
                    "shard_count": test_shards, "shard_index": index,
                },
                files={}, dependencies=builds,
                config={
                    "action": "sanitizer-test", "sanitizer": sanitizer,
                    "architecture": architecture, "shard_count": str(test_shards),
                    "shard_index": str(index),
                },
            )
            log = paths.cas(shard.uid, shard.name).log
            gate = python_action(
                gate_factory,
                f"{sanitizer}-gate-{architecture}-{index}",
                "core.sanitizer",
                ("gate", sanitizer, str(log)),
                (shard,),
                pool="sanitizer-gate",
            )
            nodes.extend((shard, gate))
            targets.append(gate.name)

    return Graph(
        upstream.nodes + tuple(nodes), tuple(targets),
        extend_pools(upstream, {"sanitizer-shard": jobs, "sanitizer-gate": jobs}),
    )


def _instrumented_variants(
    selections: tuple[tuple[str, str], ...],
    llvm_runtime: Path | None,
    llvm_runtime_identity: Mapping[str, str],
) -> tuple[InstrumentedVariant, ...]:
    return tuple(
        InstrumentedVariant(
            sanitizer,
            architecture,
            llvm_runtime if sanitizer == "ubsan" else None,
            llvm_runtime_identity if sanitizer == "ubsan" else {},
        )
        for sanitizer, architecture in selections
    )


def sanitizer_dependency_discovery_slice(
    repository: Path,
    toolchain: MsvcToolchain,
    *,
    llvm_runtime: Path | None,
    llvm_runtime_identity: Mapping[str, str],
    selections: tuple[tuple[str, str], ...] = (
        ("asan", "x86"), ("asan", "x64"), ("ubsan", "x64")
    ),
    jobs: int = 2,
) -> Graph:
    """Discover exact TU inputs for each requested sanitizer build."""

    return instrumented_dependency_discovery_slice(
        repository,
        toolchain,
        variants=_instrumented_variants(
            selections, llvm_runtime, llvm_runtime_identity
        ),
        jobs=jobs,
    )


def sanitizer_graph(
    repository: Path,
    toolchain: MsvcToolchain,
    *,
    discovery: Graph | None,
    manifests: Mapping[str, bytes],
    llvm_runtime: Path | None,
    llvm_runtime_identity: Mapping[str, str],
    asan_runtimes: Iterable[AsanRuntime] = (),
    selections: tuple[tuple[str, str], ...] = (
        ("asan", "x86"), ("asan", "x64"), ("ubsan", "x64")
    ),
    jobs: int = 4,
    test_shards: int = 4,
) -> Graph:
    """Build sanitizer artifacts and compose independent shard gates."""

    variants = _instrumented_variants(
        selections, llvm_runtime, llvm_runtime_identity
    )
    upstream, produced = instrumented_build_slice(
        repository,
        toolchain,
        discovery=discovery,
        manifests=manifests,
        variants=variants,
        jobs=jobs,
    )
    return sanitizer_artifact_graph(
        repository,
        upstream,
        produced,
        pwsh=toolchain.pwsh,
        pwsh_identity=dict(toolchain.identity),
        asan_runtimes=asan_runtimes,
        environment=tool_environment(toolchain),
        test_shards=test_shards,
        jobs=jobs,
    )

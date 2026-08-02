"""Fine-grained ASan/UBSan tests over explicit sanitizer build artifacts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from core.graph import Graph, Node, Result
from core.paths import BuildPaths
from core.toolchain import MsvcToolchain
from graphs.common import (
    BINARIES,
    BUILD_ROOT,
    canonical_artifact,
    extend_pools,
    prefixed_identity,
    python_action,
    recipe_factory,
    require_ancestor,
    require_positive_integers,
    require_tool,
    tool_environment,
)
from graphs.instrumented import InstrumentedVariant, instrumented_build_slice, instrumented_dependency_discovery_slice


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
class AsanRuntime:
    architecture: str
    path: Path
    identity: Mapping[str, str]


def _builds(
    paths: BuildPaths,
    upstream: Graph,
    selections: tuple[tuple[str, str], ...],
) -> dict[tuple[str, str], dict[str, tuple[Node, Path]]]:
    if not selections or len(selections) != len(set(selections)) or any(
        item not in _SUPPORTED for item in selections
    ):
        raise ValueError("sanitizer selections must be a unique non-empty supported set")
    selected = {}
    for sanitizer, architecture in selections:
        group = {}
        for name, filename in BINARIES.items():
            producer, path = canonical_artifact(
                paths, upstream, f"build-{name}-{architecture}-{sanitizer}", filename
            )
            group[name] = (producer, path)
        restore = (
            f"restore-vcpkg-asan-{architecture}"
            if sanitizer == "asan"
            else f"restore-vcpkg-{architecture}"
        )
        for producer, _path in group.values():
            require_ancestor(
                upstream, producer, restore,
                f"sanitizer build requires {restore} as a restore ancestor",
            )
        selected[(sanitizer, architecture)] = group
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
    *,
    selections: tuple[tuple[str, str], ...],
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
    selected = _builds(paths, upstream, selections)
    runtimes = _runtimes(selected, asan_runtimes)
    pwsh_id = prefixed_identity("pwsh", pwsh, pwsh_identity)
    gate_factory = recipe_factory(BUILD_ROOT, {})
    nodes: list[Node] = []
    targets: list[str] = []

    for (sanitizer, architecture), group in sorted(selected.items()):
        builds = tuple(group[name][0] for name in BINARIES)
        runtime = runtimes.get(architecture) if sanitizer == "asan" else None
        identity = pwsh_id
        runtime_copy = None
        if runtime is not None:
            identity = identity | prefixed_identity(
                f"asan_runtime.{architecture}", runtime.path, runtime.identity
            )
            runtime_copy = {"name": runtime.path.name, "source": str(runtime.path)}
        factory = recipe_factory(root, identity, environment)
        copies = tuple(
            {"name": BINARIES[name], "source": str(group[name][1])}
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
                results=(Result(
                    f"reports/sanitizers/{sanitizer}/{architecture}/shard-{index}.xml",
                    "sanitizer",
                    "application/xml",
                    "tests.xml",
                ),),
                config={
                    "action": "sanitizer-test", "sanitizer": sanitizer,
                    "architecture": architecture, "shard_count": str(test_shards),
                    "shard_index": str(index),
                },
            )
            log = paths.cas(shard.uid).log
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
    upstream = instrumented_build_slice(
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
        selections=selections,
        pwsh=toolchain.pwsh,
        pwsh_identity=dict(toolchain.identity),
        asan_runtimes=asan_runtimes,
        environment=tool_environment(toolchain),
        test_shards=test_shards,
        jobs=jobs,
    )

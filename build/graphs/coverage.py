"""Fine-grained LLVM source coverage over explicit instrumented build artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from core.graph import Graph, Node, Result
from core.paths import BuildPaths
from core.quality_tools import resolve_llvm, resolve_tool
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


_ARCHITECTURES = {"x86", "x64", "arm64"}
_IGNORED_SOURCES = (
    r"([\\/]src[\\/](tests|fuzz)[\\/])|([\\/]vcpkg_installed[\\/])|"
    r"([\\/]Microsoft Visual Studio[\\/])|([\\/]Windows Kits[\\/])"
)


def _builds(
    paths: BuildPaths,
    upstream: Graph,
    architectures: tuple[str, ...],
) -> dict[str, dict[str, tuple[Node, Path]]]:
    if (
        not architectures
        or len(architectures) != len(set(architectures))
        or any(item not in _ARCHITECTURES for item in architectures)
    ):
        raise ValueError("coverage architectures must be a unique non-empty supported set")
    selected = {}
    for architecture in architectures:
        group = {}
        for name, filename in BINARIES.items():
            producer, path = canonical_artifact(
                paths, upstream, f"build-{name}-{architecture}-coverage", filename
            )
            group[name] = (producer, path)
        restore = f"restore-vcpkg-{architecture}"
        for producer, _path in group.values():
            require_ancestor(
                upstream, producer, restore,
                f"coverage build requires {restore} as a restore ancestor",
            )
        selected[architecture] = group
    return selected


def coverage_artifact_graph(
    repository: Path,
    upstream: Graph,
    *,
    architectures: tuple[str, ...],
    pwsh: Path,
    pwsh_identity: Mapping[str, str],
    llvm_profdata: Path,
    llvm_profdata_identity: Mapping[str, str],
    llvm_cov: Path,
    llvm_cov_identity: Mapping[str, str],
    environment: tuple[tuple[str, str], ...] = (),
    test_shards: int = 4,
    jobs: int = 4,
    report_jobs: int = 2,
    corpus: Path | None = None,
    run_nonce: str = "",
) -> Graph:
    """Compose shard, merge, report, and immutable 100% gates per runnable arch."""

    require_positive_integers(
        (test_shards, jobs, report_jobs),
        "coverage counts and pool capacities must be positive integers",
    )
    corpus_path = None
    if corpus is not None:
        if not isinstance(run_nonce, str) or not run_nonce or "\0" in run_nonce:
            raise ValueError("corpus run nonce must be a non-empty string without NUL")
        corpus_path = Path(corpus).resolve(strict=True)
        if not corpus_path.is_dir():
            raise NotADirectoryError(corpus_path)
    root = repository.resolve(strict=True)
    paths = BuildPaths(root)
    pwsh = require_tool(pwsh, "PowerShell")
    llvm_profdata = require_tool(llvm_profdata, "llvm-profdata")
    llvm_cov = require_tool(llvm_cov, "llvm-cov")
    selected = _builds(paths, upstream, architectures)
    pwsh_id = prefixed_identity("pwsh", pwsh, pwsh_identity)
    shard_factory = recipe_factory(root, pwsh_id, environment)
    merge_factory = recipe_factory(
        root,
        pwsh_id | prefixed_identity("llvm_profdata", llvm_profdata, llvm_profdata_identity),
        environment,
    )
    report_factory = recipe_factory(
        root,
        pwsh_id | prefixed_identity("llvm_cov", llvm_cov, llvm_cov_identity),
        environment,
    )
    gate_factory = recipe_factory(BUILD_ROOT, {})
    nodes: list[Node] = []
    targets: list[str] = []

    def output(node: Node, filename: str = "") -> Path:
        return paths.cas(node.uid).output / filename

    for architecture, group in sorted(selected.items()):
        builds = tuple(group[name][0] for name in BINARIES)
        copies = tuple(
            {"name": BINARIES[name], "source": str(group[name][1])} for name in BINARIES
        )
        shards = tuple(
            shard_factory.make(
                "coverage-test.ps1",
                f"coverage-test-{architecture}-{index}",
                "coverage-shard",
                {
                    "pwsh": str(pwsh), "artifacts": copies,
                    "shard_count": test_shards, "shard_index": index,
                },
                files={}, dependencies=builds,
                results=(Result(
                    f"reports/coverage/cpp/{architecture}/tests/shard-{index}.xml",
                    "test",
                    "application/xml",
                    "tests.xml",
                ),),
                config={
                    "action": "coverage-test", "architecture": architecture,
                    "shard_count": str(test_shards), "shard_index": str(index),
                },
            )
            for index in range(test_shards)
        )
        corpus_shards: tuple[Node, ...] = ()
        if corpus_path is not None:
            corpus_environment = tuple(
                pair for pair in environment
                if pair[0].casefold() != "observer_test_corpus"
            ) + (("OBSERVER_TEST_CORPUS", str(corpus_path)),)
            corpus_shards = tuple(
                shard_factory.make(
                    "coverage-corpus-test.ps1",
                    f"coverage-corpus-{architecture}-{index}",
                    "coverage-shard",
                    {
                        "pwsh": str(pwsh), "artifacts": copies,
                        "shard_count": test_shards, "shard_index": index,
                    },
                    files={}, dependencies=builds,
                    results=(Result(
                        f"reports/coverage/cpp/{architecture}/corpus/shard-{index}.xml",
                        "test",
                        "application/xml",
                        "tests.xml",
                    ),),
                    config={
                        "action": "coverage-corpus-test",
                        "architecture": architecture,
                        "shard_count": str(test_shards),
                        "shard_index": str(index),
                        "corpus": str(corpus_path),
                        "run_nonce": run_nonce,
                    },
                    environment=corpus_environment,
                )
                for index in range(test_shards)
            )
        profile_shards = shards + corpus_shards
        merge = merge_factory.make(
            "coverage-merge.ps1",
            f"coverage-merge-{architecture}",
            "coverage-merge",
            {
                "pwsh": str(pwsh), "llvm_profdata": str(llvm_profdata),
                "profile_directories": tuple(str(output(shard)) for shard in profile_shards),
            },
            files={}, dependencies=profile_shards,
            config={"action": "coverage-merge", "architecture": architecture},
        )
        profile = output(merge, "coverage.profdata")
        reports = []
        for kind, summary_only in (("json", True), ("lcov", False)):
            report = report_factory.make(
                "coverage-report.ps1",
                f"coverage-{kind}-{architecture}",
                "coverage-report",
                {
                    "pwsh": str(pwsh), "llvm_cov": str(llvm_cov),
                    "test_executable": str(group["tests"][1]),
                    "profile": str(profile), "ignore_regex": _IGNORED_SOURCES,
                    "objects": tuple(str(group[name][1]) for name in BINARIES if name != "tests"),
                    "summary_only": summary_only, "report_name": f"coverage.{kind}",
                },
                files={}, dependencies=(merge, *builds),
                results=(Result(
                    f"reports/coverage/cpp/{architecture}/coverage.{kind}",
                    "coverage",
                    "application/json" if kind == "json" else "text/plain",
                    f"coverage.{kind}",
                ),),
                config={"action": f"coverage-{kind}", "architecture": architecture},
            )
            reports.append(report)
        gate = python_action(
            gate_factory,
            f"coverage-{architecture}",
            "core.cpp_coverage",
            ("gate", str(output(reports[0], "coverage.json"))),
            tuple(reports),
            pool="coverage-gate",
        )
        nodes.extend((*profile_shards, merge, *reports, gate))
        targets.append(gate.name)

    pools = extend_pools(
        upstream,
        {
            "coverage-shard": jobs,
            "coverage-merge": len(selected),
            "coverage-report": report_jobs,
            "coverage-gate": jobs,
        },
    )
    return Graph(upstream.nodes + tuple(nodes), tuple(targets), pools)


def coverage_dependency_discovery_slice(
    repository: Path,
    toolchain: MsvcToolchain,
    *,
    jobs: int = 2,
    architectures: tuple[str, ...] = ("x64",),
) -> Graph:
    """Discover exact inputs for every requested Coverage build."""

    variants = tuple(InstrumentedVariant("coverage", item) for item in architectures)
    return instrumented_dependency_discovery_slice(
        repository, toolchain, variants=variants, jobs=jobs
    )


def coverage_graph(
    repository: Path,
    toolchain: MsvcToolchain,
    *,
    discovery: Graph | None,
    manifests: Mapping[str, bytes],
    jobs: int = 4,
    architectures: tuple[str, ...] = ("x64",),
    test_shards: int = 4,
    report_jobs: int = 2,
    corpus: Path | None = None,
    run_nonce: str = "",
) -> Graph:
    """Build instrumented C++ artifacts, run shards, report, and gate coverage."""

    variants = tuple(InstrumentedVariant("coverage", item) for item in architectures)
    upstream = instrumented_build_slice(
        repository,
        toolchain,
        discovery=discovery,
        manifests=manifests,
        variants=variants,
        jobs=jobs,
    )
    pwsh = resolve_tool(toolchain.pwsh, "PowerShell")
    profdata = resolve_llvm(toolchain, "llvm-profdata")
    cov = resolve_llvm(toolchain, "llvm-cov")
    return coverage_artifact_graph(
        repository,
        upstream,
        architectures=architectures,
        pwsh=pwsh.path,
        pwsh_identity=dict(pwsh.identity),
        llvm_profdata=profdata.path,
        llvm_profdata_identity=dict(profdata.identity),
        llvm_cov=cov.path,
        llvm_cov_identity=dict(cov.identity),
        environment=tool_environment(toolchain),
        test_shards=test_shards,
        jobs=jobs,
        report_jobs=report_jobs,
        corpus=corpus,
        run_nonce=run_nonce,
    )

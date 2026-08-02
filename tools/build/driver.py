"""Thin command orchestration over the independently tested build graphs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import psutil

from core.graph import Graph, Node, merge_graphs
from core.host import require_runnable, runnable_architectures, verify_route
from core.node import NodeFactory
from core.paths import BuildPaths
from core.quality_tools import (
    ResolvedTool,
    resolve_binskim,
    resolve_dumpbin,
    resolve_asan_runtimes,
    resolve_umdh,
    resolve_ubsan_runtime,
)
from core.runtime import BuildRuntime
from core.source_tools import SourceTools, discover_source_tools
from core.toolchain import MsvcToolchain
from core.render import TemplateRenderer
from graphs.analysis import (analysis_discovery_slice, analysis_slice,
                             load_dependency_manifests)
from graphs.audit import BinaryArtifact, audit_graph
from graphs.common import BUILD_ROOT, require_positive_integers, restore_node, tool_environment
from graphs.coverage import coverage_dependency_discovery_slice, coverage_graph
from graphs.fuzz import (
    FUZZ_TARGETS,
    FuzzCorpusArtifact,
    fuzz_dependency_discovery_slice,
    fuzz_graph,
)
from graphs.python_coverage import python_coverage_graph
from graphs.sanitizer import (
    AsanRuntime,
    sanitizer_dependency_discovery_slice,
    sanitizer_graph,
)
from graphs.leak import leak_graph
from graphs.native import native_dependency_discovery_slice, native_graph
from graphs.package import PackageArtifact, PackageSmokeArtifact, package_graph, package_outputs
from graphs.source import source_checks as source_checks_graph


_MODULES = ("renpy", "rpgmaker", "zanzarah")
_SANITIZER_ARCHITECTURES = {
    "asan": ("ASan", frozenset(("x86", "x64"))),
    "ubsan": ("UBSan", frozenset(("x64",))),
}


def _sanitizer_selections(
    sanitizer: str, architectures: tuple[str, ...]
) -> tuple[tuple[str, str], ...]:
    label, supported = _SANITIZER_ARCHITECTURES[sanitizer]
    unsupported = next(
        (architecture for architecture in architectures if architecture not in supported),
        None,
    )
    if unsupported is not None:
        raise ValueError(f"{label} does not support {unsupported}")
    return tuple((sanitizer, architecture) for architecture in require_runnable(architectures))


class Driver:
    """Own one repository-local runtime and compose command-specific graph families."""

    def __init__(self, repository: Path, run_id: str, toolchain: MsvcToolchain,
                 *, jobs: int | None = None) -> None:
        self.repository = repository.resolve(strict=True)
        self.jobs = (psutil.cpu_count() or 1) if jobs is None else jobs
        require_positive_integers((self.jobs,), "jobs must be a positive integer")
        self.toolchain = toolchain
        self.runtime = BuildRuntime(self.repository, run_id)

    async def _run(self, graph: Graph) -> tuple[Path, ...]:
        await self.runtime.executor(graph).run()
        return tuple(self.runtime.store.paths_for(graph.node(name)).output for name in graph.targets)

    async def _staged(self, discovery: Graph, compose: Callable[..., Graph],
                      **options: object) -> Graph:
        await self._run(discovery)
        return compose(
            self.repository, self.toolchain, discovery=discovery,
            manifests=load_dependency_manifests(self.repository, discovery),
            jobs=self.jobs, **options,
        )

    async def _native(self, architectures: tuple[str, ...], configurations: tuple[str, ...],
                      runnable: tuple[str, ...], *, test_shards: int = 4,
                      include_leak_probe: bool = False, corpus: Path | None = None,
                      run_nonce: str = "") -> Graph:
        discovery = native_dependency_discovery_slice(
            self.repository, self.toolchain, jobs=self.jobs, architectures=architectures,
            configurations=configurations, include_leak_probe=include_leak_probe,
        )
        return await self._staged(
            discovery, native_graph, architectures=architectures, configurations=configurations,
            runnable_architectures=runnable, test_shards=test_shards,
            include_leak_probe=include_leak_probe, corpus=corpus, run_nonce=run_nonce,
        )

    def _release(self, graph: Graph, architecture: str, module: str) -> tuple[Node, Path]:
        producer = graph.node(f"build-{module}-{architecture}-release")
        return producer, BuildPaths(self.repository).cas(producer.uid, producer.name).output

    def _binaries(self, graph: Graph, architectures: tuple[str, ...], *,
                  include_leak_probe: bool = False) -> tuple[BinaryArtifact, ...]:
        modules = (("leak-probe",) if include_leak_probe else ()) + _MODULES
        return tuple(
            BinaryArtifact(architecture, module, producer, output /
                           ("leak-probe.exe" if module == "leak-probe" else f"{module}.so"))
            for architecture in architectures for module in modules
            for producer, output in (self._release(graph, architecture, module),)
        )

    def _packages(self, graph: Graph, architectures: tuple[str, ...]) -> tuple[PackageArtifact, ...]:
        return tuple(
            PackageArtifact(architecture, module, producer, output / f"{module}.so",
                            output / f"{module}.pdb")
            for architecture in architectures for module in _MODULES
            for producer, output in (self._release(graph, architecture, module),)
        )

    def _smokes(self, graph: Graph, architectures: tuple[str, ...]) -> tuple[PackageSmokeArtifact, ...]:
        return tuple(
            PackageSmokeArtifact(architecture, producer, output / "tests.exe")
            for architecture in architectures
            for producer, output in (self._release(graph, architecture, "tests"),)
        )

    async def _audited_release(self, architectures: tuple[str, ...], dumpbin: ResolvedTool,
                               binskim: ResolvedTool, binskim_jobs: int, *,
                               include_leak_probe: bool = False) -> Graph:
        upstream = await self._native(
            architectures, ("Release",), (), include_leak_probe=include_leak_probe
        )
        return audit_graph(
            self.repository, upstream, self._binaries(upstream, architectures),
            dumpbin=dumpbin.path, dumpbin_identity=dict(dumpbin.identity),
            binskim=binskim.path, binskim_identity=dict(binskim.identity),
            jobs=self.jobs, binskim_jobs=binskim_jobs,
        )

    async def restore(self, architectures: tuple[str, ...] = ("x64",),
                      flavors: tuple[str, ...] = ("",)) -> tuple[Path, ...]:
        factory = NodeFactory(
            TemplateRenderer(BUILD_ROOT / "templates"), self.repository, {},
            tool_environment(self.toolchain),
        )
        nodes = tuple(
            restore_node(self.repository, self.toolchain, factory, architecture, flavor=flavor)
            for flavor in flavors for architecture in architectures
        )
        return await self._run(
            Graph(nodes, tuple(node.name for node in nodes), {"restore": 1})
        )

    async def build(self, architectures: tuple[str, ...] = ("x64",),
                    configurations: tuple[str, ...] = ("Debug",)) -> tuple[Path, ...]:
        return await self._run(await self._native(architectures, configurations, ()))

    async def test(self, architectures: tuple[str, ...] = ("x64",),
                   configurations: tuple[str, ...] = ("Debug",), *, test_shards: int = 4,
                   corpus: Path | None = None, run_nonce: str = "") -> tuple[Path, ...]:
        graph = await self._native(
            architectures, configurations, require_runnable(architectures),
            test_shards=test_shards, corpus=corpus, run_nonce=run_nonce,
        )
        return await self._run(graph)

    async def source_checks(self, architectures: tuple[str, ...],
                            tools: SourceTools) -> tuple[Path, ...]:
        graph = source_checks_graph(
            self.repository, tools, jobs=self.jobs, architectures=architectures
        )
        return await self._run(graph)

    async def compiler_analysis(self, architectures: tuple[str, ...] = ("x64",)) -> tuple[Path, ...]:
        discovery = analysis_discovery_slice(
            self.repository, self.toolchain, jobs=self.jobs, architectures=architectures
        )
        graph = await self._staged(discovery, analysis_slice, architectures=architectures)
        return await self._run(graph)

    async def test_coverage(
        self, architectures: tuple[str, ...] = ("x64",), *, test_shards: int = 4,
        report_jobs: int = 2, corpus: Path | None = None, run_nonce: str = "",
    ) -> tuple[Path, ...]:
        if corpus is not None and (
            not isinstance(run_nonce, str) or not run_nonce or "\0" in run_nonce
        ):
            raise ValueError("corpus run nonce must be a non-empty string without NUL")
        runnable = require_runnable(architectures)
        discovery = coverage_dependency_discovery_slice(
            self.repository, self.toolchain, jobs=self.jobs, architectures=runnable
        )
        graph = await self._staged(
            discovery, coverage_graph, architectures=runnable,
            test_shards=test_shards, report_jobs=report_jobs,
            corpus=corpus, run_nonce=run_nonce,
        )
        return await self._run(graph)

    async def _sanitizer(
        self, selections: tuple[tuple[str, str], ...], *, llvm_runtime: Path | None,
        llvm_runtime_identity: dict[str, str], asan_runtimes: tuple[AsanRuntime, ...],
        test_shards: int,
    ) -> tuple[Path, ...]:
        discovery = sanitizer_dependency_discovery_slice(
            self.repository, self.toolchain,
            llvm_runtime=llvm_runtime, llvm_runtime_identity=llvm_runtime_identity,
            selections=selections, jobs=self.jobs,
        )
        graph = await self._staged(
            discovery, sanitizer_graph,
            llvm_runtime=llvm_runtime, llvm_runtime_identity=llvm_runtime_identity,
            asan_runtimes=asan_runtimes, selections=selections,
            test_shards=test_shards,
        )
        return await self._run(graph)

    async def test_asan(
        self, architectures: tuple[str, ...] = ("x64",), *, test_shards: int = 4,
    ) -> tuple[Path, ...]:
        selections = _sanitizer_selections("asan", architectures)
        runtimes = tuple(
            AsanRuntime(architecture, tool.path, dict(tool.identity))
            for architecture, tool in resolve_asan_runtimes(
                self.toolchain, tuple(architecture for _, architecture in selections)
            )
        )
        return await self._sanitizer(
            selections, llvm_runtime=None, llvm_runtime_identity={},
            asan_runtimes=runtimes, test_shards=test_shards,
        )

    async def test_ubsan(
        self, architectures: tuple[str, ...] = ("x64",), *, test_shards: int = 4,
    ) -> tuple[Path, ...]:
        selections = _sanitizer_selections("ubsan", architectures)
        runtime = resolve_ubsan_runtime(self.toolchain)
        return await self._sanitizer(
            selections, llvm_runtime=runtime.path,
            llvm_runtime_identity=dict(runtime.identity), asan_runtimes=(),
            test_shards=test_shards,
        )

    async def python_coverage(self) -> tuple[Path, ...]:
        return await self._run(python_coverage_graph(self.repository))

    async def fuzz(self, *, run_nonce: str, seconds: int = 60, fuzz_jobs: int = 2,
                   prior_corpora: tuple[FuzzCorpusArtifact, ...] = (),
                   targets: tuple[str, ...] = FUZZ_TARGETS) -> tuple[Path, ...]:
        require_runnable(("x64",))
        discovery = fuzz_dependency_discovery_slice(
            self.repository, self.toolchain, jobs=self.jobs, targets=targets
        )
        graph = await self._staged(
            discovery, fuzz_graph, run_nonce=run_nonce, seconds=seconds,
            fuzz_jobs=fuzz_jobs, prior_corpora=prior_corpora, targets=targets,
        )
        return await self._run(graph)

    async def audit(self, architectures: tuple[str, ...] = ("x64",), *,
                    dumpbin: ResolvedTool, binskim: ResolvedTool,
                    binskim_jobs: int = 1) -> tuple[Path, ...]:
        return await self._run(
            await self._audited_release(architectures, dumpbin, binskim, binskim_jobs)
        )

    async def package(self, architectures: tuple[str, ...] = ("x64",), *,
                      dumpbin: ResolvedTool, binskim: ResolvedTool,
                      binskim_jobs: int = 1) -> tuple[Path, ...]:
        audited = await self._audited_release(architectures, dumpbin, binskim, binskim_jobs)
        graph = package_graph(
            self.repository, audited, self._packages(audited, architectures),
            smoke_tests=self._smokes(audited, runnable_architectures(architectures)), jobs=self.jobs,
        )
        await self._run(graph)
        return package_outputs(self.repository, graph)

    async def test_leaks(self, *, run_nonce: str, dumpbin: ResolvedTool,
                         binskim: ResolvedTool, umdh: ResolvedTool, binskim_jobs: int = 1,
                         warmup: int = 8, iterations: int = 100, windows: int = 3,
                         tolerance_bytes: int = 0, session_jobs: int = 2,
                         diff_jobs: int = 4) -> tuple[Path, ...]:
        require_runnable(("x64",))
        audited = await self._audited_release(
            ("x64",), dumpbin, binskim, binskim_jobs, include_leak_probe=True
        )
        graph = leak_graph(
            self.repository, audited,
            self._binaries(audited, ("x64",), include_leak_probe=True),
            umdh=umdh.path, umdh_identity=dict(umdh.identity), run_nonce=run_nonce,
            warmup=warmup, iterations=iterations, windows=windows,
            tolerance_bytes=tolerance_bytes, jobs=self.jobs,
            session_jobs=session_jobs, diff_jobs=diff_jobs,
        )
        return await self._run(graph)

    async def verify(
        self, architectures: tuple[str, ...] = ("x64",), *, corpus: Path | None = None,
        run_nonce: str, fuzz_seconds: int = 60, test_shards: int = 4,
        warmup: int = 8, iterations: int = 100, windows: int = 3,
        tolerance_bytes: int = 0,
    ) -> tuple[Path, ...]:
        """Run every host-capable gate in one shared-pool graph after one discovery union."""

        route = verify_route(architectures)
        source_tools = discover_source_tools(self.toolchain)
        dumpbin, binskim = resolve_dumpbin(self.toolchain), resolve_binskim()
        asan_tools = resolve_asan_runtimes(self.toolchain, route.asan) if route.asan else ()
        ubsan = resolve_ubsan_runtime(self.toolchain) if route.ubsan else None
        umdh = resolve_umdh() if route.run_x64_specialists else None
        selections = tuple(("asan", item) for item in route.asan) + tuple(
            ("ubsan", item) for item in route.ubsan
        )
        llvm_runtime = ubsan.path if ubsan else None
        llvm_identity = dict(ubsan.identity) if ubsan else {}
        asan_runtimes = tuple(
            AsanRuntime(architecture, tool.path, dict(tool.identity))
            for architecture, tool in asan_tools
        )

        discoveries = [
            analysis_discovery_slice(
                self.repository, self.toolchain, jobs=self.jobs, architectures=architectures
            ),
            native_dependency_discovery_slice(
                self.repository, self.toolchain, jobs=self.jobs, architectures=architectures,
                configurations=("Debug", "Release"),
                include_leak_probe=route.run_x64_specialists,
            ),
        ]
        if route.coverage:
            discoveries.append(coverage_dependency_discovery_slice(
                self.repository, self.toolchain, jobs=self.jobs,
                architectures=route.coverage,
            ))
        if selections:
            discoveries.append(sanitizer_dependency_discovery_slice(
                self.repository, self.toolchain, llvm_runtime=llvm_runtime,
                llvm_runtime_identity=llvm_identity, selections=selections, jobs=self.jobs,
            ))
        if route.run_x64_specialists:
            discoveries.append(fuzz_dependency_discovery_slice(
                self.repository, self.toolchain, jobs=self.jobs, targets=FUZZ_TARGETS,
            ))
        discovery = merge_graphs(*discoveries)
        await self._run(discovery)
        manifests = load_dependency_manifests(self.repository, discovery)

        native = native_graph(
            self.repository, self.toolchain, discovery=discovery, manifests=manifests,
            jobs=self.jobs, architectures=architectures,
            configurations=("Debug", "Release"), runnable_architectures=route.runnable,
            test_shards=test_shards, include_leak_probe=route.run_x64_specialists,
            corpus=corpus, run_nonce=run_nonce,
        )
        audit = audit_graph(
            self.repository, native, self._binaries(native, architectures),
            dumpbin=dumpbin.path, dumpbin_identity=dict(dumpbin.identity),
            binskim=binskim.path, binskim_identity=dict(binskim.identity),
            jobs=self.jobs, binskim_jobs=1,
        )
        package = package_graph(
            self.repository, audit, self._packages(audit, architectures),
            smoke_tests=self._smokes(audit, route.runnable), jobs=self.jobs,
        )
        graphs = [
            native,
            analysis_slice(
                self.repository, self.toolchain, discovery=discovery, manifests=manifests,
                jobs=self.jobs, architectures=architectures,
            ),
            source_checks_graph(
                self.repository, source_tools, jobs=self.jobs, architectures=architectures,
            ),
            python_coverage_graph(self.repository),
            package,
        ]
        if route.coverage:
            graphs.append(coverage_graph(
                self.repository, self.toolchain, discovery=discovery, manifests=manifests,
                jobs=self.jobs, architectures=route.coverage, test_shards=test_shards,
                corpus=corpus, run_nonce=run_nonce,
            ))
        if selections:
            graphs.append(sanitizer_graph(
                self.repository, self.toolchain, discovery=discovery, manifests=manifests,
                llvm_runtime=llvm_runtime, llvm_runtime_identity=llvm_identity,
                asan_runtimes=asan_runtimes, selections=selections,
                jobs=self.jobs, test_shards=test_shards,
            ))
        if route.run_x64_specialists:
            assert umdh is not None
            graphs.extend((
                fuzz_graph(
                    self.repository, self.toolchain, discovery=discovery,
                    manifests=manifests, run_nonce=run_nonce, seconds=fuzz_seconds,
                    jobs=self.jobs, targets=FUZZ_TARGETS,
                ),
                leak_graph(
                    self.repository, audit,
                    self._binaries(audit, ("x64",), include_leak_probe=True),
                    umdh=umdh.path, umdh_identity=dict(umdh.identity),
                    run_nonce=run_nonce, warmup=warmup, iterations=iterations,
                    windows=windows, tolerance_bytes=tolerance_bytes, jobs=self.jobs,
                ),
            ))
        return await self._run(merge_graphs(*graphs))

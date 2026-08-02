from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.graph import Command, Graph, GraphError, Node, Result  # noqa: E402
from core.quality_tools import ResolvedDirectory, ResolvedTool  # noqa: E402
import driver  # noqa: E402


MODULES = ("renpy", "rpgmaker", "zanzarah")


def node(
    name: str, *, inputs: tuple[str, ...] = (), results: tuple[Result, ...] = ()
) -> Node:
    return Node(
        name,
        hashlib.md5(name.encode(), usedforsecurity=False).hexdigest(),
        "slot",
        Command(("true",)),
        inputs,
        results,
    )


def discovery_graph(architectures: tuple[str, ...]) -> Graph:
    restores = tuple(node(f"restore-vcpkg-{architecture}") for architecture in architectures)
    discovered = tuple(
        node(f"discover-{architecture}", inputs=(restore.name,))
        for architecture, restore in zip(architectures, restores, strict=True)
    )
    return Graph(restores + discovered, tuple(item.name for item in discovered), {"restore": 1, "slot": 4})


def native_result(_repository: Path, _toolchain: object, **options: object) -> Graph:
    discovery = options["discovery"]
    architectures = options["architectures"]
    configurations = options["configurations"]
    builds = tuple(
        node(f"build-{project}-{architecture}-{configuration.lower()}")
        for architecture in architectures
        for configuration in configurations
        for project in (*MODULES, "tests")
    )
    if options["include_leak_probe"]:
        builds += (node("build-leak-probe-x64-release"),)
    return Graph(discovery.nodes + builds, tuple(item.name for item in builds), {"restore": 1, "slot": 4})


class FakeRuntime:
    def __init__(self, repository: Path, run_id: str) -> None:
        self.repository = repository
        self.run_id = run_id
        self.executed: list[Graph] = []
        self.store = SimpleNamespace(
            paths_for=lambda current: SimpleNamespace(
                output=repository / "out/cas" / current.name / "out"
            )
        )

    def executor(self, graph: Graph) -> FakeRuntime:
        self.executed.append(graph)
        return self

    async def run(self) -> None:
        return None


class DriverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name) / "repo"
        self.repository.mkdir()
        self.toolchain = SimpleNamespace(identity=(), environment=())
        self.runtime_patch = mock.patch.object(driver, "BuildRuntime", FakeRuntime)
        self.runtime_patch.start()

    def tearDown(self) -> None:
        self.runtime_patch.stop()
        self.temporary.cleanup()

    def session(self, jobs: int | None = 4) -> driver.Driver:
        return driver.Driver(self.repository, "run-1", self.toolchain, jobs=jobs)

    def tool(self, name: str) -> ResolvedTool:
        path = self.repository / f"tools/{name}.exe"
        return ResolvedTool(path, (("name", name),))

    @mock.patch.object(driver.psutil, "cpu_count", return_value=None)
    def test_session_owns_one_canonical_repository_runtime_and_validates_jobs(
        self, cpu_count: mock.Mock
    ) -> None:
        session = self.session(None)
        self.assertEqual(session.repository, self.repository.resolve())
        self.assertEqual(session.jobs, 1)
        self.assertEqual(session.runtime.run_id, "run-1")
        cpu_count.assert_called_once_with()
        with self.assertRaisesRegex(ValueError, "positive"):
            self.session(0)
        with self.assertRaisesRegex(ValueError, "positive"):
            self.session(True)

    async def test_restore_build_test_source_analysis_and_fuzz_use_shared_staging(self) -> None:
        session = self.session()
        source_tools = object()
        corpus = self.repository / "corpus"
        corpus.mkdir()

        def restore_node(_repository: Path, _toolchain: object, _factory: object,
                         architecture: str, *, flavor: str = "") -> Node:
            suffix = f"-{flavor}" if flavor else ""
            name = f"restore-vcpkg{suffix}-{architecture}"
            return Node(
                name,
                hashlib.md5(f"{flavor}-{architecture}".encode(), usedforsecurity=False).hexdigest(),
                "restore", Command(("true",)), (),
            )

        with (
            mock.patch.object(driver, "restore_node", side_effect=restore_node) as restore,
            mock.patch.object(
                driver, "native_dependency_discovery_slice",
                side_effect=lambda _repository, _toolchain, **options: discovery_graph(options["architectures"]),
            ) as native_discovery,
            mock.patch.object(driver, "native_graph", side_effect=native_result) as native,
            mock.patch.object(driver, "load_dependency_manifests", return_value={"unit": b"{}"}) as manifests,
            mock.patch.object(
                driver, "source_checks_graph", return_value=Graph((node("source"),), ("source",), {"slot": 4})
            ) as source,
            mock.patch.object(
                driver, "analysis_discovery_slice", return_value=discovery_graph(("x64",))
            ) as analysis_discovery,
            mock.patch.object(
                driver, "analysis_slice", return_value=Graph((node("analysis"),), ("analysis",), {"slot": 4})
            ) as analysis,
            mock.patch.object(
                driver, "fuzz_dependency_discovery_slice", return_value=discovery_graph(("x64",))
            ) as fuzz_discovery,
            mock.patch.object(
                driver, "fuzz_graph", return_value=Graph((node("fuzz"),), ("fuzz",), {"slot": 2})
            ) as fuzz,
            mock.patch.object(driver, "require_runnable", side_effect=lambda value: value) as require,
        ):
            await session.restore(("x64",), ("", "asan"))
            await session.build(("x64",), ("Debug", "Release"))
            await session.test(("x64",), ("Debug",), test_shards=7, corpus=corpus, run_nonce="nonce")
            await session.source_checks(("x64",), source_tools)
            await session.compiler_analysis(("x64",))
            await session.fuzz(
                run_nonce="fuzz-run", seconds=9, fuzz_jobs=3,
                targets=("pickle", "renpy"),
            )

        self.assertEqual(restore.call_count, 2)
        self.assertEqual(
            session.runtime.executed[0].targets,
            ("restore-vcpkg-x64", "restore-vcpkg-asan-x64"),
        )
        self.assertEqual(native_discovery.call_count, 2)
        self.assertEqual(manifests.call_count, 4)
        self.assertEqual(native.call_args_list[0].kwargs["runnable_architectures"], ())
        self.assertFalse(native.call_args_list[0].kwargs["include_leak_probe"])
        self.assertEqual(native.call_args_list[1].kwargs["runnable_architectures"], ("x64",))
        self.assertEqual(native.call_args_list[1].kwargs["test_shards"], 7)
        self.assertEqual(native.call_args_list[1].kwargs["corpus"], corpus)
        self.assertEqual(native.call_args_list[1].kwargs["run_nonce"], "nonce")
        source.assert_called_once_with(self.repository, source_tools, jobs=4, architectures=("x64",))
        analysis_discovery.assert_called_once()
        self.assertEqual(analysis.call_args.kwargs["manifests"], {"unit": b"{}"})
        fuzz_discovery.assert_called_once()
        self.assertEqual(fuzz.call_args.kwargs["run_nonce"], "fuzz-run")
        self.assertEqual(fuzz.call_args.kwargs["seconds"], 9)
        self.assertEqual(fuzz.call_args.kwargs["fuzz_jobs"], 3)
        self.assertEqual(fuzz_discovery.call_args.kwargs["targets"], ("pickle", "renpy"))
        self.assertEqual(fuzz.call_args.kwargs["targets"], ("pickle", "renpy"))
        self.assertEqual(require.call_args_list, [mock.call(("x64",)), mock.call(("x64",))])

    async def test_coverage_sanitizers_and_python_coverage_use_staged_graph_apis(self) -> None:
        session = self.session()
        corpus = self.repository / "corpus"
        corpus.mkdir()
        asan_tools = tuple((architecture, self.tool(f"asan-{architecture}"))
                           for architecture in ("x86", "x64"))
        ubsan_file = self.tool("ubsan")
        ubsan = ResolvedDirectory(self.repository / "ubsan", (ubsan_file,))

        with (
            mock.patch.object(driver, "coverage_dependency_discovery_slice",
                              return_value=discovery_graph(("x64",))) as coverage_discovery,
            mock.patch.object(driver, "coverage_graph",
                              return_value=Graph((node("coverage"),), ("coverage",), {"slot": 4})) as coverage,
            mock.patch.object(driver, "sanitizer_dependency_discovery_slice",
                              side_effect=lambda *_args, **_kwargs: discovery_graph(("x64",))) as sanitizer_discovery,
            mock.patch.object(driver, "sanitizer_graph",
                              side_effect=lambda *_args, **kwargs: Graph(
                                  (node(kwargs["selections"][0][0]),),
                                  (kwargs["selections"][0][0],), {"slot": 4}
                              )) as sanitizer,
            mock.patch.object(driver, "python_coverage_graph",
                              return_value=Graph((node("python"),), ("python",), {"slot": 1})) as python_graph,
            mock.patch.object(driver, "resolve_asan_runtimes", return_value=asan_tools) as resolve_asan,
            mock.patch.object(driver, "resolve_ubsan_runtime", return_value=ubsan) as resolve_ubsan,
            mock.patch.object(driver, "load_dependency_manifests", return_value={"tu": b"{}"}) as manifests,
            mock.patch.object(driver, "require_runnable", side_effect=lambda value: value) as require,
        ):
            await session.test_coverage(
                ("x64",), test_shards=7, report_jobs=3,
                corpus=corpus, run_nonce="coverage-run",
            )
            await session.test_asan(("x86", "x64"), test_shards=5)
            await session.test_ubsan(("x64",), test_shards=6)
            await session.python_coverage()

        coverage_discovery.assert_called_once_with(
            self.repository, self.toolchain, jobs=4, architectures=("x64",)
        )
        self.assertEqual(coverage.call_args.kwargs["manifests"], {"tu": b"{}"})
        self.assertEqual(coverage.call_args.kwargs["test_shards"], 7)
        self.assertEqual(coverage.call_args.kwargs["report_jobs"], 3)
        self.assertEqual(coverage.call_args.kwargs["corpus"], corpus)
        self.assertEqual(coverage.call_args.kwargs["run_nonce"], "coverage-run")
        self.assertEqual(sanitizer_discovery.call_count, 2)
        self.assertEqual(sanitizer.call_count, 2)
        asan_call, ubsan_call = sanitizer.call_args_list
        self.assertEqual(asan_call.kwargs["selections"], (("asan", "x86"), ("asan", "x64")))
        self.assertEqual(tuple(item.architecture for item in asan_call.kwargs["asan_runtimes"]),
                         ("x86", "x64"))
        self.assertIsNone(asan_call.kwargs["llvm_runtime"])
        self.assertEqual(ubsan_call.kwargs["selections"], (("ubsan", "x64"),))
        self.assertEqual(ubsan_call.kwargs["llvm_runtime"], ubsan.path)
        self.assertEqual(ubsan_call.kwargs["llvm_runtime_identity"], dict(ubsan.identity))
        resolve_asan.assert_called_once_with(self.toolchain, ("x86", "x64"))
        resolve_ubsan.assert_called_once_with(self.toolchain)
        self.assertEqual(manifests.call_count, 3)
        self.assertEqual(require.call_args_list,
                         [mock.call(("x64",)), mock.call(("x86", "x64")), mock.call(("x64",))])
        python_graph.assert_called_once_with(self.repository)

    async def test_quality_commands_reject_invalid_contracts_before_discovery(self) -> None:
        session = self.session()
        corpus = self.repository / "corpus"
        corpus.mkdir()
        with (
            mock.patch.object(driver, "coverage_dependency_discovery_slice") as coverage_discovery,
            mock.patch.object(driver, "sanitizer_dependency_discovery_slice") as sanitizer_discovery,
        ):
            for invalid_nonce in ("", "bad\0nonce", 7):
                with self.subTest(run_nonce=invalid_nonce), self.assertRaisesRegex(
                    ValueError, "corpus run nonce"
                ):
                    await session.test_coverage(corpus=corpus, run_nonce=invalid_nonce)
            with mock.patch.object(
                driver, "require_runnable", side_effect=RuntimeError("cannot run")
            ), self.assertRaisesRegex(RuntimeError, "cannot run"):
                await session.test_coverage(("x64",))
            with self.assertRaisesRegex(ValueError, "ASan does not support arm64"):
                await session.test_asan(("arm64",))
            with self.assertRaisesRegex(ValueError, "UBSan does not support x86"):
                await session.test_ubsan(("x86",))
            with self.assertRaisesRegex(ValueError, "exactly one architecture"):
                await session.verify_arch(("x86", "x64"), run_nonce="verify-run")
        coverage_discovery.assert_not_called()
        sanitizer_discovery.assert_not_called()

    async def test_release_audit_package_and_leaks_pass_only_canonical_axes(self) -> None:
        session = self.session()
        dumpbin, binskim, umdh = (self.tool(name) for name in ("dumpbin", "binskim", "umdh"))

        def native_discovery(_repository: Path, _toolchain: object, **options: object) -> Graph:
            return discovery_graph(options["architectures"])

        def audit_result(_repository: Path, upstream: Graph, **options: object) -> Graph:
            modules = (("leak-probe",) if options["include_leak_probe"] else ()) + MODULES
            gates = tuple(
                node(
                    f"audit-pe-{architecture}-{module}",
                    inputs=(f"build-{module}-{architecture}-release",),
                )
                for architecture in options["architectures"]
                for module in modules
            )
            return Graph(upstream.nodes + gates, tuple(item.name for item in gates), upstream.pools | {"audit": 4})

        package_result = Graph((node("package-manifest"),), ("package-manifest",), {"slot": 4})
        leak_result = Graph((node("leak"),), ("leak",), {"slot": 4})
        expected_packages = (self.repository / "one.zip",)
        with (
            mock.patch.object(driver, "native_dependency_discovery_slice", side_effect=native_discovery),
            mock.patch.object(driver, "native_graph", side_effect=native_result) as native,
            mock.patch.object(driver, "load_dependency_manifests", return_value={}),
            mock.patch.object(driver, "audit_graph", side_effect=audit_result) as audit,
            mock.patch.object(driver, "package_graph", return_value=package_result) as package,
            mock.patch.object(driver, "package_outputs", return_value=expected_packages) as outputs,
            mock.patch.object(driver, "leak_graph", return_value=leak_result) as leak,
            mock.patch.object(driver, "runnable_architectures", return_value=("x86", "x64")) as runnable,
            mock.patch.object(driver, "require_runnable", return_value=("x64",)) as require,
        ):
            await session.audit(("x64",), dumpbin=dumpbin, binskim=binskim)
            result = await session.package(
                ("x86", "x64", "arm64"), dumpbin=dumpbin, binskim=binskim
            )
            await session.test_leaks(
                run_nonce="leak-run", dumpbin=dumpbin, binskim=binskim, umdh=umdh,
                warmup=2, iterations=3, windows=4, tolerance_bytes=5,
            )

        self.assertEqual(result, expected_packages)
        self.assertEqual(native.call_count, 3)
        self.assertTrue(all(call.kwargs["configurations"] == ("Release",) for call in native.call_args_list))
        self.assertEqual([call.kwargs["include_leak_probe"] for call in native.call_args_list], [False, False, True])
        self.assertTrue(all(call.kwargs["runnable_architectures"] == () for call in native.call_args_list))
        self.assertEqual(audit.call_count, 3)
        self.assertTrue(all(len(call.args) == 2 for call in audit.call_args_list))
        self.assertEqual(
            [call.kwargs["architectures"] for call in audit.call_args_list],
            [("x64",), ("x86", "x64", "arm64"), ("x64",)],
        )
        self.assertEqual(
            [call.kwargs["include_leak_probe"] for call in audit.call_args_list],
            [False, False, True],
        )
        self.assertEqual(audit.call_args_list[0].kwargs["dumpbin_identity"], {"name": "dumpbin"})
        self.assertEqual(audit.call_args_list[0].kwargs["binskim_identity"], {"name": "binskim"})
        self.assertTrue(all(call.kwargs["jobs"] == 4 for call in audit.call_args_list))
        self.assertTrue(all("binskim_jobs" not in call.kwargs for call in audit.call_args_list))
        self.assertEqual(package.call_args.kwargs["architectures"], ("x86", "x64", "arm64"))
        self.assertEqual(package.call_args.kwargs["smoke_architectures"], ("x86", "x64"))
        runnable.assert_called_once_with(("x86", "x64", "arm64"))
        outputs.assert_called_once_with(self.repository, package_result)
        self.assertEqual(len(leak.call_args.args), 2)
        self.assertEqual(leak.call_args.kwargs["umdh"], umdh.path)
        self.assertEqual(leak.call_args.kwargs["umdh_identity"], {"name": "umdh"})
        self.assertEqual(leak.call_args.kwargs["run_nonce"], "leak-run")
        self.assertEqual(leak.call_args.kwargs["warmup"], 2)
        self.assertEqual(leak.call_args.kwargs["iterations"], 3)
        self.assertEqual(leak.call_args.kwargs["windows"], 4)
        self.assertEqual(leak.call_args.kwargs["tolerance_bytes"], 5)
        self.assertEqual(leak.call_args.kwargs["jobs"], 4)
        self.assertNotIn("session_jobs", leak.call_args.kwargs)
        self.assertNotIn("diff_jobs", leak.call_args.kwargs)
        require.assert_called_once_with(("x64",))

    async def test_verify_runs_one_discovery_union_then_one_parallel_family_union(self) -> None:
        session = self.session()
        corpus = self.repository / "corpus"
        corpus.mkdir()
        dumpbin, binskim, umdh = (self.tool(name) for name in ("dumpbin", "binskim", "umdh"))
        ubsan = ResolvedDirectory(self.repository / "ubsan", (self.tool("ubsan"),))
        route = SimpleNamespace(
            runnable=("x86", "x64"), coverage=("x64",), asan=("x86", "x64"),
            ubsan=("x64",), run_x64_specialists=True,
        )

        def discovered(_repository: Path, _toolchain: object, **options: object) -> Graph:
            architectures = options.get("architectures", ("x64",))
            return discovery_graph(architectures)

        representative_results = {
            "native": Result(
                "reports/tests/x64/debug/unit/shard-0.xml",
                "test", "application/xml", "tests.xml",
            ),
            "analysis": Result(
                "reports/sarif/x64/analysis.sarif",
                "report", "application/sarif+json", "analysis.sarif",
            ),
            "source": Result(
                "reports/sarif/source/analysis.sarif",
                "report", "application/sarif+json", "analysis.sarif",
            ),
            "cppcheck": Result(
                "reports/sarif/x86-x64-arm64/cppcheck.sarif",
                "report", "application/sarif+json", "analysis.sarif",
            ),
            "python": Result(
                "reports/coverage/python/coverage.json",
                "coverage", "application/json", "coverage.json",
            ),
            "coverage": Result(
                "reports/coverage/cpp/x64/coverage.json",
                "coverage", "application/json", "coverage.json",
            ),
            "sanitizer": Result(
                "reports/sanitizers/asan/x64/shard-0.xml",
                "test", "application/xml", "tests.xml",
            ),
            "fuzz": Result(
                "reports/fuzz/x64/pickle/status.txt",
                "fuzz", "text/plain", "status.txt",
            ),
            "audit": Result(
                "reports/sarif/x64/binskim-renpy.sarif",
                "report", "application/sarif+json", "binskim.sarif",
            ),
            "package": Result(
                "packages/x64/renpy-x64-dll.zip",
                "package", "application/zip", "renpy-x64-dll.zip",
            ),
            "leak": Result(
                "reports/leak/x64/operations/small-success/summary.json",
                "leak-summary", "application/json", "summary.json",
            ),
        }

        def family(name: str, upstream: Graph | None = None) -> Graph:
            pools = dict(upstream.pools) if upstream else {"slot": 4}
            nodes = upstream.nodes if upstream else ()
            marker = node(name, results=(representative_results[name],))
            return Graph(nodes + (marker,), (marker.name,), pools)

        def native_with_result(
            repository: Path, toolchain: object, **options: object
        ) -> Graph:
            graph = native_result(repository, toolchain, **options)
            marker = node("native-results", results=(representative_results["native"],))
            return Graph(graph.nodes + (marker,), graph.targets + (marker.name,), graph.pools)

        source_tools = mock.Mock(return_value="source-tools")
        asan = mock.Mock(return_value=(
            ("x86", self.tool("asan-x86")), ("x64", self.tool("asan-x64")),
        ))
        resolve_ubsan = mock.Mock(return_value=ubsan)
        native = mock.Mock(side_effect=native_with_result)
        common_source = mock.Mock(return_value=family("source"))
        architecture_source = mock.Mock(return_value=family("cppcheck"))
        coverage = mock.Mock(return_value=family("coverage"))
        sanitizer = mock.Mock(return_value=family("sanitizer"))
        fuzz = mock.Mock(return_value=family("fuzz"))
        package = mock.Mock(
            side_effect=lambda _repo, upstream, *_args, **_kwargs: family("package", upstream)
        )
        leak = mock.Mock(
            side_effect=lambda _repo, upstream, *_args, **_kwargs: family("leak", upstream)
        )
        with mock.patch.multiple(
            driver,
            verify_route=mock.Mock(return_value=route),
            discover_source_tools=source_tools,
            resolve_dumpbin=mock.Mock(return_value=dumpbin),
            resolve_binskim=mock.Mock(return_value=binskim),
            resolve_umdh=mock.Mock(return_value=umdh),
            resolve_asan_runtimes=asan,
            resolve_ubsan_runtime=resolve_ubsan,
            analysis_discovery_slice=mock.Mock(side_effect=discovered),
            native_dependency_discovery_slice=mock.Mock(side_effect=discovered),
            coverage_dependency_discovery_slice=mock.Mock(side_effect=discovered),
            sanitizer_dependency_discovery_slice=mock.Mock(side_effect=discovered),
            fuzz_dependency_discovery_slice=mock.Mock(side_effect=discovered),
            load_dependency_manifests=mock.Mock(return_value={"tu": b"{}"}),
            native_graph=native,
            analysis_slice=mock.Mock(return_value=family("analysis")),
            common_source_checks=common_source,
            architecture_source_checks=architecture_source,
            python_coverage_graph=mock.Mock(return_value=family("python")),
            coverage_graph=coverage,
            sanitizer_graph=sanitizer,
            fuzz_graph=fuzz,
            audit_graph=mock.Mock(
                side_effect=lambda _repo, upstream, *_args, **_kwargs: family("audit", upstream)
            ),
            package_graph=package,
            leak_graph=leak,
        ):
            await session.verify(
                ("x86", "x64", "arm64"), corpus=corpus, run_nonce="verify-run",
                fuzz_seconds=7, test_shards=5, warmup=2, iterations=3,
                windows=4, tolerance_bytes=6,
            )

        self.assertEqual(len(session.runtime.executed), 2)
        final = session.runtime.executed[-1]
        self.assertTrue({"analysis", "source", "cppcheck", "python", "coverage", "sanitizer", "fuzz",
                         "package", "leak"}.issubset(final.targets))
        self.assertEqual(
            {result.id for current in final.nodes for result in current.results},
            {result.id for result in representative_results.values()},
        )
        for result in representative_results.values():
            self.assertEqual(final.result(result.id)[1], result)
        self.assertEqual(native.call_args.kwargs["configurations"], ("Debug", "Release"))
        self.assertEqual(native.call_args.kwargs["runnable_architectures"], ("x86", "x64"))
        self.assertTrue(native.call_args.kwargs["include_leak_probe"])
        self.assertEqual(coverage.call_args.kwargs["corpus"], corpus)
        self.assertEqual(coverage.call_args.kwargs["run_nonce"], "verify-run")
        self.assertEqual(sanitizer.call_args.kwargs["selections"], (
            ("asan", "x86"), ("asan", "x64"), ("ubsan", "x64"),
        ))
        self.assertEqual(fuzz.call_args.kwargs["seconds"], 7)
        self.assertEqual(leak.call_args.kwargs["tolerance_bytes"], 6)
        self.assertEqual(
            package.call_args.kwargs["smoke_architectures"],
            ("x86", "x64"),
        )
        source_tools.assert_called_once_with(self.toolchain)
        common_source.assert_called_once()
        architecture_source.assert_called_once()
        asan.assert_called_once_with(self.toolchain, ("x86", "x64"))
        resolve_ubsan.assert_called_once_with(self.toolchain)

    async def test_verify_source_rejects_duplicate_result_ids_across_families(self) -> None:
        session = self.session()
        duplicate = Result(
            "reports/sarif/source/analysis.sarif",
            "report", "application/sarif+json", "analysis.sarif",
        )
        common = Graph(
            (node("source", results=(duplicate,)),), ("source",), {"slot": 4}
        )
        python = Graph(
            (node("python", results=(duplicate,)),), ("python",), {"slot": 4}
        )

        with (
            mock.patch.object(driver, "discover_source_tools", return_value="tools"),
            mock.patch.object(driver, "common_source_checks", return_value=common),
            mock.patch.object(driver, "python_coverage_graph", return_value=python),
            self.assertRaisesRegex(
                GraphError,
                "duplicate result id: reports/sarif/source/analysis[.]sarif",
            ),
        ):
            await session.verify_source()

        self.assertEqual(session.runtime.executed, [])

    async def test_verify_source_runs_only_common_source_and_python_coverage(self) -> None:
        session = self.session()
        common = Graph((node("source"),), ("source",), {"slot": 4})
        python = Graph((node("python"),), ("python",), {"slot": 4})
        with (
            mock.patch.object(driver, "discover_source_tools", return_value="tools") as tools,
            mock.patch.object(driver, "common_source_checks", return_value=common) as source,
            mock.patch.object(driver, "python_coverage_graph", return_value=python) as coverage,
            mock.patch.object(driver, "export_results") as export,
        ):
            await session.verify_source(export_dir=self.repository / "evidence")

        self.assertEqual(len(session.runtime.executed), 1)
        self.assertEqual(set(session.runtime.executed[0].targets), {"source", "python"})
        tools.assert_called_once_with(self.toolchain)
        source.assert_called_once_with(self.repository, "tools", jobs=4)
        coverage.assert_called_once_with(self.repository)
        export.assert_called_once_with(
            session.runtime.executed[0], session.runtime.store,
            self.repository / "evidence", "verify-source", "success", failures=(),
        )

    async def test_failed_public_command_exports_structured_failures_before_reraising(self) -> None:
        session = self.session()
        common = Graph((node("source"),), ("source",), {"slot": 4})
        python = Graph((node("python"),), ("python",), {"slot": 4})
        failure = ExceptionGroup("failed", (RuntimeError("expected"),))
        failure.failed_nodes = ("source",)

        async def fail() -> None:
            raise failure

        with (
            mock.patch.object(driver, "discover_source_tools", return_value="tools"),
            mock.patch.object(driver, "common_source_checks", return_value=common),
            mock.patch.object(driver, "python_coverage_graph", return_value=python),
            mock.patch.object(session.runtime, "run", side_effect=fail),
            mock.patch.object(driver, "export_results") as export,
            self.assertRaises(ExceptionGroup) as raised,
        ):
            await session.verify_source(export_dir=self.repository / "failed-evidence")

        self.assertIs(raised.exception, failure)
        graph = session.runtime.executed[0]
        export.assert_called_once_with(
            graph, session.runtime.store, self.repository / "failed-evidence",
            "verify-source", "failed", failures=("source",),
        )

    async def test_export_failure_preserves_the_original_execution_failure(self) -> None:
        session = self.session()
        graph = Graph((node("source"),), ("source",), {"slot": 4})
        execution = ExceptionGroup("failed", (RuntimeError("execution"),))
        execution.failed_nodes = ("source",)

        async def fail() -> None:
            raise execution

        with (
            mock.patch.object(driver, "discover_source_tools", return_value="tools"),
            mock.patch.object(driver, "common_source_checks", return_value=graph),
            mock.patch.object(
                driver, "python_coverage_graph",
                return_value=Graph((node("python"),), ("python",), {"slot": 4}),
            ),
            mock.patch.object(session.runtime, "run", side_effect=fail),
            mock.patch.object(driver, "export_results", side_effect=RuntimeError("export")),
            self.assertRaises(ExceptionGroup) as raised,
        ):
            await session.verify_source(export_dir=self.repository / "failed-evidence")

        self.assertIs(raised.exception.exceptions[0], execution)
        self.assertRegex(str(raised.exception.exceptions[1]), "export")

    async def test_failure_before_graph_composition_does_not_publish_empty_evidence(self) -> None:
        session = self.session()
        with (
            mock.patch.object(
                driver, "discover_source_tools", side_effect=RuntimeError("discovery")
            ),
            mock.patch.object(driver, "export_results") as export,
            self.assertRaisesRegex(RuntimeError, "discovery"),
        ):
            await session.verify_source(export_dir=self.repository / "evidence")
        export.assert_not_called()

    async def test_verify_arch_omits_common_and_nonrunnable_specialists(self) -> None:
        session = self.session()
        route = SimpleNamespace(
            runnable=(), coverage=(), asan=(), ubsan=(), run_x64_specialists=False,
        )
        discovery = discovery_graph(("arm64",))

        def family(name: str, upstream: Graph | None = None) -> Graph:
            pools = dict(upstream.pools) if upstream else {"slot": 4}
            nodes = upstream.nodes if upstream else ()
            marker = node(name)
            return Graph(nodes + (marker,), (marker.name,), pools)

        unused = {
            name: mock.Mock() for name in (
                "resolve_umdh", "resolve_asan_runtimes", "resolve_ubsan_runtime",
                "coverage_dependency_discovery_slice", "sanitizer_dependency_discovery_slice",
                "fuzz_dependency_discovery_slice", "coverage_graph", "sanitizer_graph",
                "fuzz_graph", "leak_graph", "common_source_checks", "python_coverage_graph",
            )
        }
        native = mock.Mock(side_effect=native_result)
        with mock.patch.multiple(
            driver,
            verify_route=mock.Mock(return_value=route),
            discover_source_tools=mock.Mock(return_value="source-tools"),
            resolve_dumpbin=mock.Mock(return_value=self.tool("dumpbin")),
            resolve_binskim=mock.Mock(return_value=self.tool("binskim")),
            analysis_discovery_slice=mock.Mock(return_value=discovery),
            native_dependency_discovery_slice=mock.Mock(return_value=discovery),
            load_dependency_manifests=mock.Mock(return_value={}),
            native_graph=native,
            analysis_slice=mock.Mock(return_value=family("analysis")),
            architecture_source_checks=mock.Mock(return_value=family("cppcheck")),
            audit_graph=mock.Mock(
                side_effect=lambda _repo, upstream, *_args, **_kwargs: family("audit", upstream)
            ),
            package_graph=mock.Mock(
                side_effect=lambda _repo, upstream, *_args, **_kwargs: family("package", upstream)
            ),
            **unused,
        ):
            await session.verify_arch(("arm64",), run_nonce="verify-run")

        self.assertEqual(len(session.runtime.executed), 2)
        self.assertTrue({"analysis", "cppcheck", "package"}.issubset(
            session.runtime.executed[-1].targets
        ))
        self.assertFalse(native.call_args.kwargs["include_leak_probe"])
        for current in unused.values():
            current.assert_not_called()


if __name__ == "__main__":
    unittest.main()

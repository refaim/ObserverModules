from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
import io
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.graph import Command, Graph, Node  # noqa: E402
from core.paths import BuildPaths  # noqa: E402
from core.sanitizer import SanitizerError, main as sanitizer_main, require_clean_log  # noqa: E402
from graphs.sanitizer import (  # noqa: E402
    AsanRuntime,
    sanitizer_artifact_graph,
    sanitizer_dependency_discovery_slice,
    sanitizer_graph,
)
from tests import test_instrumented_graph as instrumented_fixture  # noqa: E402


def node(name: str, *, inputs: tuple[str, ...] = ()) -> Node:
    return Node(
        name,
        hashlib.md5(name.encode(), usedforsecurity=False).hexdigest(),
        "build",
        Command(("C:/sdk/build.exe",)),
        inputs,
    )


class SanitizerGraphTests(unittest.TestCase):
    def fixture(
        self,
        root: Path,
        selections: tuple[tuple[str, str], ...] = (
            ("asan", "x86"), ("asan", "x64"), ("ubsan", "x64")
        ),
    ) -> tuple[Path, Graph, tuple[AsanRuntime, ...], Path]:
        repository = root / "repo"
        repository.mkdir()
        nodes = []
        for sanitizer, architecture in selections:
            restore_name = (
                f"restore-vcpkg-asan-{architecture}"
                if sanitizer == "asan"
                else f"restore-vcpkg-{architecture}"
            )
            restore = node(restore_name)
            discovery = node(
                f"discover-{sanitizer}-{architecture}", inputs=(restore.name,)
            )
            nodes.extend((restore, discovery))
            for name, filename in (
                ("renpy", "renpy.so"),
                ("rpgmaker", "rpgmaker.so"),
                ("zanzarah", "zanzarah.so"),
                ("tests", "tests.exe"),
            ):
                producer = node(
                    f"build-{name}-{architecture}-{sanitizer}",
                    inputs=(discovery.name,),
                )
                nodes.append(producer)
        upstream = Graph(
            tuple(nodes), tuple(current.name for current in nodes[1:]), {"build": 8}
        )
        tools = root / "tools"
        tools.mkdir()
        pwsh = tools / "pwsh.exe"
        pwsh.touch()
        runtimes = []
        for architecture, filename in (
            ("x86", "clang_rt.asan_dynamic-i386.dll"),
            ("x64", "clang_rt.asan_dynamic-x86_64.dll"),
        ):
            if ("asan", architecture) in selections:
                path = tools / filename
                path.touch()
                runtimes.append(
                    AsanRuntime(architecture, path, {"sha256": f"runtime-{architecture}"})
                )
        return repository, upstream, tuple(runtimes), pwsh

    def build(self, root: Path, **options: object) -> Graph:
        selections = options.pop(
            "selections", (("asan", "x86"), ("asan", "x64"), ("ubsan", "x64"))
        )
        repository, upstream, runtimes, pwsh = self.fixture(
            root, selections  # type: ignore[arg-type]
        )
        return sanitizer_artifact_graph(
            repository,
            upstream,
            selections=selections,  # type: ignore[arg-type]
            pwsh=pwsh,
            pwsh_identity={"version": options.pop("pwsh_version", "7.5")},
            asan_runtimes=runtimes,
            **options,
        )

    def test_asan_and_ubsan_build_adapters_create_independent_shard_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph = self.build(root, test_shards=2, jobs=6)
            paths = BuildPaths(root / "repo")

        self.assertEqual(
            graph.pools,
            {"build": 8, "sanitizer-shard": 6, "sanitizer-gate": 6},
        )
        self.assertEqual(
            graph.targets,
            (
                "asan-gate-x64-0", "asan-gate-x64-1",
                "asan-gate-x86-0", "asan-gate-x86-1",
                "ubsan-gate-x64-0", "ubsan-gate-x64-1",
            ),
        )
        self.assertEqual(len(graph.nodes), 30)
        for sanitizer, architecture in (
            ("asan", "x64"), ("asan", "x86"), ("ubsan", "x64")
        ):
            builds = tuple(
                graph.node(f"build-{name}-{architecture}-{sanitizer}")
                for name in ("renpy", "rpgmaker", "zanzarah", "tests")
            )
            for index in range(2):
                shard = graph.node(f"{sanitizer}-test-{architecture}-{index}")
                gate = graph.node(f"{sanitizer}-gate-{architecture}-{index}")
                self.assertEqual(shard.inputs, tuple(item.name for item in builds))
                self.assertEqual(gate.inputs, (shard.name,))
                self.assertEqual((shard.pool, gate.pool), ("sanitizer-shard", "sanitizer-gate"))
                self.assertEqual(
                    gate.command.argv[1:],
                    (
                        "-m", "core.sanitizer", "gate", sanitizer,
                        str(paths.cas(shard.uid).log),
                    ),
                )
                self.assertEqual(
                    tuple((item.id, item.kind, item.media_type, item.relative_path)
                          for item in shard.results),
                    ((
                        f"reports/sanitizers/{sanitizer}/{architecture}/shard-{index}.xml",
                        "sanitizer", "application/xml", "tests.xml",
                    ),),
                )
                self.assertEqual(gate.results, ())
                script = shard.command.stdin.decode("utf-8")
                self.assertIn("Invoke-Checked", script)
                self.assertIn("'--shard-count'", script)
                self.assertIn("'2'", script)
                self.assertIn("'--shard-index'", script)
                self.assertIn(f"'{index}'", script)
                for binary in ("renpy.so", "rpgmaker.so", "zanzarah.so", "tests.exe"):
                    self.assertIn(binary, script)
                if sanitizer == "asan":
                    self.assertIn("ASAN_OPTIONS", script)
                    self.assertIn("halt_on_error=1:alloc_dealloc_mismatch=1", script)
                    self.assertIn("clang_rt.asan_dynamic-", script)
                    self.assertNotIn("UBSAN_OPTIONS", script)
                else:
                    self.assertIn("UBSAN_OPTIONS", script)
                    self.assertIn("halt_on_error=1:print_stacktrace=1", script)
                    self.assertNotIn("clang_rt.asan_dynamic-", script)

    def test_runtime_and_pwsh_identities_have_narrow_invalidation_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, runtimes, pwsh = self.fixture(root)

            def build(
                pwsh_version: str = "7.5", runtime_x64: str = "runtime-x64"
            ) -> Graph:
                changed = tuple(
                    AsanRuntime(
                        item.architecture,
                        item.path,
                        {"sha256": runtime_x64 if item.architecture == "x64" else "runtime-x86"},
                    )
                    for item in runtimes
                )
                return sanitizer_artifact_graph(
                    repository,
                    upstream,
                    selections=(("asan", "x86"), ("asan", "x64"), ("ubsan", "x64")),
                    pwsh=pwsh,
                    pwsh_identity={"version": pwsh_version},
                    asan_runtimes=changed,
                    test_shards=2,
                )

            before = build()
            runtime_changed = build(runtime_x64="changed")
            pwsh_changed = build(pwsh_version="7.6")

        for current in before.nodes:
            if "-test-" not in current.name and "-gate-" not in current.name:
                continue
            runtime_partition = current.name.startswith("asan-") and "-x64-" in current.name
            self.assertEqual(
                runtime_partition,
                current.uid != runtime_changed.node(current.name).uid,
                current.name,
            )
            self.assertNotEqual(current.uid, pwsh_changed.node(current.name).uid, current.name)

    def test_rejects_invalid_selections_lineage_runtimes_tools_and_pools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, runtimes, pwsh = self.fixture(
                root, (("asan", "x64"),)
            )

            def invoke(
                *,
                source: Graph = upstream,
                selections: tuple[tuple[str, str], ...] = (("asan", "x64"),),
                selected_runtimes: tuple[AsanRuntime, ...] = runtimes,
                pwsh_path: Path = pwsh,
                **options: object,
            ) -> Graph:
                return sanitizer_artifact_graph(
                    repository,
                    source,
                    selections=selections,
                    pwsh=pwsh_path,
                    pwsh_identity={"version": "7.5"},
                    asan_runtimes=selected_runtimes,
                    **options,
                )

            for invalid in (
                (),
                (("asan", "x64"), ("asan", "x64")),
                (("msan", "x64"),),
                (("asan", "arm64"),),
                (("ubsan", "x86"),),
            ):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "selections"):
                    invoke(selections=invalid)

            missing = Graph(
                tuple(item for item in upstream.nodes if item.name != "build-tests-x64-asan"),
                tuple(name for name in upstream.targets if name != "build-tests-x64-asan"),
                upstream.pools,
            )
            with self.assertRaisesRegex(ValueError, "unknown node"):
                invoke(source=missing)

            shared = node("detached-shared")
            left = node("detached-left", inputs=(shared.name,))
            right = node("detached-right", inputs=(shared.name,))
            detached = node(
                "build-renpy-x64-asan", inputs=(left.name, right.name)
            )
            detached_source = Graph(
                tuple(
                    detached if current.name == detached.name else current
                    for current in upstream.nodes
                ) + (shared, left, right),
                upstream.targets,
                upstream.pools,
            )
            with self.assertRaisesRegex(ValueError, "restore ancestor"):
                invoke(source=detached_source)

            with self.assertRaisesRegex(ValueError, "runtime.*required"):
                invoke(selected_runtimes=())
            with self.assertRaisesRegex(ValueError, "duplicate.*runtime"):
                invoke(selected_runtimes=runtimes + runtimes)
            bad_runtime = AsanRuntime("arm64", runtimes[0].path, {})
            with self.assertRaisesRegex(ValueError, "runtime identity"):
                invoke(selected_runtimes=(bad_runtime,))
            bad_name = AsanRuntime("x64", pwsh, {})
            with self.assertRaisesRegex(ValueError, "runtime identity"):
                invoke(selected_runtimes=(bad_name,))
            directory_runtime = AsanRuntime("x64", pwsh.parent, {})
            with self.assertRaisesRegex(FileNotFoundError, "not a file"):
                invoke(selected_runtimes=(directory_runtime,))
            with self.assertRaisesRegex(FileNotFoundError, "not a file"):
                invoke(pwsh_path=pwsh.parent)
            for options in ({"test_shards": 0}, {"jobs": True}):
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, "positive integer"):
                    invoke(**options)
            matching = Graph(
                upstream.nodes, upstream.targets,
                dict(upstream.pools) | {"sanitizer-shard": 4, "sanitizer-gate": 4},
            )
            self.assertEqual(invoke(source=matching).pools["sanitizer-shard"], 4)
            conflict = Graph(
                upstream.nodes, upstream.targets,
                dict(upstream.pools) | {"sanitizer-shard": 1},
            )
            with self.assertRaisesRegex(ValueError, "conflicting pool"):
                invoke(source=conflict)

    def test_complete_graph_discovers_and_builds_all_sanitizer_artifacts_itself(self) -> None:
        helper = instrumented_fixture.InstrumentedBuildGraphTests()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = helper.repository(root / "repo")
            toolchain = helper.toolchain(root)
            llvm_runtime = helper.llvm_runtime(root)
            runtime_path = root / "clang_rt.asan_dynamic-x86_64.dll"
            runtime_path.touch()
            runtimes = (
                AsanRuntime("x64", runtime_path, {"sha256": "asan-x64"}),
            )
            selections = (("asan", "x64"), ("ubsan", "x64"))
            runtime_identity = {"standalone": "sha256:a", "cxx": "sha256:b"}
            discovery = sanitizer_dependency_discovery_slice(
                repository,
                toolchain,
                selections=selections,
                llvm_runtime=llvm_runtime,
                llvm_runtime_identity=runtime_identity,
                jobs=4,
            )
            graph = sanitizer_graph(
                repository,
                toolchain,
                discovery=discovery,
                manifests=helper.manifests(repository, discovery),
                selections=selections,
                llvm_runtime=llvm_runtime,
                llvm_runtime_identity=runtime_identity,
                asan_runtimes=runtimes,
                jobs=4,
                test_shards=1,
            )

        self.assertEqual(graph.targets, ("asan-gate-x64-0", "ubsan-gate-x64-0"))
        self.assertEqual(
            len(graph.nodes),
            len(discovery.nodes) + len(selections) * (4 + 1 + 1),
        )
        for sanitizer in ("asan", "ubsan"):
            shard = graph.node(f"{sanitizer}-test-x64-0")
            for project in ("renpy", "rpgmaker", "zanzarah", "tests"):
                build = graph.node(f"build-{project}-x64-{sanitizer}")
                self.assertIn(
                    f"'/p:Configuration={'ASan' if sanitizer == 'asan' else 'UBSan'}'",
                    build.command.stdin.decode(),
                )
                self.assertEqual(shard.inputs.count(build.name), 1)

    def test_sanitizer_runtime_is_test_only_and_release_remains_static_mt(self) -> None:
        root = ET.parse(BUILD_ROOT / "ObserverProject.props").getroot()
        namespace = "{http://schemas.microsoft.com/developer/msbuild/2003}"
        runtime_libraries = root.findall(f".//{namespace}RuntimeLibrary")
        release = next(
            item for item in runtime_libraries if "$(Configuration)' == 'Release" in item.get("Condition", "")
        )
        self.assertEqual(release.text, "MultiThreaded")


class SanitizerGateTests(unittest.TestCase):
    def test_clean_logs_pass_and_findings_fail_for_each_runtime(self) -> None:
        require_clean_log("asan", "All tests passed (42 assertions)\n")
        require_clean_log("ubsan", "All tests passed (42 assertions)\n")
        for sanitizer, finding in (
            ("asan", "ERROR: AddressSanitizer: heap-use-after-free"),
            ("asan", "AddressSanitizer:DEADLYSIGNAL"),
            ("asan", "SUMMARY: AddressSanitizer: double-free"),
            ("ubsan", "foo.cpp:3: runtime error: signed integer overflow"),
            ("ubsan", "UndefinedBehaviorSanitizer:DEADLYSIGNAL"),
            ("ubsan", "SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior"),
        ):
            with self.subTest(sanitizer=sanitizer, finding=finding), self.assertRaisesRegex(
                SanitizerError, "finding"
            ):
                require_clean_log(sanitizer, finding)
        with self.assertRaisesRegex(SanitizerError, "unsupported"):
            require_clean_log("msan", "clean")

    def test_cli_has_no_relaxation_switch_and_module_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "test.log"
            log.write_text("All tests passed\n", encoding="utf-8")
            self.assertEqual(sanitizer_main(("gate", "asan", str(log))), 0)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                sanitizer_main(("gate", "asan", str(log), "--allow-findings"))
            with (
                mock.patch.object(sys, "argv", ["sanitizer.py", "gate", "ubsan", str(log)]),
                self.assertWarnsRegex(RuntimeWarning, "core.sanitizer"),
                self.assertRaises(SystemExit) as raised,
            ):
                runpy.run_module("core.sanitizer", run_name="__main__")
            self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

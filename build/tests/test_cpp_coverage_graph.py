from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.cpp_coverage import CoverageError, main as coverage_main, require_full_coverage  # noqa: E402
from core.graph import Command, Graph, Node  # noqa: E402
from core.paths import BuildPaths  # noqa: E402
from graphs.coverage import (  # noqa: E402
    coverage_artifact_graph,
    coverage_dependency_discovery_slice,
    coverage_graph,
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


class CppCoverageGraphTests(unittest.TestCase):
    def fixture(
        self, root: Path, architectures: tuple[str, ...] = ("x64", "x86")
    ) -> tuple[Path, Graph, dict[str, Path]]:
        repository = root / "repo"
        repository.mkdir()
        nodes = []
        for architecture in architectures:
            restore = node(f"restore-vcpkg-{architecture}")
            discovery = node(f"coverage-dependencies-{architecture}", inputs=(restore.name,))
            nodes.extend((restore, discovery))
            for name, filename in (
                ("renpy", "renpy.so"),
                ("rpgmaker", "rpgmaker.so"),
                ("zanzarah", "zanzarah.so"),
                ("tests", "tests.exe"),
            ):
                producer = node(
                    f"build-{name}-{architecture}-coverage", inputs=(discovery.name,)
                )
                nodes.append(producer)
        upstream = Graph(tuple(nodes), tuple(item.name for item in nodes[1:]), {"build": 8})
        tools = root / "tools"
        tools.mkdir()
        selected = {
            name: tools / name
            for name in ("pwsh.exe", "llvm-profdata.exe", "llvm-cov.exe")
        }
        for path in selected.values():
            path.touch()
        return repository, upstream, selected

    def graph(self, root: Path, **options: object) -> Graph:
        architectures = options.pop("architectures", ("x64", "x86"))
        repository, upstream, tools = self.fixture(root, architectures)  # type: ignore[arg-type]
        return coverage_artifact_graph(
            repository,
            upstream,
            architectures=architectures,  # type: ignore[arg-type]
            pwsh=tools["pwsh.exe"],
            pwsh_identity={"version": options.pop("pwsh_version", "7.5")},
            llvm_profdata=tools["llvm-profdata.exe"],
            llvm_profdata_identity={"version": options.pop("profdata_version", "20.1")},
            llvm_cov=tools["llvm-cov.exe"],
            llvm_cov_identity={"version": options.pop("cov_version", "20.1")},
            **options,
        )

    def test_build_adapter_shards_merge_parallel_reports_and_hard_gate_form_the_dag(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph = self.graph(root, test_shards=2, jobs=6, report_jobs=3)
            paths = BuildPaths(root / "repo")

        self.assertEqual(
            graph.pools,
            {
                "build": 8,
                "coverage-shard": 6,
                "coverage-merge": 2,
                "coverage-report": 3,
                "coverage-gate": 6,
            },
        )
        self.assertEqual(graph.targets, ("coverage-x64", "coverage-x86"))
        self.assertEqual(len(graph.nodes), 24)
        for architecture in ("x64", "x86"):
            builds = tuple(
                graph.node(f"build-{name}-{architecture}-coverage")
                for name in ("renpy", "rpgmaker", "zanzarah", "tests")
            )
            shards = tuple(
                graph.node(f"coverage-test-{architecture}-{index}") for index in range(2)
            )
            for index, shard in enumerate(shards):
                self.assertEqual(shard.inputs, tuple(item.name for item in builds))
                self.assertEqual(shard.pool, "coverage-shard")
                script = shard.command.stdin.decode("utf-8")
                self.assertIn("LLVM_PROFILE_FILE", script)
                self.assertIn("coverage-%m-%p.profraw", script)
                self.assertIn("'--shard-count'", script)
                self.assertIn("'2'", script)
                self.assertIn("'--shard-index'", script)
                self.assertIn(f"'{index}'", script)
                self.assertEqual(
                    tuple((item.id, item.relative_path) for item in shard.results),
                    ((
                        f"reports/coverage/cpp/{architecture}/tests/shard-{index}.xml",
                        "tests.xml",
                    ),),
                )

            merge = graph.node(f"coverage-merge-{architecture}")
            self.assertEqual(merge.inputs, tuple(item.name for item in shards))
            self.assertEqual(merge.pool, "coverage-merge")
            merge_script = merge.command.stdin.decode("utf-8")
            self.assertIn("llvm-profdata.exe", merge_script)
            self.assertIn("coverage.profdata", merge_script)
            self.assertIn("$arguments = @('merge', '-sparse') + $profiles", merge_script)
            self.assertIn("llvm-profdata.exe' $arguments", merge_script)
            for shard in shards:
                self.assertIn(str(paths.cas(shard.uid).output), merge_script)

            reports = tuple(
                graph.node(f"coverage-{kind}-{architecture}") for kind in ("json", "lcov")
            )
            for report in reports:
                self.assertEqual(report.inputs, (merge.name, *(item.name for item in builds)))
                self.assertEqual(report.pool, "coverage-report")
                script = report.command.stdin.decode("utf-8")
                self.assertIn("llvm-cov.exe", script)
                self.assertIn("--instr-profile", script)
                self.assertIn("--ignore-filename-regex", script)
                for module in ("renpy.so", "rpgmaker.so", "zanzarah.so"):
                    self.assertIn(module, script)
            self.assertEqual(
                tuple((item.id, item.kind, item.media_type, item.relative_path)
                      for item in reports[0].results),
                ((
                    f"reports/coverage/cpp/{architecture}/coverage.json",
                    "coverage", "application/json", "coverage.json",
                ),),
            )
            self.assertEqual(
                tuple((item.id, item.kind, item.media_type, item.relative_path)
                      for item in reports[1].results),
                ((
                    f"reports/coverage/cpp/{architecture}/coverage.lcov",
                    "coverage", "text/plain", "coverage.lcov",
                ),),
            )
            self.assertEqual(merge.results, ())
            self.assertIn("--summary-only", reports[0].command.stdin.decode("utf-8"))
            self.assertIn("--format=lcov", reports[1].command.stdin.decode("utf-8"))

            gate = graph.node(f"coverage-{architecture}")
            self.assertEqual(gate.inputs, tuple(item.name for item in reports))
            self.assertEqual(gate.pool, "coverage-gate")
            self.assertEqual(gate.command.argv[1:4], ("-m", "core.cpp_coverage", "gate"))
            self.assertNotIn("threshold", " ".join(gate.command.argv).casefold())
            self.assertEqual(gate.results, ())

    def test_tool_identities_invalidate_only_their_nodes_and_semantic_consumers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, tools = self.fixture(root, ("x64",))

            def build(profdata_version: str, cov_version: str) -> Graph:
                return coverage_artifact_graph(
                    repository,
                    upstream,
                    architectures=("x64",),
                    pwsh=tools["pwsh.exe"],
                    pwsh_identity={"version": "7.5"},
                    llvm_profdata=tools["llvm-profdata.exe"],
                    llvm_profdata_identity={"version": profdata_version},
                    llvm_cov=tools["llvm-cov.exe"],
                    llvm_cov_identity={"version": cov_version},
                    test_shards=2,
                )

            before, profdata, cov = build("20.1", "20.1"), build("20.2", "20.1"), build("20.1", "20.2")

        for current in before.nodes:
            if not current.name.startswith("coverage-") or current.name.startswith(
                "coverage-dependencies-"
            ):
                continue
            profdata_partition = not current.name.startswith("coverage-test-")
            cov_partition = current.name.startswith(("coverage-json-", "coverage-lcov-")) or current.name == "coverage-x64"
            self.assertEqual(profdata_partition, current.uid != profdata.node(current.name).uid, current.name)
            self.assertEqual(cov_partition, current.uid != cov.node(current.name).uid, current.name)

    def test_external_corpus_adds_independent_profile_shards_and_signs_only_path_and_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, tools = self.fixture(root, ("x64",))
            corpus = root / "corpus"
            moved_corpus = root / "moved-corpus"
            corpus.mkdir()
            moved_corpus.mkdir()

            def build(selected: Path | None = None, nonce: str = "") -> Graph:
                return coverage_artifact_graph(
                    repository,
                    upstream,
                    architectures=("x64",),
                    pwsh=tools["pwsh.exe"],
                    pwsh_identity={"version": "7.5"},
                    llvm_profdata=tools["llvm-profdata.exe"],
                    llvm_profdata_identity={"version": "20.1"},
                    llvm_cov=tools["llvm-cov.exe"],
                    llvm_cov_identity={"version": "20.1"},
                    test_shards=2,
                    corpus=selected,
                    run_nonce=nonce,
                )

            baseline = build()
            no_corpus = build(None, "ignored")
            first = build(corpus, "run-one")
            (corpus / "multi-gigabyte-placeholder.bin").write_bytes(b"not signed")
            content_changed = build(corpus, "run-one")
            rerun = build(corpus, "run-two")
            moved = build(moved_corpus, "run-one")
            cas_outputs = {
                name: BuildPaths(repository).cas(first.node(name).uid).output
                for name in (
                    *(f"coverage-test-x64-{index}" for index in range(2)),
                    *(f"coverage-corpus-x64-{index}" for index in range(2)),
                )
            }

        standard_names = tuple(f"coverage-test-x64-{index}" for index in range(2))
        corpus_names = tuple(f"coverage-corpus-x64-{index}" for index in range(2))
        for name in standard_names:
            self.assertEqual(baseline.node(name), no_corpus.node(name))
            self.assertEqual(baseline.node(name), first.node(name))
            self.assertNotIn("OBSERVER_TEST_CORPUS", dict(first.node(name).command.env))
        self.assertFalse(any(node.name.startswith("coverage-corpus-") for node in baseline.nodes))
        for index, name in enumerate(corpus_names):
            shard = first.node(name)
            self.assertEqual(shard.uid, content_changed.node(name).uid)
            self.assertNotEqual(shard.uid, rerun.node(name).uid)
            self.assertNotEqual(shard.uid, moved.node(name).uid)
            self.assertEqual(shard.inputs, first.node(standard_names[index]).inputs)
            self.assertEqual(
                dict(shard.command.env)["OBSERVER_TEST_CORPUS"], str(corpus.resolve())
            )
            script = shard.command.stdin.decode("utf-8")
            self.assertIn("'[compatibility]'", script)
            self.assertIn("LLVM_PROFILE_FILE", script)
            self.assertEqual(
                tuple((item.id, item.relative_path) for item in shard.results),
                ((
                    f"reports/coverage/cpp/x64/corpus/shard-{index}.xml",
                    "tests.xml",
                ),),
            )
        merge = first.node("coverage-merge-x64")
        self.assertEqual(merge.inputs, standard_names + corpus_names)
        for name in (*standard_names, *corpus_names):
            self.assertIn(str(cas_outputs[name]), merge.command.stdin.decode())
        self.assertTrue(all(
            "OBSERVER_TEST_CORPUS" not in dict(node.command.env)
            for node in first.nodes if not node.name.startswith("coverage-corpus-")
        ))

    def test_rejects_invalid_axes_lineage_tools_pools_and_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, tools = self.fixture(root, ("x64",))

            def invoke(
                source: Graph = upstream,
                architectures: tuple[str, ...] = ("x64",),
                **options: object,
            ) -> Graph:
                return coverage_artifact_graph(
                    repository,
                    source,
                    architectures=architectures,
                    pwsh=options.pop("pwsh", tools["pwsh.exe"]),  # type: ignore[arg-type]
                    pwsh_identity={"version": "7.5"},
                    llvm_profdata=tools["llvm-profdata.exe"],
                    llvm_profdata_identity={"version": "20.1"},
                    llvm_cov=tools["llvm-cov.exe"],
                    llvm_cov_identity={"version": "20.1"},
                    **options,
                )

            for invalid in ((), ("x64", "x64"), ("armv7",)):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "architectures"):
                    invoke(architectures=invalid)

            missing = Graph(
                tuple(item for item in upstream.nodes if item.name != "build-tests-x64-coverage"),
                tuple(name for name in upstream.targets if name != "build-tests-x64-coverage"),
                upstream.pools,
            )
            with self.assertRaisesRegex(ValueError, "unknown node"):
                invoke(missing)

            shared = node("detached-shared")
            left = node("detached-left", inputs=(shared.name,))
            right = node("detached-right", inputs=(shared.name,))
            detached = node("build-renpy-x64-coverage", inputs=(left.name, right.name))
            detached_upstream = Graph(
                tuple(
                    detached if current.name == detached.name else current
                    for current in upstream.nodes
                ) + (shared, left, right),
                upstream.targets,
                upstream.pools,
            )
            with self.assertRaisesRegex(ValueError, "restore ancestor"):
                invoke(detached_upstream)

            for options in ({"test_shards": 0}, {"jobs": True}, {"report_jobs": 0}):
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, "positive integer"):
                    invoke(**options)
            with self.assertRaisesRegex(FileNotFoundError, "not a file"):
                invoke(pwsh=tools["pwsh.exe"].parent)
            conflicting = Graph(
                upstream.nodes,
                upstream.targets,
                dict(upstream.pools) | {"coverage-report": 1},
            )
            with self.assertRaisesRegex(ValueError, "conflicting pool"):
                invoke(conflicting, report_jobs=2)
            corpus = root / "corpus"
            corpus.mkdir()
            for nonce in ("", True, "bad\0nonce"):
                with self.subTest(nonce=nonce), self.assertRaisesRegex(ValueError, "run nonce"):
                    invoke(corpus=corpus, run_nonce=nonce)
            with self.assertRaises(FileNotFoundError):
                invoke(corpus=root / "missing", run_nonce="run")
            file_corpus = root / "corpus.bin"
            file_corpus.touch()
            with self.assertRaises(NotADirectoryError):
                invoke(corpus=file_corpus, run_nonce="run")

    def test_complete_graph_discovers_and_builds_coverage_artifacts_itself(self) -> None:
        helper = instrumented_fixture.InstrumentedBuildGraphTests()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = helper.repository(root / "repo")
            toolchain = helper.toolchain(root)
            for name in ("llvm-cov.exe", "llvm-profdata.exe"):
                (toolchain.llvm_dir / "bin" / name).write_bytes(b"llvm-v1")
            discovery = coverage_dependency_discovery_slice(
                repository, toolchain, jobs=4, architectures=("x64",)
            )
            graph = coverage_graph(
                repository,
                toolchain,
                discovery=discovery,
                manifests=helper.manifests(repository, discovery),
                jobs=4,
                architectures=("x64",),
                test_shards=1,
            )
            corpus = root / "corpus"
            corpus.mkdir()
            corpus_graph = coverage_graph(
                repository,
                toolchain,
                discovery=discovery,
                manifests=helper.manifests(repository, discovery),
                jobs=4,
                architectures=("x64",),
                test_shards=1,
                corpus=corpus,
                run_nonce="high-level-run",
            )

        self.assertEqual(graph.targets, ("coverage-x64",))
        self.assertEqual(
            len(graph.nodes),
            len(discovery.nodes) + 4 + 1 + 1 + 2 + 1,
        )
        shard = graph.node("coverage-test-x64-0")
        self.assertIn("coverage-corpus-x64-0", tuple(node.name for node in corpus_graph.nodes))
        for project in ("renpy", "rpgmaker", "zanzarah", "tests"):
            build = graph.node(f"build-{project}-x64-coverage")
            self.assertIn("'/p:Configuration=Coverage'", build.command.stdin.decode())
            self.assertEqual(shard.inputs.count(build.name), 1)

    def test_complete_graph_hashes_exact_llvm_report_tool_bytes(self) -> None:
        helper = instrumented_fixture.InstrumentedBuildGraphTests()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = helper.repository(root / "repo")
            toolchain = helper.toolchain(root)
            cov = toolchain.llvm_dir / "bin/llvm-cov.exe"
            profdata = toolchain.llvm_dir / "bin/llvm-profdata.exe"
            cov.write_bytes(b"cov-v1")
            profdata.write_bytes(b"profdata-v1")
            discovery = coverage_dependency_discovery_slice(repository, toolchain, jobs=4)
            manifests = helper.manifests(repository, discovery)

            def build() -> Graph:
                return coverage_graph(
                    repository,
                    toolchain,
                    discovery=discovery,
                    manifests=manifests,
                    test_shards=1,
                )

            before = build()
            profdata.write_bytes(b"profdata-v2")
            profdata_changed = build()
            profdata.write_bytes(b"profdata-v1")
            cov.write_bytes(b"cov-v2")
            cov_changed = build()

        for name in (
            "coverage-test-x64-0",
            "coverage-merge-x64",
            "coverage-json-x64",
            "coverage-lcov-x64",
            "coverage-x64",
        ):
            self.assertEqual(
                name != "coverage-test-x64-0",
                before.node(name).uid != profdata_changed.node(name).uid,
                name,
            )
            self.assertEqual(
                name.startswith(("coverage-json-", "coverage-lcov-"))
                or name == "coverage-x64",
                before.node(name).uid != cov_changed.node(name).uid,
                name,
            )


class CppCoverageGateTests(unittest.TestCase):
    @staticmethod
    def report(lines: tuple[int, int] = (7, 7), branches: tuple[int, int] = (3, 3)) -> dict[str, object]:
        return {
            "type": "llvm.coverage.json.export",
            "data": [{"totals": {
                "lines": {"count": lines[0], "covered": lines[1]},
                "branches": {"count": branches[0], "covered": branches[1]},
            }}],
        }

    def test_gate_requires_nonempty_exactly_complete_first_party_lines_and_branches(self) -> None:
        require_full_coverage(self.report())
        for document, message in (
            (self.report(lines=(7, 6)), "lines"),
            (self.report(branches=(3, 2)), "branches"),
            (self.report(branches=(0, 0)), "no first-party branches"),
            (self.report(lines=(0, 0)), "no first-party lines"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(CoverageError, message):
                require_full_coverage(document)

    def test_gate_rejects_malformed_or_ambiguous_llvm_reports(self) -> None:
        malformed = (
            {},
            {"data": []},
            {"data": [{"totals": {}}, {"totals": {}}]},
            {"data": [{"totals": []}]},
            {"data": [{"totals": {"lines": [], "branches": {"count": 1, "covered": 1}}}]},
            {"data": [{"totals": {"lines": {"count": True, "covered": 1}, "branches": {"count": 1, "covered": 1}}}]},
            {"data": [{"totals": {"lines": {"count": 1, "covered": 2}, "branches": {"count": 1, "covered": 1}}}]},
        )
        for document in malformed:
            with self.subTest(document=document), self.assertRaisesRegex(CoverageError, "malformed"):
                require_full_coverage(document)

    def test_cli_reads_json_without_a_threshold_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "coverage.json"
            report.write_text(json.dumps(self.report()), encoding="utf-8")
            self.assertEqual(coverage_main(("gate", str(report))), 0)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                coverage_main(("gate", str(report), "99"))

            with (
                mock.patch.object(sys, "argv", ["cpp_coverage.py", "gate", str(report)]),
                self.assertWarnsRegex(RuntimeWarning, "core.cpp_coverage"),
                self.assertRaises(SystemExit) as raised,
            ):
                runpy.run_module("core.cpp_coverage", run_name="__main__")
            self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

from core.binary_audit import AuditError, main as binary_audit_main  # noqa: E402
from core.graph import Command, Graph, Node  # noqa: E402
from core.paths import BuildPaths  # noqa: E402
from graphs.audit import BinaryArtifact, audit_graph  # noqa: E402


def node(name: str, *, inputs: tuple[str, ...] = ()) -> Node:
    return Node(
        name,
        hashlib.md5(name.encode(), usedforsecurity=False).hexdigest(),
        "build",
        Command((str(Path("C:/sdk/build.exe")),)),
        inputs,
    )


class AuditGraphTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Graph, tuple[BinaryArtifact, ...], Path, Path]:
        repository = root / "repo"
        repository.mkdir()
        restore = node("restore-release")
        renpy = node("build-renpy-x64-release", inputs=(restore.name,))
        rpgmaker = node("build-rpgmaker-x86-release", inputs=(restore.name,))
        upstream = Graph(
            (restore, renpy, rpgmaker),
            (renpy.name, rpgmaker.name),
            {"build": 2},
        )
        paths = BuildPaths(repository)
        artifacts = (
            BinaryArtifact("x64", "renpy", renpy, paths.cas(renpy.uid, renpy.name).output / "renpy.so"),
            BinaryArtifact(
                "x86", "rpgmaker", rpgmaker,
                paths.cas(rpgmaker.uid, rpgmaker.name).output / "rpgmaker.so",
            ),
        )
        tools = root / "tools"
        tools.mkdir()
        dumpbin, binskim = tools / "dumpbin.exe", tools / "BinSkim.exe"
        dumpbin.touch()
        binskim.touch()
        return repository, upstream, artifacts, dumpbin, binskim

    def build_graph(
        self,
        root: Path,
        *,
        dumpbin_version: str = "14.44",
        binskim_version: str = "4.4",
    ) -> Graph:
        repository, upstream, artifacts, dumpbin, binskim = self.fixture(root)
        return audit_graph(
            repository,
            upstream,
            artifacts,
            dumpbin=dumpbin,
            dumpbin_identity={"path": str(dumpbin), "version": dumpbin_version},
            binskim=binskim,
            binskim_identity={"path": str(binskim), "version": binskim_version},
            jobs=3,
            binskim_jobs=2,
        )

    def test_each_artifact_composes_six_fine_grained_nodes_over_its_full_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph = self.build_graph(root)

        self.assertEqual(graph.pools, {"build": 2, "audit": 3, "binskim": 2, "dumpbin": 3})
        self.assertEqual(
            graph.targets,
            (
                "audit-pe-x64-renpy",
                "audit-binskim-x64-renpy",
                "audit-pe-x86-rpgmaker",
                "audit-binskim-x86-rpgmaker",
            ),
        )
        self.assertEqual(15, len(graph.nodes))
        self.assertEqual(graph.node("build-renpy-x64-release").inputs, ("restore-release",))

        for architecture, module, producer in (
            ("x64", "renpy", "build-renpy-x64-release"),
            ("x86", "rpgmaker", "build-rpgmaker-x86-release"),
        ):
            dump_nodes = tuple(
                graph.node(f"audit-dumpbin-{mode}-{architecture}-{module}")
                for mode in ("headers", "dependents", "exports")
            )
            pe_gate = graph.node(f"audit-pe-{architecture}-{module}")
            binskim_run = graph.node(f"audit-binskim-run-{architecture}-{module}")
            binskim_gate = graph.node(f"audit-binskim-{architecture}-{module}")
            self.assertTrue(all(current.inputs == (producer,) for current in dump_nodes))
            self.assertEqual(pe_gate.inputs, tuple(current.name for current in dump_nodes))
            self.assertEqual(binskim_run.inputs, (producer,))
            self.assertEqual(binskim_gate.inputs, (binskim_run.name,))
            self.assertTrue(all(current.pool == "dumpbin" for current in dump_nodes))
            self.assertEqual((pe_gate.pool, binskim_run.pool, binskim_gate.pool), ("audit", "binskim", "audit"))

    def test_dumpbin_and_python_gates_receive_exact_binary_logs_and_report_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, artifacts, dumpbin, binskim = self.fixture(root)
            graph = audit_graph(
                repository,
                upstream,
                artifacts[:1],
                dumpbin=dumpbin,
                dumpbin_identity={"version": "14.44"},
                binskim=binskim,
                binskim_identity={"version": "4.4"},
            )
            paths = BuildPaths(repository)

        dump_nodes = tuple(
            graph.node(f"audit-dumpbin-{mode}-x64-renpy")
            for mode in ("headers", "dependents", "exports")
        )
        for mode, current in zip(("headers", "dependents", "exports"), dump_nodes, strict=True):
            self.assertEqual(current.command.argv, (str(dumpbin), f"/{mode}", str(artifacts[0].path)))
        pe_gate = graph.node("audit-pe-x64-renpy")
        self.assertEqual(pe_gate.command.argv[1:5], ("-m", "core.binary_audit", "pe", "x64"))
        self.assertEqual(
            pe_gate.command.argv[5:],
            tuple(str(paths.cas(current.uid, current.name).log) for current in dump_nodes),
        )
        binskim_run = graph.node("audit-binskim-run-x64-renpy")
        self.assertEqual(
            binskim_run.command.argv[1:],
            ("-m", "core.binary_audit", "run-binskim", str(binskim), str(artifacts[0].path)),
        )
        binskim_gate = graph.node("audit-binskim-x64-renpy")
        report = paths.cas(binskim_run.uid, binskim_run.name).output / "binskim.sarif"
        self.assertEqual(binskim_gate.command.argv[1:], ("-m", "core.binary_audit", "binskim", str(report)))

    def test_tool_identity_invalidates_only_its_run_and_semantic_consumers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, artifacts, dumpbin, binskim = self.fixture(root)

            def graph(dumpbin_version: str, binskim_version: str) -> Graph:
                return audit_graph(
                    repository,
                    upstream,
                    artifacts,
                    dumpbin=dumpbin,
                    dumpbin_identity={"version": dumpbin_version},
                    binskim=binskim,
                    binskim_identity={"version": binskim_version},
                )

            before = graph("14.44", "4.4")
            dump_changed = graph("14.45", "4.4")
            binskim_changed = graph("14.44", "4.5")

        for current in before.nodes:
            dump_partition = "dumpbin" in current.name or "audit-pe-" in current.name
            binskim_partition = "binskim" in current.name
            self.assertEqual(
                dump_partition,
                current.uid != dump_changed.node(current.name).uid,
                current.name,
            )
            self.assertEqual(
                binskim_partition,
                current.uid != binskim_changed.node(current.name).uid,
                current.name,
            )

    def test_artifact_must_match_its_exact_upstream_producer_cas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, artifacts, dumpbin, binskim = self.fixture(root)
            escaped = BinaryArtifact("x64", "renpy", artifacts[0].producer, root / "outside.so")
            with self.assertRaisesRegex(ValueError, "producer CAS"):
                audit_graph(
                    repository,
                    upstream,
                    (escaped,),
                    dumpbin=dumpbin,
                    dumpbin_identity={"version": "14.44"},
                    binskim=binskim,
                    binskim_identity={"version": "4.4"},
                )

    def test_graph_rejects_ambiguous_tools_pools_artifacts_and_capacities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, artifacts, dumpbin, binskim = self.fixture(root)

            def invoke(
                selected: tuple[BinaryArtifact, ...] = artifacts,
                *,
                source: Graph = upstream,
                dumpbin_path: Path = dumpbin,
                jobs: object = 2,
                binskim_jobs: object = 1,
            ) -> Graph:
                return audit_graph(
                    repository,
                    source,
                    selected,
                    dumpbin=dumpbin_path,
                    dumpbin_identity={"version": "14.44"},
                    binskim=binskim,
                    binskim_identity={"version": "4.4"},
                    jobs=jobs,  # type: ignore[arg-type]
                    binskim_jobs=binskim_jobs,  # type: ignore[arg-type]
                )

            for jobs, binskim_jobs in ((True, 1), ("two", 1), (2, 0)):
                with self.subTest(capacities=(jobs, binskim_jobs)), self.assertRaisesRegex(
                    ValueError, "capacities"
                ):
                    invoke(jobs=jobs, binskim_jobs=binskim_jobs)

            with self.assertRaisesRegex(FileNotFoundError, "not a file"):
                invoke(dumpbin_path=dumpbin.parent)
            with self.assertRaisesRegex(ValueError, "at least one"):
                invoke(())
            with self.assertRaisesRegex(ValueError, "duplicate"):
                invoke((artifacts[0], artifacts[0]))
            for invalid in (
                BinaryArtifact("riscv64", "renpy", artifacts[0].producer, artifacts[0].path),
                BinaryArtifact("x64", "Bad Module", artifacts[0].producer, artifacts[0].path),
            ):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "identity"):
                    invoke((invalid,))

            impostor = node(artifacts[0].producer.name)
            mismatched = BinaryArtifact("x64", "renpy", impostor, artifacts[0].path)
            with self.assertRaisesRegex(ValueError, "producer CAS"):
                invoke((mismatched,))
            expected_output = BuildPaths(repository).cas(
                artifacts[0].producer.uid, artifacts[0].producer.name
            ).output
            for wrong_path in (expected_output, BuildPaths(repository).cas_root / "other.so"):
                with self.subTest(wrong_path=wrong_path), self.assertRaisesRegex(
                    ValueError, "producer CAS"
                ):
                    invoke((BinaryArtifact("x64", "renpy", artifacts[0].producer, wrong_path),))

            matching_pools = Graph(
                upstream.nodes,
                upstream.targets,
                {"build": 2, "audit": 2, "binskim": 1, "dumpbin": 2},
            )
            self.assertEqual(invoke(source=matching_pools).pools["audit"], 2)
            conflicting = Graph(upstream.nodes, upstream.targets, {"build": 2, "audit": 1})
            with self.assertRaisesRegex(ValueError, "conflicting pool"):
                invoke(source=conflicting)

    def test_binary_audit_cli_dispatches_pe_binskim_and_literal_binskim_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            headers, dependents, exports = root / "headers.txt", root / "dependents.txt", root / "exports.txt"
            headers.write_text(" 8664 machine (x64)\n", encoding="utf-8")
            dependents.write_text(" KERNEL32.dll\n", encoding="utf-8")
            exports.write_text(
                " 1 0 0001 LoadSubModule\n 2 1 0002 UnloadSubModule\n", encoding="utf-8"
            )
            self.assertEqual(
                0,
                binary_audit_main(("pe", "x64", str(headers), str(dependents), str(exports))),
            )
            report = root / "approved.sarif"
            report.write_text(json.dumps({"runs": []}), encoding="utf-8")
            self.assertEqual(0, binary_audit_main(("binskim", str(report))))

            output = root / "out"
            output.mkdir()
            tool, binary = root / "BinSkim.exe", root / "renpy.so"
            tool.touch()
            binary.touch()

            def complete(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                Path(argv[argv.index("--output") + 1]).write_text('{"runs":[]}', encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0)

            with mock.patch.dict(os.environ, {"OBSERVER_OUT_DIR": str(output)}, clear=False):
                with mock.patch("core.binary_audit.subprocess.run", side_effect=complete) as invoked:
                    self.assertEqual(0, binary_audit_main(("run-binskim", str(tool), str(binary))))
            argv = invoked.call_args.args[0]
            self.assertEqual(argv[:3], [str(tool), "analyze", str(binary)])
            self.assertIn("--disable-telemetry", argv)
            self.assertEqual(output / "binskim.sarif", Path(argv[argv.index("--output") + 1]))

            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(AuditError, "OBSERVER_OUT_DIR"):
                    binary_audit_main(("run-binskim", str(tool), str(binary)))
            empty_output = root / "empty-output"
            empty_output.mkdir()
            with mock.patch.dict(os.environ, {"OBSERVER_OUT_DIR": str(empty_output)}, clear=False):
                with mock.patch("core.binary_audit.subprocess.run"):
                    with self.assertRaisesRegex(AuditError, "did not produce"):
                        binary_audit_main(("run-binskim", str(tool), str(binary)))

            with (
                mock.patch.object(sys, "argv", ["binary_audit.py", "binskim", str(report)]),
                self.assertWarnsRegex(RuntimeWarning, "core.binary_audit"),
                self.assertRaises(SystemExit) as raised,
            ):
                runpy.run_module("core.binary_audit", run_name="__main__")
            self.assertEqual(0, raised.exception.code)


if __name__ == "__main__":
    unittest.main(verbosity=2)

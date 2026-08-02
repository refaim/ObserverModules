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
from graphs.audit import audit_graph  # noqa: E402


def node(name: str, *, inputs: tuple[str, ...] = ()) -> Node:
    return Node(
        name,
        hashlib.md5(name.encode(), usedforsecurity=False).hexdigest(),
        "build",
        Command((str(Path("C:/sdk/build.exe")),)),
        inputs,
    )


class AuditGraphTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Graph, Path, Path]:
        repository = root / "repo"
        repository.mkdir()
        restore = node("restore-release")
        builds = tuple(
            node(f"build-{module}-{architecture}-release", inputs=(restore.name,))
            for architecture in ("x64", "x86")
            for module in ("renpy", "rpgmaker", "zanzarah")
        )
        upstream = Graph(
            (restore, *builds),
            tuple(item.name for item in builds),
            {"build": 2},
        )
        tools = root / "tools"
        tools.mkdir()
        dumpbin, binskim = tools / "dumpbin.exe", tools / "BinSkim.exe"
        dumpbin.touch()
        binskim.touch()
        return repository, upstream, dumpbin, binskim

    def build_graph(
        self,
        root: Path,
        *,
        dumpbin_version: str = "14.44",
        binskim_version: str = "4.4",
    ) -> Graph:
        repository, upstream, dumpbin, binskim = self.fixture(root)
        return audit_graph(
            repository,
            upstream,
            architectures=("x64", "x86"),
            dumpbin=dumpbin,
            dumpbin_identity={"path": str(dumpbin), "version": dumpbin_version},
            binskim=binskim,
            binskim_identity={"path": str(binskim), "version": binskim_version},
            jobs=3,
        )

    def test_selects_canonical_release_producers_without_artifact_dtos(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, upstream, dumpbin, binskim = self.fixture(Path(temporary))
            graph = audit_graph(
                repository,
                upstream,
                architectures=("x64", "x86"),
                dumpbin=dumpbin,
                dumpbin_identity={"version": "14.44"},
                binskim=binskim,
                binskim_identity={"version": "4.4"},
            )

        self.assertEqual(
            graph.node("audit-binskim-run-x64-renpy").inputs,
            ("build-renpy-x64-release",),
        )
        self.assertEqual(
            graph.node("audit-binskim-run-x86-rpgmaker").inputs,
            ("build-rpgmaker-x86-release",),
        )

    def test_each_artifact_composes_six_fine_grained_nodes_over_its_full_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph = self.build_graph(root)

        self.assertEqual(graph.pools, {"build": 2, "slot": 3})
        self.assertEqual(
            graph.targets,
            tuple(
                f"audit-{kind}-{architecture}-{module}"
                for architecture in ("x64", "x86")
                for module in ("renpy", "rpgmaker", "zanzarah")
                for kind in ("pe", "binskim")
            ),
        )
        self.assertEqual(43, len(graph.nodes))
        self.assertEqual(graph.node("build-renpy-x64-release").inputs, ("restore-release",))

        for architecture in ("x64", "x86"):
          for module in ("renpy", "rpgmaker", "zanzarah"):
            producer = f"build-{module}-{architecture}-release"
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
            self.assertEqual(
                tuple((item.id, item.kind, item.media_type, item.relative_path)
                      for item in binskim_run.results),
                ((
                    f"reports/sarif/{architecture}/binskim-{module}.sarif",
                    "sarif", "application/sarif+json", "binskim.sarif",
                ),),
            )
            self.assertTrue(all(not current.results for current in (*dump_nodes, pe_gate, binskim_gate)))
            self.assertTrue(all(current.pool == "slot" for current in dump_nodes))
            self.assertEqual((pe_gate.pool, binskim_run.pool, binskim_gate.pool), ("slot", "slot", "slot"))

    def test_dumpbin_and_python_gates_receive_exact_binary_logs_and_report_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, dumpbin, binskim = self.fixture(root)
            graph = audit_graph(
                repository,
                upstream,
                architectures=("x64",),
                dumpbin=dumpbin,
                dumpbin_identity={"version": "14.44"},
                binskim=binskim,
                binskim_identity={"version": "4.4"},
            )
            paths = BuildPaths(repository)
            binary = paths.cas(upstream.node("build-renpy-x64-release").uid).output / "renpy.so"

        dump_nodes = tuple(
            graph.node(f"audit-dumpbin-{mode}-x64-renpy")
            for mode in ("headers", "dependents", "exports")
        )
        for mode, current in zip(("headers", "dependents", "exports"), dump_nodes, strict=True):
            self.assertEqual(current.command.argv, (str(dumpbin), f"/{mode}", str(binary)))
        pe_gate = graph.node("audit-pe-x64-renpy")
        self.assertEqual(pe_gate.command.argv[1:5], ("-m", "core.binary_audit", "pe", "x64"))
        self.assertEqual(
            pe_gate.command.argv[5:],
            tuple(str(paths.cas(current.uid).log) for current in dump_nodes),
        )
        binskim_run = graph.node("audit-binskim-run-x64-renpy")
        self.assertEqual(
            binskim_run.command.argv[1:],
            ("-m", "core.binary_audit", "run-binskim", str(binskim), str(binary)),
        )
        binskim_gate = graph.node("audit-binskim-x64-renpy")
        report = paths.cas(binskim_run.uid).output / "binskim.sarif"
        self.assertEqual(binskim_gate.command.argv[1:], ("-m", "core.binary_audit", "binskim", str(report)))

    def test_tool_identity_invalidates_only_its_run_and_semantic_consumers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, dumpbin, binskim = self.fixture(root)

            def graph(dumpbin_version: str, binskim_version: str) -> Graph:
                return audit_graph(
                    repository,
                    upstream,
                    architectures=("x64", "x86"),
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

    def test_graph_rejects_invalid_axes_tools_pools_and_capacities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, dumpbin, binskim = self.fixture(root)

            def invoke(
                *,
                source: Graph = upstream,
                architectures: tuple[str, ...] = ("x64", "x86"),
                dumpbin_path: Path = dumpbin,
                jobs: object = 2,
            ) -> Graph:
                return audit_graph(
                    repository,
                    source,
                    architectures=architectures,
                    dumpbin=dumpbin_path,
                    dumpbin_identity={"version": "14.44"},
                    binskim=binskim,
                    binskim_identity={"version": "4.4"},
                    jobs=jobs,  # type: ignore[arg-type]
                )

            for jobs in (True, "two", 0):
                with self.subTest(jobs=jobs), self.assertRaisesRegex(
                    ValueError, "capacities"
                ):
                    invoke(jobs=jobs)

            with self.assertRaisesRegex(FileNotFoundError, "not a file"):
                invoke(dumpbin_path=dumpbin.parent)
            for invalid in ((), ("x64", "x64"), ("riscv64",)):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "architectures"):
                    invoke(architectures=invalid)

            incomplete = Graph(
                tuple(item for item in upstream.nodes if item.name != "build-renpy-x64-release"),
                tuple(name for name in upstream.targets if name != "build-renpy-x64-release"),
                upstream.pools,
            )
            with self.assertRaisesRegex(ValueError, "unknown node"):
                invoke(source=incomplete)

            matching_pools = Graph(
                upstream.nodes,
                upstream.targets,
                {"build": 2, "slot": 2},
            )
            self.assertEqual(invoke(source=matching_pools).pools["slot"], 2)
            conflicting = Graph(upstream.nodes, upstream.targets, {"build": 2, "slot": 1})
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

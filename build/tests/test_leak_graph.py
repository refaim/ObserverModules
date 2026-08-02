from __future__ import annotations

import hashlib
import io
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

from core.graph import Command, Graph, Node  # noqa: E402
import core.leak as leak  # noqa: E402
from core.leak import LeakError, main as leak_main  # noqa: E402
from core.paths import BuildPaths  # noqa: E402
from graphs.leak import LEAK_MODES, LEAK_SCENARIOS, leak_graph  # noqa: E402


def node(name: str, *, inputs: tuple[str, ...] = ()) -> Node:
    return Node(
        name,
        hashlib.md5(name.encode(), usedforsecurity=False).hexdigest(),
        "build",
        Command(("build.exe",)),
        inputs,
    )


class LeakGraphTests(unittest.TestCase):
    def fixture(
        self, root: Path
    ) -> tuple[Path, Graph, Path]:
        repository = root / "repo"
        repository.mkdir()
        restore = node("restore-release")
        builds = tuple(
            node(f"build-{name}-x64-release", inputs=(restore.name,))
            for name in ("leak-probe", "renpy", "rpgmaker", "zanzarah")
        )
        audit_gates = tuple(
            node(f"audit-{kind}-x64-{module}", inputs=(producer.name,))
            for module, producer in zip(
                ("leak-probe", "renpy", "rpgmaker", "zanzarah"), builds, strict=True
            )
            for kind in ("pe", "binskim")
        )
        upstream = Graph((restore, *builds, *audit_gates), tuple(item.name for item in builds), {"build": 4})
        tools = root / "tools"
        tools.mkdir()
        umdh = tools / "umdh.exe"
        umdh.touch()
        return repository, upstream, umdh

    def build_graph(self, root: Path, **options: object) -> Graph:
        repository, upstream, umdh = self.fixture(root)
        return leak_graph(
            repository,
            upstream,
            umdh=umdh,
            umdh_identity={"version": "10.0"},
            run_nonce="run-42",
            **options,
        )

    def test_default_graph_has_one_shared_setup_and_fourteen_independent_branches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            graph = self.build_graph(Path(temporary), jobs=7)

        leak_nodes = tuple(item for item in graph.nodes if item.name.startswith("leak-"))
        self.assertEqual(99, len(leak_nodes))
        self.assertEqual(
            graph.pools,
            {"build": 4, "slot": 7},
        )
        self.assertEqual(
            graph.targets,
            tuple(
                f"leak-gate-{mode}-{scenario}"
                for mode in LEAK_MODES
                for scenario in LEAK_SCENARIOS
            ),
        )

        setup = graph.node("leak-setup-x64-release")
        self.assertEqual(
            setup.inputs,
            ("build-leak-probe-x64-release",)
            + tuple(
                dependency
                for module in ("renpy", "rpgmaker", "zanzarah")
                for dependency in (
                    f"build-{module}-x64-release",
                    f"audit-pe-x64-{module}",
                    f"audit-binskim-x64-{module}",
                )
            ),
        )
        self.assertEqual(setup.command.argv[1:4], ("-m", "core.leak", "setup"))

        for mode in LEAK_MODES:
            for scenario in LEAK_SCENARIOS:
                stem = f"{mode}-{scenario}"
                preflight = graph.node(f"leak-preflight-{stem}")
                capture = graph.node(f"leak-capture-{stem}")
                adjacent = tuple(
                    graph.node(f"leak-diff-{stem}-window-{index}") for index in (1, 2)
                )
                overall = graph.node(f"leak-diff-{stem}-overall")
                summary = graph.node(f"leak-summary-{stem}")
                gate = graph.node(f"leak-gate-{stem}")

                self.assertEqual(preflight.inputs, (setup.name,))
                self.assertEqual(capture.inputs, (preflight.name,))
                self.assertTrue(all(item.inputs == (capture.name,) for item in (*adjacent, overall)))
                self.assertEqual(summary.inputs, tuple(item.name for item in (*adjacent, overall)))
                self.assertEqual(gate.inputs, (summary.name,))
                self.assertEqual(
                    (preflight.pool, capture.pool, adjacent[0].pool, summary.pool, gate.pool),
                    ("slot", "slot", "slot", "slot", "slot"),
                )
                self.assertEqual(preflight.command.argv[-2:], (mode, scenario))
                self.assertEqual(capture.command.argv[6:8], (mode, scenario))
                self.assertEqual((overall.command.argv[3], overall.command.argv[6]), ("diff", "overall"))
                self.assertEqual(summary.command.argv[9], "0")
                self.assertEqual(
                    tuple((item.id, item.kind, item.media_type, item.relative_path)
                          for item in preflight.results),
                    ((f"reports/leak/x64/{mode}/{scenario}/preflight.json",
                      "leak", "application/json", "preflight.json"),),
                )
                self.assertEqual(
                    tuple((item.id, item.relative_path) for item in capture.results),
                    (
                        (f"reports/leak/x64/{mode}/{scenario}/capture.json", "capture.json"),
                        (f"reports/leak/x64/{mode}/{scenario}/snapshots", "snapshots"),
                        (f"reports/leak/x64/{mode}/{scenario}/probe.stderr.log", "probe.stderr.log"),
                    ),
                )
                evidence = tuple(zip(("window-1", "window-2"), adjacent, strict=True)) + (
                    ("overall", overall),
                )
                for label, item in evidence:
                    self.assertEqual(
                        tuple((result.id, result.relative_path) for result in item.results),
                        (
                            (f"reports/leak/x64/{mode}/{scenario}/diffs/{label}.json", "diff.json"),
                            (f"reports/leak/x64/{mode}/{scenario}/diffs/{label}.txt", "report.txt"),
                        ),
                    )
                self.assertEqual(
                    tuple((item.id, item.relative_path) for item in summary.results),
                    ((f"reports/leak/x64/{mode}/{scenario}/summary.json", "summary.json"),),
                )
                self.assertEqual(gate.results, ())

    def test_measurement_options_expand_snapshot_diffs_and_are_signed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph = self.build_graph(
                root, warmup=2, iterations=3, windows=4, tolerance_bytes=17
            )

        self.assertEqual(113, len(tuple(item for item in graph.nodes if item.name.startswith("leak-"))))
        capture = graph.node("leak-capture-operations-small-success")
        self.assertEqual(capture.command.argv[-3:], ("2", "3", "4"))
        summary = graph.node("leak-summary-operations-small-success")
        self.assertEqual(
            summary.inputs,
            (
                "leak-diff-operations-small-success-window-1",
                "leak-diff-operations-small-success-window-2",
                "leak-diff-operations-small-success-window-3",
                "leak-diff-operations-small-success-overall",
            ),
        )
        self.assertEqual(summary.command.argv[3:10], ("summarize", "operations", "small-success", "2", "3", "4", "17"))

    def test_run_and_tool_identities_invalidate_only_the_measurement_partition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, umdh = self.fixture(root)

            def graph(
                *,
                nonce: str = "run-a",
                umdh_version: str = "10.0",
                tolerance: int = 4096,
            ) -> Graph:
                return leak_graph(
                    repository,
                    upstream,
                    umdh=umdh,
                    umdh_identity={"version": umdh_version},
                    run_nonce=nonce,
                    tolerance_bytes=tolerance,
                )

            before = graph()
            rerun = graph(nonce="run-b")
            new_umdh = graph(umdh_version="10.1")
            new_tolerance = graph(tolerance=8192)

        for current in before.nodes:
            if not current.name.startswith("leak-"):
                continue
            measured = current.name.startswith(("leak-capture-", "leak-diff-", "leak-summary-", "leak-gate-"))
            judged = current.name.startswith(("leak-summary-", "leak-gate-"))
            self.assertEqual(measured, current.uid != rerun.node(current.name).uid, current.name)
            self.assertEqual(measured, current.uid != new_umdh.node(current.name).uid, current.name)
            self.assertEqual(judged, current.uid != new_tolerance.node(current.name).uid, current.name)

    def test_rejects_invalid_tools_lineage_pools_and_measurement_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, umdh = self.fixture(root)

            def invoke(
                *,
                source: Graph = upstream,
                umdh_path: Path = umdh,
                nonce: object = "run-42",
                warmup: object = 8,
                iterations: object = 100,
                windows: object = 3,
                tolerance: object = 4096,
                jobs: object = 4,
            ) -> Graph:
                return leak_graph(
                    repository,
                    source,
                    umdh=umdh_path,
                    umdh_identity={"version": "10.0"},
                    run_nonce=nonce,  # type: ignore[arg-type]
                    warmup=warmup,  # type: ignore[arg-type]
                    iterations=iterations,  # type: ignore[arg-type]
                    windows=windows,  # type: ignore[arg-type]
                    tolerance_bytes=tolerance,  # type: ignore[arg-type]
                    jobs=jobs,  # type: ignore[arg-type]
                )

            for jobs in (True, "four", 0):
                with self.subTest(jobs=jobs), self.assertRaisesRegex(
                    ValueError, "capacities"
                ):
                    invoke(jobs=jobs)
            for counts in ((True, 100, 3), (8, "many", 3), (8, 100, 0)):
                with self.subTest(counts=counts), self.assertRaisesRegex(ValueError, "counts"):
                    invoke(warmup=counts[0], iterations=counts[1], windows=counts[2])
            with self.assertRaisesRegex(ValueError, "at least three"):
                invoke(windows=2)
            for tolerance in (True, "zero", -1):
                with self.subTest(tolerance=tolerance), self.assertRaisesRegex(ValueError, "tolerance"):
                    invoke(tolerance=tolerance)
            for nonce in (7, "", "bad\0nonce"):
                with self.subTest(nonce=nonce), self.assertRaisesRegex(ValueError, "nonce"):
                    invoke(nonce=nonce)

            with self.assertRaisesRegex(FileNotFoundError, "not a file"):
                invoke(umdh_path=umdh.parent)

            missing_names = {
                "build-leak-probe-x64-release",
                "audit-pe-x64-leak-probe",
                "audit-binskim-x64-leak-probe",
            }
            missing_build = Graph(
                tuple(item for item in upstream.nodes if item.name not in missing_names),
                tuple(name for name in upstream.targets if name != "build-leak-probe-x64-release"),
                upstream.pools,
            )
            with self.assertRaisesRegex(ValueError, "unknown node"):
                invoke(source=missing_build)

            missing_gate = Graph(
                tuple(node for node in upstream.nodes if node.name != "audit-binskim-x64-renpy"),
                upstream.targets,
                upstream.pools,
            )
            with self.assertRaisesRegex(ValueError, "audit gates"):
                invoke(source=missing_gate)

            matching = Graph(
                upstream.nodes,
                upstream.targets,
                {"build": 4, "slot": 4},
            )
            self.assertEqual(4, invoke(source=matching).pools["slot"])
            conflicting = Graph(upstream.nodes, upstream.targets, {"build": 4, "slot": 1})
            with self.assertRaisesRegex(ValueError, "conflicting pool"):
                invoke(source=conflicting)


class FakeProbe:
    def __init__(self, lines: list[str], *, result: int = 0, timeout: bool = False) -> None:
        class Stream(io.StringIO):
            def close(stream) -> None:
                stream.was_closed = True

        self.stdout = Stream("\n".join(lines) + "\n")
        self.stdin = Stream()
        self.pid = 77
        self.result = result
        self.timeout = timeout
        self.returncode: int | None = None
        self.killed = False
        self.descendant = mock.Mock()

    def wait(self, timeout: int = 0) -> int:
        if self.timeout:
            raise leak.psutil.TimeoutExpired(timeout, self.pid)
        self.returncode = self.result
        return self.result

    def poll(self) -> int | None:
        return self.returncode

    def children(self, *, recursive: bool) -> list[object]:
        self.recursive = recursive
        return [self.descendant]

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class LeakWorkerTests(unittest.TestCase):
    def output(self, root: Path):
        return mock.patch.dict(os.environ, {"OBSERVER_OUT_DIR": str(root)}, clear=False)

    def test_setup_stages_exact_binaries_symbols_and_hash_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "out"
            output.mkdir()
            sources = []
            for index, name in enumerate(leak.BINARIES):
                directory = root / str(index)
                directory.mkdir()
                source = directory / name
                source.write_bytes(name.encode())
                (directory / f"{index}.pdb").write_bytes(bytes([index]))
                sources.append(source)
            with self.output(output):
                self.assertEqual(0, leak_main(("setup", *(str(path) for path in sources))))
            evidence = json.loads((output / "release-binaries.json").read_text(encoding="utf-8"))

            self.assertEqual(list(leak.BINARIES), [item["name"] for item in evidence["binaries"]])
            self.assertEqual("MT_StaticRelease", evidence["runtimeLibrary"])
            self.assertTrue(all((output / name).is_file() for name in leak.BINARIES))
            self.assertTrue(all((output / f"{index}.pdb").is_file() for index in range(4)))

            with self.output(output), self.assertRaisesRegex(LeakError, "not found"):
                leak_main(("setup", *(str(path) for path in (*sources[:-1], root / "missing"))))

    def test_preflight_uses_communicate_safe_capture_and_checks_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "leak-probe.exe").touch()
            ready = "OBSERVER_LEAK_PROBE|READY|pid=12|mode=operations|configuration=Release|scenarios=malformed"
            complete = subprocess.CompletedProcess([], 0, ready + "\nOBSERVER_LEAK_PROBE|DONE|pid=12\n")
            with self.output(root), mock.patch("core.leak.subprocess.run", return_value=complete) as invoked:
                leak_main(("preflight", str(root), "operations", "malformed"))
            self.assertIs(invoked.call_args.kwargs["stderr"], subprocess.STDOUT)
            self.assertIn("--automatic", invoked.call_args.args[0])

            failures = (
                subprocess.CompletedProcess([], 2, "broken"),
                subprocess.CompletedProcess([], 0, ready),
                subprocess.CompletedProcess([], 0, ready.replace("malformed", "read-failure") + "\nOBSERVER_LEAK_PROBE|DONE|pid=12"),
            )
            for result in failures:
                with self.subTest(result=result), self.output(root), mock.patch(
                    "core.leak.subprocess.run", return_value=result
                ), self.assertRaises(LeakError):
                    leak_main(("preflight", str(root), "operations", "malformed"))

    def test_capture_streams_stdout_redirects_stderr_to_file_and_uses_psutil(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "leak-probe.exe").touch()
            umdh = root / "umdh.exe"
            umdh.touch()
            gflags = root / "gflags.exe"
            gflags.touch()
            lines = [
                "probe startup noise",
                "OBSERVER_LEAK_PROBE|READY|pid=77|mode=operations|configuration=Release|scenarios=malformed",
                *(f"OBSERVER_LEAK_PROBE|SNAPSHOT|{label}|pid=77|completed_operations=1" for label in ("baseline", "window-1", "window-2", "window-3")),
                "OBSERVER_LEAK_PROBE|DONE|pid=77|completed_operations=4",
            ]
            process = FakeProbe(lines)

            def snapshot(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                if Path(argv[0]) == gflags:
                    output = "Current Registry Settings for leak-probe.exe executable are: 00000000" if len(argv) == 3 else ""
                    return subprocess.CompletedProcess(argv, 0, output)
                Path(argv[-1].removeprefix("-f:")).write_text("BackTrace 1\n", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "")

            with self.output(root), mock.patch("core.leak.psutil.Popen", return_value=process) as popen, mock.patch(
                "core.leak.subprocess.run", side_effect=snapshot
            ) as invoked:
                leak_main(("capture", str(root), str(umdh), "operations", "malformed", "1", "2", "3"))

            commands = [call.args[0] for call in invoked.call_args_list]
            self.assertEqual(
                commands[:2],
                [
                    [str(gflags), "/i", "leak-probe.exe"],
                    [str(gflags), "/i", "leak-probe.exe", "+ust"],
                ],
            )
            self.assertEqual([str(gflags), "/i", "leak-probe.exe", "-ust"], commands[2])
            self.assertIsNot(popen.call_args.kwargs["stderr"], subprocess.PIPE)
            self.assertEqual("continue|baseline\ncontinue|window-1\ncontinue|window-2\ncontinue|window-3\n", process.stdin.getvalue())
            self.assertTrue(process.stdin.was_closed)
            self.assertTrue(process.stdout.was_closed)
            self.assertEqual(77, json.loads((root / "capture.json").read_text())["processId"])

    def test_capture_kills_the_probe_tree_on_protocol_timeout_or_exit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "leak-probe.exe").touch()
            umdh = root / "umdh.exe"
            umdh.touch()
            gflags = root / "gflags.exe"
            gflags.touch()
            cases = (
                FakeProbe(["OBSERVER_LEAK_PROBE|READY|pid=77|mode=operations|configuration=Release|scenarios=malformed"]),
                FakeProbe([
                    "OBSERVER_LEAK_PROBE|READY|pid=77|mode=operations|configuration=Release|scenarios=malformed",
                    "OBSERVER_LEAK_PROBE|SNAPSHOT|baseline|pid=12|",
                ]),
                FakeProbe([
                    "OBSERVER_LEAK_PROBE|READY|pid=77|mode=operations|configuration=Release|scenarios=malformed",
                    *(f"OBSERVER_LEAK_PROBE|SNAPSHOT|{label}|pid=77|" for label in ("baseline", "window-1", "window-2", "window-3")),
                    "OBSERVER_LEAK_PROBE|DONE|pid=77|",
                ], timeout=True),
                FakeProbe([
                    "OBSERVER_LEAK_PROBE|READY|pid=77|mode=operations|configuration=Release|scenarios=malformed",
                    *(f"OBSERVER_LEAK_PROBE|SNAPSHOT|{label}|pid=77|" for label in ("baseline", "window-1", "window-2", "window-3")),
                    "OBSERVER_LEAK_PROBE|DONE|pid=77|",
                ], result=5),
            )

            def snapshot(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                if Path(argv[0]) == gflags:
                    output = "Current Registry Settings for leak-probe.exe executable are: 00000000" if len(argv) == 3 else ""
                    return subprocess.CompletedProcess(argv, 0, output)
                Path(argv[-1].removeprefix("-f:")).write_text("BackTrace\n", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "")

            for index, process in enumerate(cases):
                output = root / f"output-{index}"
                output.mkdir()
                with self.subTest(process=process), self.output(output), mock.patch(
                    "core.leak.psutil.Popen", return_value=process
                ), mock.patch("core.leak.psutil.wait_procs"), mock.patch(
                    "core.leak.subprocess.run", side_effect=snapshot
                ), self.assertRaises(LeakError):
                    leak_main(("capture", str(root), str(umdh), "operations", "malformed", "1", "2", "3"))
                if process.timeout or process.result == 0:
                    self.assertTrue(process.killed)

            rejected = root / "gflags-rejected"
            rejected.mkdir()
            failure = subprocess.CompletedProcess([], 1, "access denied")

            def reject(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                if len(argv) == 3:
                    return subprocess.CompletedProcess(
                        argv, 0, "Current Registry Settings for leak-probe.exe executable are: 00000000"
                    )
                return failure

            with self.output(rejected), mock.patch(
                "core.leak.subprocess.run", side_effect=reject
            ), mock.patch("core.leak.psutil.Popen") as popen, self.assertRaisesRegex(
                LeakError, "GFlags \\+ust failed"
            ):
                leak_main(("capture", str(root), str(umdh), "operations", "malformed", "1", "2", "3"))
            popen.assert_not_called()

            cleanup = root / "gflags-cleanup"
            cleanup.mkdir()

            def reject_cleanup(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                output = "Current Registry Settings for leak-probe.exe executable are: 00000000" if len(argv) == 3 else ""
                return failure if argv[-1] == "-ust" else subprocess.CompletedProcess(argv, 0, output)

            process = FakeProbe([])
            with self.output(cleanup), mock.patch(
                "core.leak.subprocess.run", side_effect=reject_cleanup
            ), mock.patch("core.leak.psutil.Popen", return_value=process), mock.patch(
                "core.leak.psutil.wait_procs"
            ), self.assertRaisesRegex(LeakError, "GFlags -ust failed"):
                leak_main(("capture", str(root), str(umdh), "operations", "malformed", "1", "2", "3"))
            self.assertTrue(process.killed)

            elevation = root / "gflags-elevation"
            elevation.mkdir()
            elevated = OSError("requires elevation")
            elevated.winerror = 740  # type: ignore[attr-defined]
            calls = 0

            def unavailable(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                nonlocal calls
                if Path(argv[0]) == gflags:
                    calls += 1
                    if calls == 1:
                        return subprocess.CompletedProcess(
                            argv, 0, "Current Registry Settings for leak-probe.exe executable are: 00000000"
                        )
                    raise elevated
                Path(argv[-1].removeprefix("-f:")).write_text("BackTrace\n", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "")

            process = FakeProbe([
                "OBSERVER_LEAK_PROBE|READY|pid=77|mode=operations|configuration=Release|scenarios=malformed",
                *(f"OBSERVER_LEAK_PROBE|SNAPSHOT|{label}|pid=77|" for label in ("baseline", "window-1", "window-2", "window-3")),
                "OBSERVER_LEAK_PROBE|DONE|pid=77|",
            ])
            with self.output(elevation), mock.patch(
                "core.leak.subprocess.run", side_effect=unavailable
            ), mock.patch("core.leak.psutil.Popen", return_value=process) as popen:
                leak_main(("capture", str(root), str(umdh), "operations", "malformed", "1", "2", "3"))
            popen.assert_called_once()

            existing = mock.Mock(returncode=0, stdout="Current Registry Settings for leak-probe.exe executable are: 00001000")
            with mock.patch("core.leak.subprocess.run", return_value=existing) as invoked:
                self.assertFalse(leak._enable_stack_traces(gflags, "leak-probe.exe"))
            invoked.assert_called_once()

    def test_stack_trace_activation_handles_missing_and_invalid_gflags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.exe"
            self.assertFalse(leak._enable_stack_traces(missing, "probe.exe"))

            gflags = root / "gflags.exe"
            gflags.touch()
            for result, message in (
                (mock.Mock(returncode=1, stdout="denied"), "query failed"),
                (mock.Mock(returncode=0, stdout="unexpected"), "unrecognized"),
            ):
                with self.subTest(message=message), mock.patch(
                    "core.leak.subprocess.run", return_value=result
                ), self.assertRaisesRegex(LeakError, message):
                    leak._enable_stack_traces(gflags, "probe.exe")

            query = mock.Mock(returncode=0, stdout="Current Registry Settings for probe.exe executable are: 00000000")
            unexpected = OSError("unexpected launch failure")
            unexpected.winerror = 5  # type: ignore[attr-defined]
            with mock.patch("core.leak.subprocess.run", side_effect=(query, unexpected)), self.assertRaises(OSError):
                leak._enable_stack_traces(gflags, "probe.exe")

    def test_diff_and_judge_preserve_growth_evidence_and_reject_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_text = "\n".join(("+ 500 (x) 1 allocs BackTrace ABC", "Total increase == 500"))

            def compare(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                Path(argv[-1].removeprefix("-f:")).write_text(report_text, encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "")

            with self.output(root), mock.patch("core.leak.subprocess.run", side_effect=compare):
                leak_main(("diff", "umdh", str(root), "window-1", "before", "after"))
            first = root / "diff.json"
            self.assertEqual(500, json.loads(first.read_text())["positiveStacks"]["ABC"])

            paths = []
            for label, total in (("window-1", 500), ("window-2", 500), ("overall", 1001)):
                path = root / f"{label}.json"
                path.write_text(json.dumps({"label": label, "totalIncrease": total, "positiveStacks": {"ABC": 500}}))
                paths.extend((label, str(path)))
            with self.output(root), self.assertRaisesRegex(LeakError, "sustained"):
                leak_main(("judge", "operations", "malformed", "1", "2", "3", "100", *paths))
            self.assertFalse(json.loads((root / "summary.json").read_text())["passed"])
            with self.output(root):
                leak_main(("summarize", "operations", "malformed", "1", "2", "3", "100", *paths))
                with self.assertRaisesRegex(LeakError, "sustained"):
                    leak_main(("gate", str(root / "summary.json")))

            for path in (root / "window-1.json", root / "window-2.json", root / "overall.json"):
                document = json.loads(path.read_text())
                document["totalIncrease"] = 0
                document["positiveStacks"] = {}
                path.write_text(json.dumps(document))
            with self.output(root):
                leak_main(("judge", "operations", "malformed", "1", "2", "3", "100", *paths))
                leak_main(("gate", str(root / "summary.json")))
            self.assertTrue(json.loads((root / "summary.json").read_text())["passed"])

    def test_worker_rejects_invalid_arguments_protocol_and_umdh_evidence(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(LeakError, "OBSERVER_OUT_DIR"):
            leak._output()
        with self.assertRaisesRegex(LeakError, "setup expects"):
            leak_main(("setup",))
        with self.assertRaisesRegex(LeakError, "not found"):
            leak_main(("preflight", "missing", "operations", "malformed"))
        for mode, scenario in (("bad", "malformed"), ("operations", "bad")):
            with self.subTest(selection=(mode, scenario)), self.assertRaisesRegex(LeakError, "selection"):
                leak._selection(mode, scenario)
        for value, message in (("many", "integer"), ("0", "at least")):
            with self.subTest(value=value), self.assertRaisesRegex(LeakError, message):
                leak._count(value, "rounds")
        with self.assertRaisesRegex(LeakError, "requested process"):
            leak._ready(
                "OBSERVER_LEAK_PROBE|READY|pid=2|mode=operations|configuration=Release|scenarios=malformed",
                "operations", "malformed", 1,
            )
        with self.assertRaisesRegex(LeakError, "expected leak action"):
            leak_main(())
        with self.assertRaisesRegex(LeakError, "expected leak action"):
            leak_main(("unknown",))
        with mock.patch.object(sys, "argv", ["leak.py", "unknown"]), self.assertRaises(LeakError):
            leak_main()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invalid = root / "summary.json"
            with self.output(root):
                with self.assertRaisesRegex(LeakError, "summary is invalid"):
                    leak_main(("gate", str(invalid)))
                invalid.write_text('{"passed":"yes"}', encoding="utf-8")
                with self.assertRaisesRegex(LeakError, "summary is invalid"):
                    leak_main(("gate", str(invalid)))

        process = FakeProbe([])
        process.descendant.kill.side_effect = leak.psutil.NoSuchProcess(88)
        with mock.patch("core.leak.psutil.wait_procs") as waited:
            leak._kill_tree(process)  # type: ignore[arg-type]
        waited.assert_called_once()
        self.assertTrue(process.killed)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "snapshot.txt"
            bad_results = (
                (subprocess.CompletedProcess([], 2, "bad"), True, ""),
                (subprocess.CompletedProcess([], 1, "bad"), True, "wrong"),
                (subprocess.CompletedProcess([], 1, "bad"), False, "BackTrace"),
                (subprocess.CompletedProcess([], 0, ""), False, "database is full BackTrace"),
                (subprocess.CompletedProcess([], 0, ""), False, "empty"),
            )
            for result, baseline, content in bad_results:
                if content:
                    destination.write_text(content, encoding="utf-8")
                elif destination.exists():
                    destination.unlink()
                with self.subTest(snapshot=(result.returncode, baseline, content)), mock.patch(
                    "core.leak.subprocess.run", return_value=result
                ), self.assertRaises(LeakError):
                    leak._snapshot(Path("umdh"), 1, destination, baseline)

            report = root / "report.txt"

            def diff_result(returncode: int, content: str | None):
                def run(_argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                    if content is not None:
                        report.write_text(content, encoding="utf-8")
                    elif report.exists():
                        report.unlink()
                    return subprocess.CompletedProcess([], returncode, "bad")

                return run

            with self.output(root):
                for returncode, content, message in (
                    (2, None, "failed"),
                    (0, None, "failed"),
                    (0, "no totals", "no total"),
                ):
                    with self.subTest(diff=(returncode, content)), mock.patch(
                        "core.leak.subprocess.run", side_effect=diff_result(returncode, content)
                    ), self.assertRaisesRegex(LeakError, message):
                        leak_main(("diff", "umdh", str(root), "window-1", "before", "after"))
                with mock.patch(
                    "core.leak.subprocess.run",
                    side_effect=diff_result(0, "Total decrease == 7"),
                ):
                    leak_main(("diff", "umdh", str(root), "window-1", "before", "after"))
                self.assertEqual(-7, json.loads((root / "diff.json").read_text())["totalIncrease"])

    def test_judge_rejects_shape_and_label_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            documents = []
            for label in ("window-1", "window-2", "overall"):
                path = root / f"{label}.json"
                path.write_text(json.dumps({"label": label, "totalIncrease": 0, "positiveStacks": {}}))
                documents.extend((label, str(path)))
            with self.output(root):
                for arguments in (
                    ("judge", "operations"),
                    ("judge", "operations", "malformed", "1", "2", "3", "0", "window-1", "x", "extra"),
                ):
                    with self.subTest(arguments=arguments), self.assertRaisesRegex(LeakError, "judge expects"):
                        leak_main(arguments)
                with self.assertRaisesRegex(LeakError, "labels"):
                    leak_main(("judge", "operations", "malformed", "1", "2", "3", "0", "wrong", *documents[1:]))
                first = Path(documents[1])
                value = json.loads(first.read_text())
                value["label"] = "wrong"
                first.write_text(json.dumps(value))
                with self.assertRaisesRegex(LeakError, "labels"):
                    leak_main(("judge", "operations", "malformed", "1", "2", "3", "0", *documents))

        with (
            mock.patch.object(sys, "argv", ["leak.py", "setup"]),
            self.assertWarnsRegex(RuntimeWarning, "core.leak"),
            self.assertRaises(RuntimeError),
        ):
            runpy.run_module("core.leak", run_name="__main__")


if __name__ == "__main__":
    unittest.main(verbosity=2)

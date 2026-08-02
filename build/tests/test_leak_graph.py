from __future__ import annotations

import hashlib
from dataclasses import replace
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
from graphs.audit import BinaryArtifact  # noqa: E402
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
    ) -> tuple[Path, Graph, tuple[BinaryArtifact, ...], Path]:
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
        paths = BuildPaths(repository)
        artifacts = tuple(
            BinaryArtifact(
                "x64",
                name,
                producer,
                paths.cas(producer.uid, producer.name).output
                / ("leak-probe.exe" if name == "leak-probe" else f"{name}.so"),
            )
            for name, producer in zip(
                ("leak-probe", "renpy", "rpgmaker", "zanzarah"), builds, strict=True
            )
        )
        tools = root / "tools"
        tools.mkdir()
        umdh = tools / "umdh.exe"
        umdh.touch()
        return repository, upstream, artifacts, umdh

    def build_graph(self, root: Path, **options: object) -> Graph:
        repository, upstream, artifacts, umdh = self.fixture(root)
        return leak_graph(
            repository,
            upstream,
            artifacts,
            umdh=umdh,
            umdh_identity={"version": "10.0"},
            run_nonce="run-42",
            **options,
        )

    def test_default_graph_has_one_shared_setup_and_fourteen_independent_branches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            graph = self.build_graph(Path(temporary), jobs=7, session_jobs=3, diff_jobs=5)

        leak_nodes = tuple(item for item in graph.nodes if item.name.startswith("leak-"))
        self.assertEqual(85, len(leak_nodes))
        self.assertEqual(
            graph.pools,
            {"build": 4, "leak": 7, "leak-session": 3, "leak-diff": 5},
        )
        self.assertEqual(
            graph.targets,
            tuple(
                f"leak-judge-{mode}-{scenario}"
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
                judge = graph.node(f"leak-judge-{stem}")

                self.assertEqual(preflight.inputs, (setup.name,))
                self.assertEqual(capture.inputs, (preflight.name,))
                self.assertTrue(all(item.inputs == (capture.name,) for item in (*adjacent, overall)))
                self.assertEqual(judge.inputs, tuple(item.name for item in (*adjacent, overall)))
                self.assertEqual(
                    (preflight.pool, capture.pool, adjacent[0].pool, judge.pool),
                    ("leak", "leak-session", "leak-diff", "leak"),
                )
                self.assertEqual(preflight.command.argv[-2:], (mode, scenario))
                self.assertEqual(capture.command.argv[6:8], (mode, scenario))
                self.assertEqual((overall.command.argv[3], overall.command.argv[6]), ("diff", "overall"))
                self.assertEqual(judge.command.argv[9], "0")

    def test_measurement_options_expand_snapshot_diffs_and_are_signed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph = self.build_graph(
                root, warmup=2, iterations=3, windows=4, tolerance_bytes=17
            )

        self.assertEqual(99, len(tuple(item for item in graph.nodes if item.name.startswith("leak-"))))
        capture = graph.node("leak-capture-operations-small-success")
        self.assertEqual(capture.command.argv[-3:], ("2", "3", "4"))
        judge = graph.node("leak-judge-operations-small-success")
        self.assertEqual(
            judge.inputs,
            (
                "leak-diff-operations-small-success-window-1",
                "leak-diff-operations-small-success-window-2",
                "leak-diff-operations-small-success-window-3",
                "leak-diff-operations-small-success-overall",
            ),
        )
        self.assertEqual(judge.command.argv[3:10], ("judge", "operations", "small-success", "2", "3", "4", "17"))

    def test_run_and_tool_identities_invalidate_only_the_measurement_partition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, artifacts, umdh = self.fixture(root)

            def graph(
                *,
                nonce: str = "run-a",
                umdh_version: str = "10.0",
                tolerance: int = 4096,
            ) -> Graph:
                return leak_graph(
                    repository,
                    upstream,
                    artifacts,
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
            measured = current.name.startswith(("leak-capture-", "leak-diff-", "leak-judge-"))
            judged = current.name.startswith("leak-judge-")
            self.assertEqual(measured, current.uid != rerun.node(current.name).uid, current.name)
            self.assertEqual(measured, current.uid != new_umdh.node(current.name).uid, current.name)
            self.assertEqual(judged, current.uid != new_tolerance.node(current.name).uid, current.name)

    def test_rejects_invalid_tools_artifacts_pools_and_measurement_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, upstream, artifacts, umdh = self.fixture(root)

            def invoke(
                selected: tuple[BinaryArtifact, ...] = artifacts,
                *,
                source: Graph = upstream,
                umdh_path: Path = umdh,
                nonce: object = "run-42",
                warmup: object = 8,
                iterations: object = 100,
                windows: object = 3,
                tolerance: object = 4096,
                jobs: object = 4,
                session_jobs: object = 2,
                diff_jobs: object = 4,
            ) -> Graph:
                return leak_graph(
                    repository,
                    source,
                    selected,
                    umdh=umdh_path,
                    umdh_identity={"version": "10.0"},
                    run_nonce=nonce,  # type: ignore[arg-type]
                    warmup=warmup,  # type: ignore[arg-type]
                    iterations=iterations,  # type: ignore[arg-type]
                    windows=windows,  # type: ignore[arg-type]
                    tolerance_bytes=tolerance,  # type: ignore[arg-type]
                    jobs=jobs,  # type: ignore[arg-type]
                    session_jobs=session_jobs,  # type: ignore[arg-type]
                    diff_jobs=diff_jobs,  # type: ignore[arg-type]
                )

            for capacities in ((True, 2, 4), (4, "two", 4), (4, 2, 0)):
                with self.subTest(capacities=capacities), self.assertRaisesRegex(
                    ValueError, "capacities"
                ):
                    invoke(jobs=capacities[0], session_jobs=capacities[1], diff_jobs=capacities[2])
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
            with self.assertRaisesRegex(ValueError, "missing"):
                invoke(artifacts[:-1])
            with self.assertRaisesRegex(ValueError, "duplicate"):
                invoke((*artifacts, artifacts[0]))
            for invalid in (
                replace(artifacts[0], architecture="x86"),
                replace(artifacts[0], module="unknown"),
            ):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "invalid"):
                    invoke((invalid, *artifacts[1:]))

            missing_gate = Graph(
                tuple(node for node in upstream.nodes if node.name != "audit-binskim-x64-renpy"),
                upstream.targets,
                upstream.pools,
            )
            with self.assertRaisesRegex(ValueError, "audit gates"):
                invoke(source=missing_gate)

            impostor = Node(
                artifacts[0].producer.name,
                artifacts[0].producer.uid,
                "build",
                Command(("other-build.exe",)),
                artifacts[0].producer.inputs,
            )
            with self.assertRaisesRegex(ValueError, "producer CAS"):
                invoke((replace(artifacts[0], producer=impostor), *artifacts[1:]))
            unknown = node("unknown-producer")
            with self.assertRaisesRegex(ValueError, "producer CAS"):
                invoke((replace(artifacts[0], producer=unknown), *artifacts[1:]))

            paths = BuildPaths(repository)
            producer_output = paths.cas(artifacts[0].producer.uid, artifacts[0].producer.name).output
            bad_paths = (
                producer_output / "wrong-name.exe",
                paths.cas(artifacts[1].producer.uid, artifacts[1].producer.name).output
                / "leak-probe.exe",
            )
            for bad_path in bad_paths:
                with self.subTest(bad_path=bad_path), self.assertRaisesRegex(
                    ValueError, "producer CAS"
                ):
                    invoke((replace(artifacts[0], path=bad_path), *artifacts[1:]))

            matching = Graph(
                upstream.nodes,
                upstream.targets,
                {"build": 4, "leak": 4, "leak-session": 2, "leak-diff": 4},
            )
            self.assertEqual(4, invoke(source=matching).pools["leak"])
            conflicting = Graph(upstream.nodes, upstream.targets, {"build": 4, "leak": 1})
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
            lines = [
                "probe startup noise",
                "OBSERVER_LEAK_PROBE|READY|pid=77|mode=operations|configuration=Release|scenarios=malformed",
                *(f"OBSERVER_LEAK_PROBE|SNAPSHOT|{label}|pid=77|completed_operations=1" for label in ("baseline", "window-1", "window-2", "window-3")),
                "OBSERVER_LEAK_PROBE|DONE|pid=77|completed_operations=4",
            ]
            process = FakeProbe(lines)

            def snapshot(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                Path(argv[-1].removeprefix("-f:")).write_text("BackTrace 1\n", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "")

            with self.output(root), mock.patch("core.leak.psutil.Popen", return_value=process) as popen, mock.patch(
                "core.leak.subprocess.run", side_effect=snapshot
            ):
                leak_main(("capture", str(root), str(umdh), "operations", "malformed", "1", "2", "3"))

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

            for path in (root / "window-1.json", root / "window-2.json", root / "overall.json"):
                document = json.loads(path.read_text())
                document["totalIncrease"] = 0
                document["positiveStacks"] = {}
                path.write_text(json.dumps(document))
            with self.output(root):
                leak_main(("judge", "operations", "malformed", "1", "2", "3", "100", *paths))
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

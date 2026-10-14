"""Behavioural tests for the ``build/leaks.py`` UMDH worker CLI.

The worker owns the irreducible live-measurement logic: staging the built binaries, capturing
windows under UMDH, comparing them and failing on repeated growth.  These tests exercise the
real setup and measure stages with the probe and UMDH faked, so the evidence and the
live-process cleanup stay covered without launching a real debugger.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import leaks  # noqa: E402
from leaks import LeakError, main as leak_main  # noqa: E402


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
            raise leaks.psutil.TimeoutExpired(timeout, self.pid)
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
        return mock.patch.dict(os.environ, {"UNPACKRAT_OUT_DIR": str(root)}, clear=False)

    @staticmethod
    def probe_lines(windows: int = 3) -> list[str]:
        labels = ("baseline", *(f"window-{index}" for index in range(1, windows + 1)))
        return [
            "UNPACKRAT_LEAK_PROBE|READY|pid=77|mode=operations|configuration=Release|scenarios=malformed",
            *(f"UNPACKRAT_LEAK_PROBE|SNAPSHOT|{label}|pid=77|" for label in labels),
            "UNPACKRAT_LEAK_PROBE|DONE|pid=77|",
        ]

    def test_setup_stages_binaries_and_symbols(self) -> None:
        self.assertEqual(leaks.BINARIES[-1], "godot.so")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "out"
            output.mkdir()
            sources = []
            for index, name in enumerate(leaks.BINARIES):
                directory = root / str(index)
                directory.mkdir()
                source = directory / name
                source.write_bytes(name.encode())
                (directory / f"{index}.pdb").write_bytes(bytes([index]))
                sources.append(source)
            with self.output(output):
                self.assertEqual(0, leak_main(("setup", *(str(path) for path in sources))))
            self.assertTrue(all((output / name).is_file() for name in leaks.BINARIES))
            self.assertTrue(all((output / f"{index}.pdb").is_file() for index in range(len(leaks.BINARIES))))

            with self.output(output), self.assertRaisesRegex(LeakError, "not found"):
                leak_main(("setup", *(str(path) for path in (*sources[:-1], root / "missing"))))

    def test_measure_captures_diffs_and_judges_in_one_worker_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "leak-probe.exe").touch()
            umdh = root / "umdh.exe"
            umdh.touch()
            gflags = root / "gflags.exe"
            gflags.touch()
            output = root / "evidence"
            output.mkdir()
            process = FakeProbe([
                "probe startup noise",
                "UNPACKRAT_LEAK_PROBE|READY|pid=77|mode=operations|configuration=Release|scenarios=malformed",
                *(f"UNPACKRAT_LEAK_PROBE|SNAPSHOT|{label}|pid=77|completed_operations=1" for label in ("baseline", "window-1", "window-2", "window-3")),
                "UNPACKRAT_LEAK_PROBE|DONE|pid=77|completed_operations=4",
            ])

            def snapshot(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                if Path(argv[0]) == gflags:
                    settings = "Current Registry Settings for leak-probe.exe executable are: 00000000" if len(argv) == 3 else ""
                    return subprocess.CompletedProcess(argv, 0, settings)
                self.assertEqual(str(root), _options["env"]["_NT_SYMBOL_PATH"])
                self.assertEqual("1", _options["env"]["OANOCACHE"])
                Path(argv[-1].removeprefix("-f:")).write_text(
                    "+ 500 (x) 1 allocs BackTrace ABC\nTotal increase == 500\n"
                    if "-d" in argv else "BackTrace 1\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(argv, 0, "")

            with self.output(output), mock.patch("leaks.psutil.Popen", return_value=process) as popen, mock.patch(
                "leaks.subprocess.run", side_effect=snapshot
            ) as invoked, self.assertRaisesRegex(LeakError, "sustained"):
                leak_main(("measure", str(root), str(umdh), "operations", "malformed", "1", "2", "3", "0"))

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
            self.assertEqual(
                "continue|baseline\ncontinue|window-1\ncontinue|window-2\ncontinue|window-3\n",
                process.stdin.getvalue(),
            )
            self.assertTrue(process.stdin.was_closed)
            self.assertTrue(process.stdout.was_closed)
            self.assertTrue((output / "probe.stderr.log").is_file())
            self.assertEqual(
                sorted(item.name for item in (output / "diffs").iterdir()),
                ["overall.txt", "window-1.txt", "window-2.txt"],
            )
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual([500, 500], summary["totalGrowthByWindow"])
            self.assertEqual(500, summary["overallGrowthBytes"])
            self.assertEqual(["ABC"], summary["repeatedGrowingStacks"])
            self.assertIs(False, summary["passed"])
            self.assertEqual(
                (1, 2, 3, 0),
                (summary["warmupRounds"], summary["iterationsPerWindow"], summary["windows"], summary["toleranceBytes"]),
            )

    def test_measure_publishes_a_pass_when_growth_stays_within_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "leak-probe.exe").touch()
            umdh = root / "umdh.exe"
            umdh.touch()
            output = root / "evidence"
            output.mkdir()

            def runner(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                destination = Path(argv[-1].removeprefix("-f:"))
                destination.write_text("Total decrease == 7\n" if "-d" in argv else "BackTrace\n", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "")

            with self.output(output), mock.patch(
                "leaks.psutil.Popen", return_value=FakeProbe(self.probe_lines())
            ), mock.patch("leaks.subprocess.run", side_effect=runner):
                leak_main(("measure", str(root), str(umdh), "operations", "malformed", "1", "2", "3", "100"))
            summary = json.loads((output / "summary.json").read_text())
            self.assertIs(True, summary["passed"])

    def test_capture_kills_the_probe_tree_on_protocol_timeout_or_exit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "leak-probe.exe").touch()
            umdh = root / "umdh.exe"
            umdh.touch()
            (root / "gflags.exe").touch()
            cases = (
                FakeProbe(["UNPACKRAT_LEAK_PROBE|READY|pid=77|mode=operations|configuration=Release|scenarios=malformed"]),
                FakeProbe([
                    "UNPACKRAT_LEAK_PROBE|READY|pid=77|mode=operations|configuration=Release|scenarios=malformed",
                    "UNPACKRAT_LEAK_PROBE|SNAPSHOT|baseline|pid=12|",
                ]),
                FakeProbe(self.probe_lines(), timeout=True),
                FakeProbe(self.probe_lines(), result=5),
            )

            def snapshot(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                if Path(argv[0]).name == "gflags.exe":
                    output = "Current Registry Settings for leak-probe.exe executable are: 00000000" if len(argv) == 3 else ""
                    return subprocess.CompletedProcess(argv, 0, output)
                Path(argv[-1].removeprefix("-f:")).write_text("BackTrace\n", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "")

            for index, process in enumerate(cases):
                output = root / f"output-{index}"
                output.mkdir()
                with self.subTest(process=index), self.output(output), mock.patch(
                    "leaks.psutil.Popen", return_value=process
                ), mock.patch("leaks.psutil.wait_procs"), mock.patch(
                    "leaks.subprocess.run", side_effect=snapshot
                ), self.assertRaises(LeakError):
                    leak_main(("measure", str(root), str(umdh), "operations", "malformed", "1", "2", "3", "0"))
                if process.timeout or process.result == 0:
                    self.assertTrue(process.killed)

            rejected = root / "gflags-rejected"
            rejected.mkdir()
            failure = subprocess.CompletedProcess([], 1, "access denied")

            def reject(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                if len(argv) == 3:
                    return subprocess.CompletedProcess(argv, 0, "Current Registry Settings for leak-probe.exe executable are: 00000000")
                return failure

            with self.output(rejected), mock.patch(
                "leaks.subprocess.run", side_effect=reject
            ), mock.patch("leaks.psutil.Popen") as popen, self.assertRaisesRegex(LeakError, "GFlags \\+ust failed"):
                leak_main(("measure", str(root), str(umdh), "operations", "malformed", "1", "2", "3", "0"))
            popen.assert_not_called()

            cleanup = root / "gflags-cleanup"
            cleanup.mkdir()

            def reject_cleanup(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                output = "Current Registry Settings for leak-probe.exe executable are: 00000000" if len(argv) == 3 else ""
                return failure if argv[-1] == "-ust" else subprocess.CompletedProcess(argv, 0, output)

            process = FakeProbe([])
            with self.output(cleanup), mock.patch(
                "leaks.subprocess.run", side_effect=reject_cleanup
            ), mock.patch("leaks.psutil.Popen", return_value=process), mock.patch(
                "leaks.psutil.wait_procs"
            ), self.assertRaisesRegex(LeakError, "GFlags -ust failed"):
                leak_main(("measure", str(root), str(umdh), "operations", "malformed", "1", "2", "3", "0"))
            self.assertTrue(process.killed)

    def test_capture_propagates_a_spawn_failure_without_touching_a_probe(self) -> None:
        # A probe that never launches leaves no process to reap: the failure must propagate
        # unchanged, the cleanup must skip the absent process, and no probe object may be killed.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "leak-probe.exe").touch()
            umdh = root / "umdh.exe"
            umdh.touch()
            (root / "gflags.exe").touch()
            output = root / "evidence"
            output.mkdir()

            def preset(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                # Stack traces are already live, so capture reaches the probe launch directly.
                return subprocess.CompletedProcess(
                    argv, 0, "Current Registry Settings for leak-probe.exe executable are: 00001000"
                )

            failure = OSError("the probe could not be launched")
            with self.output(output), mock.patch(
                "leaks.subprocess.run", side_effect=preset
            ), mock.patch("leaks.psutil.Popen", side_effect=failure) as popen, self.assertRaises(
                OSError
            ) as raised:
                leak_main(("measure", str(root), str(umdh), "operations", "malformed", "1", "2", "3", "0"))

            self.assertIs(raised.exception, failure)
            popen.assert_called_once()
            self.assertTrue((output / "probe.stderr.log").is_file())

    def test_gflags_elevation_and_preset_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "leak-probe.exe").touch()
            umdh = root / "umdh.exe"
            umdh.touch()
            (root / "gflags.exe").touch()
            elevated = OSError("requires elevation")
            elevated.winerror = 740  # type: ignore[attr-defined]
            calls = 0

            def unavailable(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                nonlocal calls
                if Path(argv[0]).name == "gflags.exe":
                    calls += 1
                    if calls == 1:
                        return subprocess.CompletedProcess(argv, 0, "Current Registry Settings for leak-probe.exe executable are: 00000000")
                    raise elevated
                Path(argv[-1].removeprefix("-f:")).write_text(
                    "Total increase == 0\n" if "-d" in argv else "BackTrace\n", encoding="utf-8"
                )
                return subprocess.CompletedProcess(argv, 0, "")

            elevation = root / "gflags-elevation"
            elevation.mkdir()
            with self.output(elevation), mock.patch(
                "leaks.subprocess.run", side_effect=unavailable
            ), mock.patch("leaks.psutil.Popen", return_value=FakeProbe(self.probe_lines())) as popen:
                leak_main(("measure", str(root), str(umdh), "operations", "malformed", "1", "2", "3", "0"))
            popen.assert_called_once()

            def already_enabled(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                if Path(argv[0]).name == "gflags.exe":
                    return subprocess.CompletedProcess(argv, 0, "Current Registry Settings for leak-probe.exe executable are: 00001000")
                Path(argv[-1].removeprefix("-f:")).write_text(
                    "Total increase == 0\n" if "-d" in argv else "BackTrace\n", encoding="utf-8"
                )
                return subprocess.CompletedProcess(argv, 0, "")

            preset = root / "gflags-preset"
            preset.mkdir()
            with self.output(preset), mock.patch(
                "leaks.subprocess.run", side_effect=already_enabled
            ) as invoked, mock.patch("leaks.psutil.Popen", return_value=FakeProbe(self.probe_lines())):
                leak_main(("measure", str(root), str(umdh), "operations", "malformed", "1", "2", "3", "0"))
            self.assertEqual(
                [call.args[0] for call in invoked.call_args_list if Path(call.args[0][0]).name == "gflags.exe"],
                [[str(root / "gflags.exe"), "/i", "leak-probe.exe"]],
            )

            existing = mock.Mock(returncode=0, stdout="Current Registry Settings for leak-probe.exe executable are: 00001000")
            with mock.patch("leaks.subprocess.run", return_value=existing) as invoked:
                self.assertEqual((False, True), leaks._enable_stack_traces(root / "gflags.exe", "leak-probe.exe"))
            invoked.assert_called_once()

    def test_stack_trace_activation_handles_missing_and_invalid_gflags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.exe"
            self.assertEqual((False, False), leaks._enable_stack_traces(missing, "probe.exe"))

            gflags = root / "gflags.exe"
            gflags.touch()

            denied = OSError("requires elevation")
            denied.winerror = 740  # type: ignore[attr-defined]
            with mock.patch("leaks.subprocess.run", side_effect=denied) as blocked:
                self.assertEqual((False, False), leaks._enable_stack_traces(gflags, "probe.exe"))
            blocked.assert_called_once()

            refused = OSError("access denied")
            refused.winerror = 5  # type: ignore[attr-defined]
            with mock.patch("leaks.subprocess.run", side_effect=refused), self.assertRaises(OSError):
                leaks._enable_stack_traces(gflags, "probe.exe")

            absent = mock.Mock(returncode=0, stdout="No Registry Settings for probe.exe executable")
            changed = mock.Mock(returncode=0, stdout="")
            with mock.patch("leaks.subprocess.run", side_effect=(absent, changed)):
                self.assertEqual((True, True), leaks._enable_stack_traces(gflags, "probe.exe"))

            dual_view = mock.Mock(
                returncode=0,
                stdout="Current Registry Settings for probe.exe executable are: 00000000 : 00000000",
            )
            with mock.patch("leaks.subprocess.run", side_effect=(dual_view, changed)):
                self.assertEqual((True, True), leaks._enable_stack_traces(gflags, "probe.exe"))

            for result, message in (
                (mock.Mock(returncode=1, stdout="denied"), "query failed"),
                (mock.Mock(returncode=0, stdout="unexpected"), "unrecognized"),
            ):
                with self.subTest(message=message), mock.patch(
                    "leaks.subprocess.run", return_value=result
                ), self.assertRaisesRegex(LeakError, message):
                    leaks._enable_stack_traces(gflags, "probe.exe")

            query = mock.Mock(returncode=0, stdout="Current Registry Settings for probe.exe executable are: 00000000")
            unexpected = OSError("unexpected launch failure")
            unexpected.winerror = 5  # type: ignore[attr-defined]
            with mock.patch("leaks.subprocess.run", side_effect=(query, unexpected)), self.assertRaises(OSError):
                leaks._enable_stack_traces(gflags, "probe.exe")

    def test_measure_rejects_unusable_umdh_comparisons(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "leak-probe.exe").touch()
            umdh = root / "umdh.exe"
            umdh.touch()
            failures = ((2, "any", "failed"), (0, None, "failed"), (0, "no totals", "no total"))
            for index, (returncode, content, message) in enumerate(failures):
                output = root / f"unusable-{index}"
                output.mkdir()

                def compare(
                    argv: list[str], *, _code: int = returncode,
                    _content: str | None = content, **_options: object,
                ) -> subprocess.CompletedProcess[str]:
                    destination = Path(argv[-1].removeprefix("-f:"))
                    if "-d" not in argv:
                        destination.write_text("BackTrace\n", encoding="utf-8")
                        return subprocess.CompletedProcess(argv, 0, "")
                    if _content is not None:
                        destination.write_text(_content, encoding="utf-8")
                    return subprocess.CompletedProcess(argv, _code, "bad")

                with self.subTest(message=message), self.output(output), mock.patch(
                    "leaks.psutil.Popen", return_value=FakeProbe(self.probe_lines())
                ), mock.patch("leaks.subprocess.run", side_effect=compare), self.assertRaisesRegex(LeakError, message):
                    leak_main(("measure", str(root), str(umdh), "operations", "malformed", "1", "2", "3", "0"))

    def test_worker_rejects_invalid_arguments_protocol_and_umdh_evidence(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(LeakError, "UNPACKRAT_OUT_DIR"):
            leaks._output()
        with self.assertRaisesRegex(LeakError, "setup expects"):
            leak_main(("setup",))
        with self.assertRaisesRegex(LeakError, "measure expects"):
            leak_main(("measure",))
        for mode, scenario in (("bad", "malformed"), ("operations", "bad")):
            with self.subTest(selection=(mode, scenario)), self.assertRaisesRegex(LeakError, "selection"):
                leaks._selection(mode, scenario)
        for value, message in (("many", "integer"), ("0", "at least")):
            with self.subTest(value=value), self.assertRaisesRegex(LeakError, message):
                leaks._count(value, "rounds")
        with self.assertRaisesRegex(LeakError, "requested process"):
            leaks._ready(
                "UNPACKRAT_LEAK_PROBE|READY|pid=2|mode=operations|configuration=Release|scenarios=malformed",
                "operations", "malformed", 1,
            )
        with self.assertRaisesRegex(LeakError, "expected leak action"):
            leak_main(())
        with self.assertRaisesRegex(LeakError, "expected leak action"):
            leak_main(("unknown",))
        with mock.patch.object(sys, "argv", ["leaks.py", "unknown"]), self.assertRaises(LeakError):
            leak_main()

        process = FakeProbe([])
        process.descendant.kill.side_effect = leaks.psutil.NoSuchProcess(88)
        with mock.patch("leaks.psutil.wait_procs") as waited:
            leaks._kill_tree(process)  # type: ignore[arg-type]
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
                    "leaks.subprocess.run", return_value=result
                ), self.assertRaises(LeakError):
                    leaks._snapshot(Path("umdh"), 1, destination, baseline, {})

            missing = root / "nope"
            with self.assertRaisesRegex(LeakError, "leak probe was not found"):
                leaks._file(str(missing), "leak probe")


if __name__ == "__main__":
    unittest.main(verbosity=2)

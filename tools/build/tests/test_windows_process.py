from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import psutil


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))
EXE = str(Path(sys.executable).resolve())

from core.graph import Command  # noqa: E402
from core.windows_process import WindowsProcessRunner  # noqa: E402


class FakeProcess:
    def __init__(
        self,
        events: list[tuple[object, ...]],
        *,
        resume_error: BaseException | None = None,
        communicate_error: BaseException | None = None,
        kill_error: BaseException | None = None,
        exit_code: int = 0,
    ) -> None:
        self._handle = 301
        self.returncode: int | None = None
        self._events = events
        self._resume_error = resume_error
        self._communicate_error = communicate_error
        self._kill_error = kill_error
        self._exit_code = exit_code
        self.communicate_started = threading.Event()
        self.communicate_release = threading.Event()
        self.communicate_release.set()

    def resume(self) -> None:
        self._events.append(("resume",))
        if self._resume_error is not None:
            raise self._resume_error

    def communicate(self, *, input: bytes) -> tuple[None, None]:
        self._events.append(("communicate-start", input))
        self.communicate_started.set()
        self.communicate_release.wait(timeout=5)
        self._events.append(("communicate-done",))
        if self._communicate_error is not None:
            raise self._communicate_error
        self.returncode = self._exit_code
        return None, None

    def kill(self) -> None:
        self._events.append(("kill",))
        self.communicate_release.set()
        if self._kill_error is not None:
            raise self._kill_error

    def wait(self) -> int:
        self._events.append(("wait",))
        self.communicate_release.wait(timeout=5)
        self.returncode = self._exit_code
        return self._exit_code


class FakePopenFactory:
    def __init__(
        self,
        process: FakeProcess,
        *,
        create_error: BaseException | None = None,
    ) -> None:
        self._process = process
        self._create_error = create_error
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> FakeProcess:
        self._process._events.append(("popen",))
        self.calls.append((tuple(argv), kwargs))
        if self._create_error is not None:
            raise self._create_error
        return self._process


class FakeJob:
    def __init__(
        self,
        process: FakeProcess,
        *,
        assign_error: BaseException | None = None,
        close_error: BaseException | None = None,
        terminate_error: BaseException | None = None,
    ) -> None:
        self._process = process
        self._assign_error = assign_error
        self._close_error = close_error
        self._terminate_error = terminate_error
        self.closed = False
        process._events.append(("create-job",))

    def assign_process(self, process_handle: int) -> None:
        self._process._events.append(("assign", process_handle))
        if self._assign_error is not None:
            raise self._assign_error

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._process._events.append(("close-job",))
        if self._close_error is not None:
            raise self._close_error
        self._process.communicate_release.set()

    def terminate(self) -> None:
        self._process._events.append(("terminate-job",))
        self._process.communicate_release.set()
        if self._terminate_error is not None:
            raise self._terminate_error


class WindowsProcessRunnerTests(unittest.IsolatedAsyncioTestCase):
    def patch_runtime(
        self,
        process: FakeProcess,
        *,
        create_error: BaseException | None = None,
        assign_error: BaseException | None = None,
        close_error: BaseException | None = None,
        terminate_error: BaseException | None = None,
    ) -> FakePopenFactory:
        popen = FakePopenFactory(process, create_error=create_error)
        self.enterContext(patch("core.windows_process.psutil.Popen", popen))
        self.enterContext(
            patch(
                "core.windows_process.WindowsJob",
                new=lambda: FakeJob(
                    process,
                    assign_error=assign_error,
                    close_error=close_error,
                    terminate_error=terminate_error,
                ),
            )
        )
        return popen

    async def test_literal_argv_is_assigned_before_public_resume(self) -> None:
        events: list[tuple[object, ...]] = []
        process = FakeProcess(events, exit_code=23)
        popen = self.patch_runtime(process)
        log = io.BytesIO()
        command = Command(
            (EXE, "literal ; & | argument", 'quote"inside'),
            env=(("ZED", "last"), ("Alpha", "first"), ("PATH", "tools")),
            cwd=r"C:\repo\work dir",
            stdin=b"Write-Output 'exact'\r\nexit 23\r\n",
        )

        with patch.dict(
            os.environ, {"HOST": "base", "Path": "host", "zed": "host"}, clear=True
        ):
            exit_code = await WindowsProcessRunner().run(command, log=log)

        self.assertEqual(exit_code, 23)
        argv, options = popen.calls[0]
        self.assertEqual(argv, command.argv)
        self.assertIs(options["stdout"], log)
        self.assertIs(options["stderr"], subprocess.STDOUT)
        self.assertIs(options["stdin"], subprocess.PIPE)
        self.assertIs(options["shell"], False)
        self.assertIs(options["close_fds"], True)
        self.assertEqual(options["executable"], command.argv[0])
        self.assertEqual(options["cwd"], command.cwd)
        self.assertEqual(
            options["env"],
            {
                "Alpha": "first",
                "HOST": "base",
                "PATH": "tools" + os.pathsep + "host",
                "ZED": "last",
            },
        )
        self.assertEqual(options["creationflags"], 0x00000404)
        self.assertLess(events.index(("assign", 301)), events.index(("resume",)))
        self.assertLess(
            events.index(("resume",)),
            events.index(("communicate-start", command.stdin)),
        )
        self.assertEqual(events.count(("close-job",)), 1)
        self.assertNotIn(("kill",), events)

    async def test_executable_must_be_an_absolute_existing_regular_file(self) -> None:
        invalid = ("tool.exe", str(BUILD_ROOT / "missing.exe"), str(BUILD_ROOT))
        events: list[tuple[object, ...]] = []
        process = FakeProcess(events)
        popen = self.patch_runtime(process)
        for executable in invalid:
            with self.subTest(executable=executable):
                with self.assertRaisesRegex(ValueError, "executable"):
                    await WindowsProcessRunner().run(
                        Command((executable,)), log=io.BytesIO()
                    )
        self.assertEqual(events, [])
        self.assertEqual(popen.calls, [])

    async def test_popen_failure_closes_job(self) -> None:
        events: list[tuple[object, ...]] = []
        process = FakeProcess(events)
        self.patch_runtime(process, create_error=OSError("create failed"))

        with self.assertRaisesRegex(OSError, "create failed"):
            await WindowsProcessRunner().run(Command((EXE,)), log=io.BytesIO())

        self.assertEqual(events, [("create-job",), ("popen",), ("close-job",)])

    async def test_assignment_failure_kills_suspended_process_and_reaps(self) -> None:
        events: list[tuple[object, ...]] = []
        process = FakeProcess(events)
        self.patch_runtime(process, assign_error=OSError("assign failed"))

        with self.assertRaisesRegex(OSError, "assign failed"):
            await WindowsProcessRunner().run(Command((EXE,)), log=io.BytesIO())

        self.assertNotIn(("resume",), events)
        self.assertLess(events.index(("close-job",)), events.index(("kill",)))
        self.assertIn(("wait",), events)

    async def test_resume_failure_closes_assigned_job_and_reaps(self) -> None:
        events: list[tuple[object, ...]] = []
        process = FakeProcess(events, resume_error=OSError("resume failed"))
        self.patch_runtime(process)

        with self.assertRaisesRegex(OSError, "resume failed"):
            await WindowsProcessRunner().run(Command((EXE,)), log=io.BytesIO())

        self.assertLess(events.index(("assign", 301)), events.index(("resume",)))
        self.assertLess(events.index(("close-job",)), events.index(("wait",)))
        self.assertNotIn(("kill",), events)

    async def test_cancellation_closes_job_then_waits_for_communicate(self) -> None:
        events: list[tuple[object, ...]] = []
        process = FakeProcess(events)
        process.communicate_release.clear()
        self.patch_runtime(process)
        task = asyncio.create_task(
            WindowsProcessRunner().run(Command((EXE,)), log=io.BytesIO())
        )
        await asyncio.to_thread(process.communicate_started.wait, 5)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertLess(
            events.index(("close-job",)), events.index(("communicate-done",))
        )
        self.assertNotIn(("kill",), events)

    async def test_job_close_failure_terminates_assigned_job_and_is_noted(self) -> None:
        events: list[tuple[object, ...]] = []
        process = FakeProcess(events, communicate_error=OSError("write failed"))
        self.patch_runtime(process, close_error=OSError("job close failed"))

        with self.assertRaisesRegex(OSError, "write failed") as raised:
            await WindowsProcessRunner().run(
                Command((EXE,), stdin=b"recipe"), log=io.BytesIO()
            )

        self.assertLess(
            events.index(("close-job",)), events.index(("terminate-job",))
        )
        self.assertNotIn(("kill",), events)
        self.assertTrue(
            any("job close failed" in note for note in raised.exception.__notes__)
        )

    async def test_success_does_not_hide_job_close_failure(self) -> None:
        events: list[tuple[object, ...]] = []
        process = FakeProcess(events)
        self.patch_runtime(process, close_error=OSError("job close failed"))

        with self.assertRaisesRegex(OSError, "job close failed"):
            await WindowsProcessRunner().run(Command((EXE,)), log=io.BytesIO())

        self.assertLess(
            events.index(("close-job",)), events.index(("terminate-job",))
        )
        self.assertNotIn(("kill",), events)

    async def test_kill_failure_is_noted_on_primary_failure(self) -> None:
        events: list[tuple[object, ...]] = []
        process = FakeProcess(
            events,
            communicate_error=OSError("write failed"),
            kill_error=OSError("kill failed"),
        )
        self.patch_runtime(
            process,
            close_error=OSError("job close failed"),
            terminate_error=OSError("job terminate failed"),
        )

        with self.assertRaisesRegex(OSError, "write failed") as raised:
            await WindowsProcessRunner().run(Command((EXE,)), log=io.BytesIO())

        self.assertLess(events.index(("close-job",)), events.index(("terminate-job",)))
        self.assertLess(events.index(("terminate-job",)), events.index(("kill",)))
        notes = raised.exception.__notes__
        self.assertTrue(any("job close failed" in note for note in notes))
        self.assertTrue(any("job terminate failed" in note for note in notes))
        self.assertTrue(any("kill failed" in note for note in notes))


@unittest.skipUnless(os.name == "nt", "requires Windows Job Objects")
class WindowsProcessRunnerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def command(*arguments: str, stdin: bytes = b"") -> Command:
        return Command(
            (EXE, *arguments),
            env=tuple(os.environ.items()),
            cwd=str(BUILD_ROOT),
            stdin=stdin,
        )

    async def test_real_process_receives_exact_stdin_and_combines_log(self) -> None:
        script = (
            "import sys; data=sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(b'OUT:' + data); "
            "sys.stderr.buffer.write(b'|ERR'); raise SystemExit(23)"
        )
        payload = b"literal ; & | \x00 recipe\r\n"
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "process.log"
            with log_path.open("w+b") as log:
                exit_code = await WindowsProcessRunner().run(
                    self.command("-c", script, stdin=payload), log=log
                )

            self.assertEqual(exit_code, 23)
            self.assertEqual(log_path.read_bytes(), b"OUT:" + payload + b"|ERR")

    async def test_real_cancellation_kills_descendant_process(self) -> None:
        script = (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)']); "
            "print(p.pid, flush=True); time.sleep(300)"
        )
        child: psutil.Process | None = None
        task: asyncio.Task[int] | None = None
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "tree.log"
            try:
                with log_path.open("w+b") as log:
                    task = asyncio.create_task(
                        WindowsProcessRunner().run(
                            self.command("-c", script), log=log
                        )
                    )
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and not task.done():
                        content = log_path.read_text(encoding="ascii").strip()
                        if content:
                            child = psutil.Process(int(content))
                            break
                        await asyncio.sleep(0.05)
                    if child is None:
                        if task.done():
                            await task
                        self.fail("child process pid was not reported")

                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

                gone, alive = psutil.wait_procs([child], timeout=10)
                self.assertEqual(gone, [child])
                self.assertEqual(alive, [])
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                if child is not None and child.is_running():
                    child.kill()
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

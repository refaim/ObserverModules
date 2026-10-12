from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import core.windows_process as windows_process  # noqa: E402
from core.windows_process import WindowsProcessRunner  # noqa: E402


class _Job:
    def __init__(self, *, assign_error=None, close_error=None, terminate_error=None) -> None:
        self._assign_error = assign_error
        self._close_error = close_error
        self._terminate_error = terminate_error
        self.assigned: list[int] = []
        self.closed = 0
        self.terminated = 0

    def assign_process(self, handle: int) -> None:
        if self._assign_error is not None:
            raise self._assign_error
        self.assigned.append(handle)

    def close(self) -> None:
        self.closed += 1
        if self._close_error is not None:
            raise self._close_error

    def terminate(self) -> None:
        self.terminated += 1
        if self._terminate_error is not None:
            raise self._terminate_error


class _Process:
    def __init__(self, *, output: str | None = "", code: int = 0, handle: int = 11,
                 resume_error=None) -> None:
        self._handle = handle
        self._output = output
        self._code = code
        self._resume_error = resume_error
        self.resumed = 0
        self.killed = 0

    def resume(self) -> None:
        self.resumed += 1
        if self._resume_error is not None:
            raise self._resume_error

    def communicate(self):
        return (self._output, None)

    def wait(self) -> int:
        return self._code

    def kill(self) -> None:
        self.killed += 1


class RunnerTests(unittest.TestCase):
    def run_with(self, *, job: _Job, process: object, argv=("tool.exe", "a"),
                 cwd=Path("cwd"), env=None, stdout=None, spawn_error=None):
        with (
            mock.patch.object(windows_process, "WindowsJob", return_value=job),
            mock.patch.object(windows_process.psutil, "Popen") as popen,
        ):
            if spawn_error is not None:
                popen.side_effect = spawn_error
            else:
                popen.return_value = process
            result = WindowsProcessRunner(priority_class=64).run(argv, cwd, env, stdout=stdout)
        return result, popen

    def test_run_assigns_the_job_and_captures_output(self) -> None:
        job = _Job()
        process = _Process(output="hello", code=0)
        result, popen = self.run_with(job=job, process=process, env={"K": "V"})
        self.assertEqual((result.args, result.returncode, result.stdout),
                         (["tool.exe", "a"], 0, "hello"))
        self.assertEqual(job.assigned, [11])
        self.assertEqual(process.resumed, 1)
        self.assertEqual(job.closed, 1)
        self.assertIs(popen.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs["env"]["K"], "V")
        self.assertEqual(popen.call_args.kwargs["env"]["PATH"], os.environ["PATH"])

    def test_run_returns_a_nonzero_exit_code(self) -> None:
        result, _ = self.run_with(job=_Job(), process=_Process(output="", code=3))
        self.assertEqual(result.returncode, 3)

    def test_a_supplied_stdout_stream_is_forwarded(self) -> None:
        stream = mock.MagicMock()
        result, popen = self.run_with(job=_Job(), process=_Process(output=None), stdout=stream)
        self.assertIs(popen.call_args.kwargs["stdout"], stream)
        self.assertIsNone(popen.call_args.kwargs["stderr"])
        self.assertIsNone(result.stdout)

    def test_environment_defaults_to_the_process_environment(self) -> None:
        _, popen = self.run_with(job=_Job(), process=_Process(), env=None)
        self.assertEqual(popen.call_args.kwargs["env"], dict(os.environ))

    def test_priority_class_is_passed_to_the_job(self) -> None:
        with mock.patch.object(windows_process, "WindowsJob") as factory, \
             mock.patch.object(windows_process.psutil, "Popen", return_value=_Process()):
            WindowsProcessRunner(priority_class=128).run(["tool.exe"])
        factory.assert_called_once_with(priority_class=128)

    def test_an_assignment_failure_closes_the_job_and_kills_the_process(self) -> None:
        job = _Job(assign_error=RuntimeError("assign"))
        process = _Process()
        with self.assertRaisesRegex(RuntimeError, "assign"):
            self.run_with(job=job, process=process)
        self.assertEqual(job.closed, 1)
        self.assertEqual(process.killed, 1)

    def test_a_resume_failure_stops_the_owned_tree_without_a_kill(self) -> None:
        job = _Job()
        process = _Process(resume_error=RuntimeError("resume"))
        with self.assertRaisesRegex(RuntimeError, "resume"):
            self.run_with(job=job, process=process)
        self.assertEqual(job.closed, 1)
        self.assertEqual(process.killed, 0)

    def test_a_close_failure_falls_back_to_job_termination(self) -> None:
        job = _Job(close_error=RuntimeError("close"))
        process = _Process()
        with self.assertRaisesRegex(RuntimeError, "close"):
            self.run_with(job=job, process=process)
        self.assertEqual(job.terminated, 1)
        self.assertEqual(process.killed, 0)

    def test_a_close_and_terminate_failure_hard_kills_the_process(self) -> None:
        job = _Job(close_error=RuntimeError("close"), terminate_error=RuntimeError("terminate"))
        process = _Process()
        with self.assertRaisesRegex(RuntimeError, "close") as raised:
            self.run_with(job=job, process=process)
        self.assertEqual(process.killed, 1)
        self.assertTrue(any("terminate" in note for note in raised.exception.__notes__))

    def test_an_assign_and_close_failure_notes_the_cleanup_error(self) -> None:
        job = _Job(assign_error=RuntimeError("assign"), close_error=RuntimeError("close"))
        with self.assertRaisesRegex(RuntimeError, "assign") as raised:
            self.run_with(job=job, process=_Process())
        self.assertTrue(any("close" in note for note in raised.exception.__notes__))

    def test_a_spawn_failure_still_closes_the_job(self) -> None:
        job = _Job()
        with self.assertRaisesRegex(OSError, "cannot spawn"):
            self.run_with(job=job, process=None, spawn_error=OSError("cannot spawn"))
        self.assertEqual(job.closed, 1)


class RealProcessTests(unittest.TestCase):
    def test_the_runner_executes_a_real_child(self) -> None:
        result = WindowsProcessRunner().run(
            [sys.executable, "-c", "print('unpackrat-probe')"],
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("unpackrat-probe", result.stdout)


if __name__ == "__main__":
    unittest.main()

"""Small synchronous Windows process runner owned by a kill-on-close Job.

The direct CLI shells out through the repository validators, but every child is
still launched inside a :class:`core.windows_job.WindowsJob` so that closing the
job terminates the whole process tree.  A cancelled or failed run therefore
leaves no compilers, MSBuild nodes or vcpkg workers behind, exactly as the
retired graph engine did, without any async engine, graph or command protocol:
the runner takes a literal ``argv`` and returns a standard
:class:`subprocess.CompletedProcess`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import os
from pathlib import Path
import subprocess
from typing import IO

import psutil

from core.windows_job import WindowsJob


_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400


def _attempt(errors: list[BaseException], operation: Callable[[], object]) -> bool:
    try:
        operation()
        return True
    except BaseException as error:
        errors.append(error)
        return False


def _stop(
    job: WindowsJob,
    process: psutil.Popen | None,
    assigned: bool,
    errors: list[BaseException],
) -> None:
    """Close the job so its tree dies with it, falling back to a hard kill."""

    closed = _attempt(errors, job.close)
    tree_stopped = assigned and closed
    if assigned and not closed:
        tree_stopped = _attempt(errors, job.terminate)
    if process is not None and not tree_stopped:
        _attempt(errors, process.kill)


def _environment(env: Mapping[str, str] | None) -> dict[str, str]:
    if env is None:
        return dict(os.environ)
    return {**os.environ, **env}


class WindowsProcessRunner:
    """Run one literal argv in a kill-on-close Job and capture its output."""

    def __init__(self, *, priority_class: int | None = None) -> None:
        self._priority_class = priority_class

    def run(
        self,
        argv: Sequence[str],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        *,
        stdout: IO[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [os.fspath(item) for item in argv]
        job = WindowsJob(priority_class=self._priority_class)
        process: psutil.Popen | None = None
        assigned = False
        try:
            process = psutil.Popen(
                command,
                executable=command[0],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if stdout is None else stdout,
                # Capture mode merges stderr into the captured stream, exactly like the
                # repository tool runner; a caller-supplied stream keeps stderr inherited so a
                # parser reading that stream sees only the child's stdout.
                stderr=subprocess.STDOUT if stdout is None else None,
                cwd=None if cwd is None else os.fspath(cwd),
                env=_environment(env),
                close_fds=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT,
            )
            job.assign_process(int(process._handle))
            assigned = True
            process.resume()
            captured, _ = process.communicate()
            returncode = process.wait()
        except BaseException as primary:
            errors: list[BaseException] = []
            _stop(job, process, assigned, errors)
            for error in errors:
                primary.add_note(f"cleanup failure: {error!r}")
            raise
        errors = []
        _stop(job, process, assigned, errors)
        if errors:
            primary, *notes = errors
            for error in notes:
                primary.add_note(f"cleanup failure: {error!r}")
            raise primary
        return subprocess.CompletedProcess(command, returncode, captured)

"""Small psutil-backed Windows process-tree runner."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import os
from pathlib import Path
import subprocess
from typing import BinaryIO

import psutil

from core.graph import Command
from core.windows_job import WindowsJob


_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400


def _attempt(
    errors: list[BaseException], operation: Callable[[], object]
) -> bool:
    try:
        operation()
        return True
    except BaseException as error:
        errors.append(error)
        return False


def _stop(
    job: WindowsJob,
    process: psutil.Popen[bytes] | None,
    assigned: bool,
    errors: list[BaseException],
) -> None:
    closed = _attempt(errors, job.close)
    tree_stopped = assigned and closed
    if assigned and not closed:
        tree_stopped = _attempt(errors, job.terminate)
    if process is not None and not tree_stopped:
        _attempt(errors, process.kill)


async def _reap(
    process: psutil.Popen[bytes],
    communication: asyncio.Task[object] | None,
    errors: list[BaseException],
    primary: BaseException,
) -> None:
    if communication is not None:
        try:
            await asyncio.shield(communication)
        except BaseException as error:
            if error is not primary:
                errors.append(error)
    if process.returncode is None:
        await asyncio.to_thread(_attempt, errors, process.wait)


class WindowsProcessRunner:
    """Run literal argv in a kill-on-close Job and stream exact stdin."""

    async def run(self, command: Command, *, log: BinaryIO) -> int:
        executable = Path(command.argv[0])
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("command executable must be an absolute existing file")

        job = WindowsJob()
        process: psutil.Popen[bytes] | None = None
        communication: asyncio.Task[object] | None = None
        assigned = False
        try:
            environment = {
                key.casefold(): (key, value) for key, value in os.environ.items()
            }
            for key, value in command.env:
                folded = key.casefold()
                if folded == "path" and folded in environment:
                    value += os.pathsep + environment[folded][1]
                environment[folded] = (key, value)
            process = psutil.Popen(
                list(command.argv),
                executable=command.argv[0],
                shell=False,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=command.cwd,
                env=dict(environment.values()),
                close_fds=True,
                creationflags=_CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT,
            )
            job.assign_process(int(process._handle))
            assigned = True
            process.resume()
            communication = asyncio.create_task(
                asyncio.to_thread(process.communicate, input=command.stdin)
            )
            await asyncio.shield(communication)
            if process.returncode is None:
                raise RuntimeError("process completed without an exit code")
            exit_code = process.returncode
        except BaseException as primary:
            errors: list[BaseException] = []
            _stop(job, process, assigned, errors)
            if process is not None:
                await _reap(process, communication, errors, primary)
            for error in errors:
                primary.add_note(f"cleanup failure: {error!r}")
            raise

        errors = []
        _stop(job, process, assigned, errors)
        if errors:
            primary, *cleanup_errors = errors
            for error in cleanup_errors:
                primary.add_note(f"cleanup failure: {error!r}")
            raise primary
        return exit_code

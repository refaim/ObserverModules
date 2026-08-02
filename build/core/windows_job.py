"""Minimal kill-on-close Windows Job Object wrapper."""

from __future__ import annotations

import win32job


class WindowsJob:
    """Own one Job handle until an explicit, observable close."""

    def __init__(self) -> None:
        handle = win32job.CreateJobObject(None, "")
        self._handle = handle
        try:
            information = win32job.QueryInformationJobObject(
                handle, win32job.JobObjectExtendedLimitInformation
            )
            information["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                handle,
                win32job.JobObjectExtendedLimitInformation,
                information,
            )
        except BaseException as error:
            self._handle = None
            try:
                handle.Close()
            except BaseException as cleanup_error:
                error.add_note(f"cleanup failure: {cleanup_error!r}")
            raise

    def assign_process(self, process_handle: int) -> None:
        win32job.AssignProcessToJobObject(self._handle, process_handle)

    def terminate(self) -> None:
        win32job.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        handle = self._handle
        if handle is not None:
            handle.Close()
            self._handle = None

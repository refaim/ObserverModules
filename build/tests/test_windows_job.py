from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import win32job

from core.windows_job import WindowsJob


class FakeHandle:
    def __init__(
        self,
        events: list[tuple[object, ...]],
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self._events = events
        self._close_error = close_error

    def Close(self) -> None:
        self._events.append(("close",))
        if self._close_error is not None:
            raise self._close_error


class FakeWin32Job:
    JobObjectExtendedLimitInformation = (
        win32job.JobObjectExtendedLimitInformation
    )
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )

    def __init__(
        self,
        *,
        configure_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.events: list[tuple[object, ...]] = []
        self.handle = FakeHandle(self.events, close_error=close_error)
        self.configure_error = configure_error
        self.information = {
            "BasicLimitInformation": {"LimitFlags": 0x40},
            "IoInfo": {},
        }

    def CreateJobObject(self, attributes: object, name: str) -> FakeHandle:
        self.events.append(("create", attributes, name))
        return self.handle

    def QueryInformationJobObject(
        self, handle: FakeHandle, information_class: int
    ) -> dict[str, object]:
        self.events.append(("query", handle, information_class))
        return self.information

    def SetInformationJobObject(
        self,
        handle: FakeHandle,
        information_class: int,
        information: dict[str, object],
    ) -> None:
        self.events.append(("set", handle, information_class, information))
        if self.configure_error is not None:
            raise self.configure_error

    def AssignProcessToJobObject(
        self, handle: FakeHandle, process_handle: int
    ) -> None:
        self.events.append(("assign", handle, process_handle))

    def TerminateJobObject(self, handle: FakeHandle, exit_code: int) -> None:
        self.events.append(("terminate", handle, exit_code))


class WindowsJobTests(unittest.TestCase):
    def job(self, api: FakeWin32Job) -> WindowsJob:
        with patch("core.windows_job.win32job", api):
            return WindowsJob()

    def test_existing_limits_are_preserved_and_kill_on_close_precedes_assign(
        self,
    ) -> None:
        api = FakeWin32Job()

        with patch("core.windows_job.win32job", api):
            job = WindowsJob()
            job.assign_process(202)
            job.close()

        self.assertEqual(
            [event[0] for event in api.events],
            ["create", "query", "set", "assign", "close"],
        )
        self.assertEqual(api.events[0], ("create", None, ""))
        self.assertEqual(
            api.information["BasicLimitInformation"][  # type: ignore[index]
                "LimitFlags"
            ],
            0x40 | win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        )

    def test_close_is_idempotent(self) -> None:
        api = FakeWin32Job()
        job = self.job(api)

        with patch("core.windows_job.win32job", api):
            job.close()
            job.close()

        self.assertEqual(api.events.count(("close",)), 1)

    def test_configuration_failure_closes_handle_and_propagates(self) -> None:
        api = FakeWin32Job(configure_error=OSError("configuration failed"))

        with patch("core.windows_job.win32job", api):
            with self.assertRaisesRegex(OSError, "configuration failed"):
                WindowsJob()

        self.assertEqual(
            [event[0] for event in api.events],
            ["create", "query", "set", "close"],
        )

    def test_configuration_failure_preserves_close_failure_as_note(self) -> None:
        api = FakeWin32Job(
            configure_error=OSError("configuration failed"),
            close_error=OSError("close failed"),
        )

        with patch("core.windows_job.win32job", api):
            with self.assertRaisesRegex(OSError, "configuration failed") as raised:
                WindowsJob()

        self.assertTrue(
            any("close failed" in note for note in raised.exception.__notes__)
        )

    def test_close_failure_keeps_handle_available_for_tree_termination(self) -> None:
        api = FakeWin32Job(close_error=OSError("close failed"))
        job = self.job(api)

        with self.assertRaisesRegex(OSError, "close failed"):
            job.close()
        with patch("core.windows_job.win32job", api):
            job.terminate()

        self.assertEqual(api.events.count(("close",)), 1)
        self.assertEqual(api.events[-1], ("terminate", api.handle, 1))


@unittest.skipUnless(os.name == "nt", "requires Windows Job Objects")
class WindowsJobIntegrationTests(unittest.TestCase):
    def test_real_job_can_be_configured_and_closed(self) -> None:
        job = WindowsJob()
        job.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

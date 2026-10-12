"""Focused contracts for the review-fix helpers introduced in ``main`` and ``leaks``.

The accepted regression modules pin the review findings through the public operations.
These cases cover the narrower helpers those fixes add: the installed-tool fallbacks for
the analysis and assembly tools, the BinSkim finding gate's documented severity policy,
and the bounded leak-marker read.  Every external program stays behind the single
``main.run`` seam or a fake stream, so no real tool is ever launched.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import leaks  # noqa: E402
import main  # noqa: E402
from leaks import LeakError  # noqa: E402


class ToolLookupTests(unittest.TestCase):
    """The analysis tools resolve from the installed Visual Studio without a dev console."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_binskim_resolves_from_path_or_reports_a_missing_tool(self) -> None:
        with mock.patch("shutil.which", side_effect=lambda name: f"/tools/{name}" if name == "binskim" else None):
            self.assertEqual(main._find_binskimm(), Path("/tools/binskim"))
        with mock.patch("shutil.which", return_value=None), self.assertRaises(FileNotFoundError):
            main._find_binskimm()

    def test_dumpbin_reports_a_missing_msvc_toolset(self) -> None:
        with mock.patch("shutil.which", return_value=None), mock.patch.object(
            main, "_vs_installation", return_value=self.root
        ), self.assertRaises(FileNotFoundError):
            main._find_dumpbin()


class BinskimGateTests(unittest.TestCase):
    """The SARIF finding gate fails on error/warning levels but for the documented BA2027."""

    @staticmethod
    def _result(rule: str, level: str | None) -> dict[str, object]:
        result: dict[str, object] = {"ruleId": rule, "message": {"text": rule}}
        if level is not None:
            result["level"] = level
        return result

    def test_note_level_and_empty_runs_are_clean(self) -> None:
        main.require_clean_binskim({"runs": [{"results": [self._result("BA2001", "note")]}]})
        main.require_clean_binskim({"runs": [{"results": []}]})
        main.require_clean_binskim({"runs": []})

    def test_error_and_default_warning_levels_fail_but_the_ba2027_warning_is_exempt(self) -> None:
        for result in (self._result("BA2001", "error"), self._result("BA2001", None)):
            with self.subTest(result=result), self.assertRaises(main.BuildError):
                main.require_clean_binskim({"runs": [{"results": [result]}]})
        main.require_clean_binskim({"runs": [{"results": [self._result("BA2027", "warning")]}]})


class _BlockingStream:
    """A probe stdout whose ``readline`` never returns before the read timeout expires."""

    def readline(self) -> str:
        threading.Event().wait(10)
        return ""


class LeakMarkerTimeoutTests(unittest.TestCase):
    """A probe that never emits its marker is given up on rather than blocking CI forever."""

    def test_read_times_out_on_a_stalled_stream(self) -> None:
        process = mock.Mock()
        process.stdout = _BlockingStream()
        with self.assertRaises(LeakError):
            leaks._read(process, "READY", "", timeout=0.05)


if __name__ == "__main__":
    unittest.main()

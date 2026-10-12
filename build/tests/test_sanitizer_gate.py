"""Standalone worker tests for the ``core.sanitizer`` log gate.

These assertions were preserved from ``test_sanitizer_graph.py`` when the retired
``graphs.sanitizer`` composition was dropped.  They pin the per-runtime clean-log
requirement and the CLI's lack of a relaxation switch, so the gate stays covered.
"""

from __future__ import annotations

from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import tempfile
import unittest


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.sanitizer import SanitizerError, main as sanitizer_main, require_clean_log  # noqa: E402

class SanitizerGateTests(unittest.TestCase):
    def test_clean_logs_pass_and_findings_fail_for_each_runtime(self) -> None:
        require_clean_log("asan", "All tests passed (42 assertions)\n")
        require_clean_log("ubsan", "All tests passed (42 assertions)\n")
        for sanitizer, finding in (
            ("asan", "ERROR: AddressSanitizer: heap-use-after-free"),
            ("asan", "AddressSanitizer:DEADLYSIGNAL"),
            ("asan", "SUMMARY: AddressSanitizer: double-free"),
            ("ubsan", "foo.cpp:3: runtime error: signed integer overflow"),
            ("ubsan", "UndefinedBehaviorSanitizer:DEADLYSIGNAL"),
            ("ubsan", "SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior"),
        ):
            with self.subTest(sanitizer=sanitizer, finding=finding), self.assertRaisesRegex(
                SanitizerError, "finding"
            ):
                require_clean_log(sanitizer, finding)
        with self.assertRaisesRegex(SanitizerError, "unsupported"):
            require_clean_log("msan", "clean")

    def test_cli_has_no_relaxation_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "test.log"
            log.write_text("All tests passed\n", encoding="utf-8")
            self.assertEqual(sanitizer_main(("gate", "asan", str(log))), 0)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                sanitizer_main(("gate", "asan", str(log), "--allow-findings"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Fixture isolation for the pinned standalone LLVM tool lookup.

The clang-format and clang-tidy selectors honour explicit ``UNPACKRAT_CLANG_FORMAT`` /
``UNPACKRAT_CLANG_TIDY`` overrides.  A CI runner that exports those overrides into the
ambient environment must not change what the neighbouring selection tests measure: every
selection case still has to resolve the synthetic PATH/default, cache and missing-tool
behaviour it asserts.  This test drives the real neighbouring cases through the standard
``unittest`` runner while both overrides and an unrelated sentinel are present, so it
verifies the actual fixture contracts and the scoped restoration they rely on.

The neighbouring cases replace ``main.run`` with a fake, so the synthetic byte files used
as overrides are never executed as real tools; this test pins that same fake around the
inherited-environment context so a leak through the override path can never reach the
process loader.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))

# Import the modules, not their ``TestCase`` subclasses: importing the classes here would
# make ``unittest`` discovery collect every neighbouring suite a second time.
import test_clang_format_selection as format_module  # noqa: E402
import test_clang_tidy_selection as tidy_module  # noqa: E402
import test_review_driver_findings as review_module  # noqa: E402


# The cases whose observable behaviour must survive an inherited CI override.
SELECTION_CASES = (
    (format_module.ClangFormatSelectionTests, "test_default_resolves_the_pinned_tool_from_the_standard_path"),
    (format_module.ClangFormatSelectionTests, "test_default_never_reads_or_creates_a_user_cache"),
    (format_module.ClangFormatSelectionTests, "test_missing_default_tool_or_missing_override_fails_closed"),
    (format_module.ClangFormatSelectionTests, "test_accepts_the_official_release_vcs_suffix"),
    (tidy_module.ClangTidySelectionTests, "test_default_resolves_the_pinned_tool_from_the_standard_path"),
    (tidy_module.ClangTidySelectionTests, "test_default_never_reads_or_creates_a_user_cache"),
    (tidy_module.ClangTidySelectionTests, "test_missing_default_tool_or_missing_override_fails_closed"),
    (tidy_module.ClangTidySelectionTests, "test_accepts_the_official_release_vcs_suffix"),
    (review_module.DriverCase, "test_source_and_llvm_tools_resolve_from_the_installed_vs_without_path"),
)

SENTINEL = "UNPACKRAT_TEST_AMBIENT_SENTINEL"
SENTINEL_VALUE = "ambient sentinel value"

# A version line both standalone selectors accept, so the pinned fake never lets the real
# loader run against the ambient byte files.
VERSION_OUTPUT = "clang-format version 23.1.3\nLLVM version 23.1.3\n"


class ToolLookupEnvironmentIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Real byte files so the selectors treat the inherited overrides as present; the
        # pinned fake ``main.run`` below means these bytes are never executed.
        self.format_override = self.root / "ambient-clang-format.exe"
        self.format_override.write_bytes(b"ambient clang-format override")
        self.tidy_override = self.root / "ambient-clang-tidy.exe"
        self.tidy_override.write_bytes(b"ambient clang-tidy override")

    def test_selection_cases_ignore_an_inherited_override_and_restore_it(self) -> None:
        ambient = {
            "UNPACKRAT_CLANG_FORMAT": str(self.format_override),
            "UNPACKRAT_CLANG_TIDY": str(self.tidy_override),
            SENTINEL: SENTINEL_VALUE,
        }
        version = subprocess.CompletedProcess([], 0, VERSION_OUTPUT)
        with mock.patch.object(format_module.main, "run", return_value=version), \
             mock.patch.dict(os.environ, ambient, clear=False):
            for case_class, method in SELECTION_CASES:
                with self.subTest(case=f"{case_class.__name__}.{method}"):
                    result = unittest.TestResult()
                    case_class(method).run(result)
                    diagnostic = str(result.errors + result.failures)
                    # Every case must restore the inherited overrides and the unrelated
                    # sentinel after its own scoped fixture cleanup, even on failure.
                    self.assertEqual(os.environ.get("UNPACKRAT_CLANG_FORMAT"), str(self.format_override))
                    self.assertEqual(os.environ.get("UNPACKRAT_CLANG_TIDY"), str(self.tidy_override))
                    self.assertEqual(os.environ.get(SENTINEL), SENTINEL_VALUE)
                    self.assertTrue(result.wasSuccessful(), diagnostic)


if __name__ == "__main__":
    unittest.main()

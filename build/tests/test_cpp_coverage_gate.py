"""Standalone worker tests for the ``core.cpp_coverage`` coverage gate.

These assertions were preserved from ``test_cpp_coverage_graph.py`` when the
retired ``graphs.coverage`` composition was dropped.  They pin the exact
first-party line/branch completeness gate and its CLI, so the threshold and
malformed-report handling stay covered.
"""

from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.cpp_coverage import CoverageError, main as coverage_main, require_full_coverage  # noqa: E402

class CppCoverageGateTests(unittest.TestCase):
    @staticmethod
    def report(lines: tuple[int, int] = (7, 7), branches: tuple[int, int] = (3, 3)) -> dict[str, object]:
        return {
            "type": "llvm.coverage.json.export",
            "data": [{"totals": {
                "lines": {"count": lines[0], "covered": lines[1]},
                "branches": {"count": branches[0], "covered": branches[1]},
            }}],
        }

    def test_gate_requires_nonempty_exactly_complete_first_party_lines_and_branches(self) -> None:
        require_full_coverage(self.report())
        for document, message in (
            (self.report(lines=(7, 6)), "lines"),
            (self.report(branches=(3, 2)), "branches"),
            (self.report(branches=(0, 0)), "no first-party branches"),
            (self.report(lines=(0, 0)), "no first-party lines"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(CoverageError, message):
                require_full_coverage(document)

    def test_gate_rejects_malformed_or_ambiguous_llvm_reports(self) -> None:
        malformed = (
            {},
            {"data": []},
            {"data": [{"totals": {}}, {"totals": {}}]},
            {"data": [{"totals": []}]},
            {"data": [{"totals": {"lines": [], "branches": {"count": 1, "covered": 1}}}]},
            {"data": [{"totals": {"lines": {"count": True, "covered": 1}, "branches": {"count": 1, "covered": 1}}}]},
            {"data": [{"totals": {"lines": {"count": 1, "covered": 2}, "branches": {"count": 1, "covered": 1}}}]},
        )
        for document in malformed:
            with self.subTest(document=document), self.assertRaisesRegex(CoverageError, "malformed"):
                require_full_coverage(document)

    def test_cli_reads_json_without_a_threshold_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "coverage.json"
            report.write_text(json.dumps(self.report()), encoding="utf-8")
            self.assertEqual(coverage_main(("gate", str(report))), 0)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                coverage_main(("gate", str(report), "99"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

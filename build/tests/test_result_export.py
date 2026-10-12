from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import core.result_export as result_export  # noqa: E402
from core.result_export import (  # noqa: E402
    DEFERRED,
    FAILED,
    PASSED,
    Operation,
    ResultExportError,
    export_results,
)


def file(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class ResultExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_successful_export_publishes_reports_logs_packages_and_manifest(self) -> None:
        report = file(self.root / "src" / "analysis.sarif", "{}")
        package = file(self.root / "src" / "renpy-3.1.0-x64.zip", "zip")
        manifest = file(self.root / "src" / "packages.json", "{}")
        destination = self.root / "evidence"

        published = export_results(
            destination,
            "verify-arch",
            (
                Operation("source", PASSED, reports=(report,)),
                Operation("package", DEFERRED, detail="blocked by coverage"),
            ),
            packages=(package, manifest),
        )

        self.assertEqual(published, destination)
        document = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], 1)
        self.assertEqual(document["command"], "verify-arch")
        self.assertEqual(document["status"], "success")
        self.assertEqual(document["passed"], ["source"])
        self.assertEqual(document["failed"], [])
        self.assertEqual(document["deferred"], [{"name": "package", "detail": "blocked by coverage"}])
        self.assertEqual(
            (destination / "reports" / "source" / "analysis.sarif").read_text(encoding="utf-8"),
            "{}",
        )
        self.assertTrue((destination / "packages" / "renpy-3.1.0-x64.zip").is_file())
        self.assertTrue((destination / "packages" / "packages.json").is_file())
        self.assertTrue((destination / "logs").is_dir())
        self.assertEqual(document["reports"][0]["path"], "reports/source/analysis.sarif")
        self.assertEqual(len(document["reports"][0]["sha256"]), 64)
        self.assertGreater(document["reports"][0]["size"], 0)

    def test_failed_export_writes_the_failure_log_and_marks_the_command_failed(self) -> None:
        destination = self.root / "evidence"
        published = export_results(
            destination,
            "verify",
            (Operation("coverage", FAILED, detail="gate failed", log="traceback\nboom\n"),),
        )

        document = json.loads((published / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(document["status"], "failed")
        self.assertEqual(document["failed"], ["coverage"])
        self.assertEqual(
            (published / "logs" / "coverage.log").read_text(encoding="utf-8"),
            "traceback\nboom\n",
        )
        self.assertEqual(document["logs"][0]["path"], "logs/coverage.log")

    def test_an_existing_destination_is_refused(self) -> None:
        destination = self.root / "evidence"
        destination.mkdir()
        with self.assertRaisesRegex(ResultExportError, "already exists"):
            export_results(destination, "verify", ())

    def test_a_missing_destination_parent_is_refused(self) -> None:
        with self.assertRaisesRegex(ResultExportError, "parent must be an existing directory"):
            export_results(self.root / "missing" / "evidence", "verify", ())

    def test_a_reparse_ancestor_is_refused(self) -> None:
        destination = self.root / "child" / "evidence"
        (self.root / "child").mkdir()
        with (
            mock.patch.object(result_export, "_is_reparse", side_effect=lambda path: Path(path) == destination.parent),
            self.assertRaisesRegex(ResultExportError, "reparse point in export destination"),
        ):
            export_results(destination, "verify", ())

    def test_invalid_command_and_operations_are_refused(self) -> None:
        for command in ("Verify", "", "bad name"):
            with self.subTest(command=command), self.assertRaisesRegex(ResultExportError, "command must"):
                export_results(self.root / "e", command, ())

        cases = (
            ((Operation("bad name", PASSED),), "invalid operation name"),
            ((Operation("a", PASSED), Operation("a", PASSED)), "duplicate operation"),
            ((Operation("a", "unknown"),), "invalid operation status"),
        )
        for operations, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ResultExportError, message):
                export_results(self.root / "e", "verify", operations)

    def test_missing_reparse_and_non_file_evidence_are_refused(self) -> None:
        report = file(self.root / "src" / "report.sarif", "{}")
        with self.assertRaisesRegex(ResultExportError, "claimed evidence is missing"):
            export_results(self.root / "e1", "verify", (Operation("a", PASSED, reports=(self.root / "no",)),))
        with self.assertRaisesRegex(ResultExportError, "not a regular file"):
            export_results(self.root / "e2", "verify", (Operation("a", PASSED, reports=(self.root,)),))
        with (
            mock.patch.object(result_export, "_is_reparse", side_effect=lambda path: Path(path) == report),
            self.assertRaisesRegex(ResultExportError, "reparse points are forbidden in evidence"),
        ):
            export_results(self.root / "e3", "verify", (Operation("a", PASSED, reports=(report,)),))

    def test_same_basenames_in_different_directories_export_distinctly(self) -> None:
        debug = file(self.root / "tests" / "x64" / "Debug" / "tests.xml", "<debug/>")
        release = file(self.root / "tests" / "x64" / "Release" / "tests.xml", "<release/>")
        destination = self.root / "evidence"
        published = export_results(
            destination, "verify-arch",
            (Operation("tests-x64", PASSED, reports=(debug, release)),),
        )
        document = json.loads((published / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [record["path"] for record in document["reports"]],
            ["reports/tests-x64/Debug/tests.xml", "reports/tests-x64/Release/tests.xml"],
        )
        self.assertEqual(
            (published / "reports" / "tests-x64" / "Debug" / "tests.xml").read_text(
                encoding="utf-8"
            ),
            "<debug/>",
        )
        self.assertEqual(
            (published / "reports" / "tests-x64" / "Release" / "tests.xml").read_text(
                encoding="utf-8"
            ),
            "<release/>",
        )

    def test_multiarch_same_basename_reports_keep_their_architecture(self) -> None:
        x86 = file(self.root / "out" / "x86" / "analysis.sarif", "x86")
        x64 = file(self.root / "out" / "x64" / "analysis.sarif", "x64")
        published = export_results(
            self.root / "evidence", "verify-arch",
            (Operation("compiler-analysis", PASSED, reports=(x86, x64)),),
        )
        document = json.loads((published / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [record["path"] for record in document["reports"]],
            [
                "reports/compiler-analysis/x86/analysis.sarif",
                "reports/compiler-analysis/x64/analysis.sarif",
            ],
        )

    def test_a_repeated_report_path_is_refused(self) -> None:
        report = file(self.root / "one" / "same.txt", "1")
        with self.assertRaisesRegex(ResultExportError, "duplicate evidence name"):
            export_results(
                self.root / "e", "verify",
                (Operation("a", PASSED, reports=(report, report)),),
            )

    def test_a_passed_operation_may_carry_its_command_log(self) -> None:
        destination = self.root / "evidence"
        published = export_results(
            destination, "verify",
            (Operation("tests-x64", PASSED, log="suite output\n"),),
        )
        document = json.loads((published / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(document["status"], "success")
        self.assertEqual(document["logs"][0]["path"], "logs/tests-x64.log")
        self.assertEqual(
            (published / "logs" / "tests-x64.log").read_text(encoding="utf-8"),
            "suite output\n",
        )

    def test_a_reparse_ancestor_of_claimed_evidence_is_refused(self) -> None:
        ancestor = self.root / "src" / "nested"
        report = file(ancestor / "report.sarif", "{}")
        with (
            mock.patch.object(
                result_export, "_is_reparse", side_effect=lambda path: Path(path) == ancestor
            ),
            self.assertRaisesRegex(ResultExportError, "reparse points are forbidden in evidence"),
        ):
            export_results(self.root / "e", "verify", (Operation("a", PASSED, reports=(report,)),))

    def test_a_copy_failure_is_wrapped(self) -> None:
        report = file(self.root / "src" / "report.sarif", "{}")
        with (
            mock.patch.object(result_export.shutil, "copyfile", side_effect=OSError("locked")),
            self.assertRaisesRegex(ResultExportError, "could not publish evidence export"),
        ):
            export_results(self.root / "e", "verify", (Operation("a", PASSED, reports=(report,)),))

    def test_missing_lstat_of_a_destination_reports_absence(self) -> None:
        self.assertIsNone(result_export._lstat(self.root / "absent"))
        self.assertIsNotNone(result_export._lstat(self.root))


if __name__ == "__main__":
    unittest.main()

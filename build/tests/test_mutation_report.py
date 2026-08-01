import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = REPOSITORY_ROOT / "build" / "mutation" / "validate_report.py"


class MutationReportValidatorTests(unittest.TestCase):
    def run_validator(self, report):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "mutation.json"
            if isinstance(report, str):
                report_path.write_text(report, encoding="utf-8")
            else:
                report_path.write_text(json.dumps(report), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(VALIDATOR), str(report_path)],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

    @staticmethod
    def report(*mutants):
        return {
            "schemaVersion": "1.0",
            "files": {
                "src/modules/renpy/pickle.cpp": {
                    "language": "cpp",
                    "mutants": list(mutants),
                }
            },
        }

    def test_accepts_non_empty_report_when_every_mutant_is_killed(self):
        result = self.run_validator(
            self.report(
                {"id": "1", "status": "Killed", "location": {"start": {"line": 10}}},
                {"id": "2", "status": "Killed", "location": {"start": {"line": 20}}},
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "mutation report: 2 reached mutants, all killed")

    def test_rejects_empty_report_instead_of_accepting_infinite_score(self):
        result = self.run_validator(self.report())

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no reached mutants", result.stderr)

    def test_rejects_surviving_mutant_with_actionable_identity(self):
        result = self.run_validator(
            self.report(
                {
                    "id": "boundary-7",
                    "status": "Survived",
                    "location": {"start": {"line": 73}},
                }
            )
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Survived", result.stderr)
        self.assertIn("pickle.cpp:73", result.stderr)
        self.assertIn("boundary-7", result.stderr)

    def test_rejects_non_killed_reached_statuses(self):
        for status in ("NoCoverage", "Timeout", "RuntimeError", "Ignored"):
            with self.subTest(status=status):
                result = self.run_validator(self.report({"id": status, "status": status}))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(status, result.stderr)

    def test_rejects_malformed_json_and_malformed_mutants(self):
        invalid_json = self.run_validator("not-json")
        self.assertNotEqual(invalid_json.returncode, 0)
        self.assertIn("valid JSON", invalid_json.stderr)

        invalid_mutant = self.run_validator(self.report({"id": "missing-status"}))
        self.assertNotEqual(invalid_mutant.returncode, 0)
        self.assertIn("status", invalid_mutant.stderr)


if __name__ == "__main__":
    unittest.main()

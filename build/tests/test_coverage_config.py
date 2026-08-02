from __future__ import annotations

import unittest
from pathlib import Path

from coverage import Coverage


BUILD_ROOT = Path(__file__).resolve().parents[1]


class CoverageConfigTests(unittest.TestCase):
    def test_first_party_python_gate_requires_every_line_and_branch(self) -> None:
        coverage = Coverage(config_file=str(BUILD_ROOT / "pyproject.toml"))

        self.assertTrue(coverage.get_option("run:branch"))
        self.assertEqual(coverage.get_option("run:source"), ["."])
        self.assertEqual(
            coverage.get_option("run:command_line"),
            "-m unittest discover -s tests -p test_*.py",
        )
        self.assertEqual(coverage.get_option("report:fail_under"), 100)
        self.assertEqual(coverage.get_option("report:exclude_lines"), [])
        self.assertEqual(
            coverage.get_option("report:include"),
            ["core/*", "graphs/*", "driver.py", "main.py"],
        )
        self.assertTrue(coverage.get_option("report:show_missing"))
        self.assertFalse(coverage.get_option("report:skip_covered"))


if __name__ == "__main__":
    unittest.main()

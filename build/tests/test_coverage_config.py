from __future__ import annotations

import unittest
from pathlib import Path

from coverage import Coverage
from coverage.parser import PythonParser


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
        self.assertEqual(
            coverage.get_option("report:include"),
            ["main.py", "leaks.py", "licenses.py"],
        )
        self.assertTrue(coverage.get_option("report:show_missing"))
        self.assertFalse(coverage.get_option("report:skip_covered"))

    def test_the_first_party_gate_has_no_coverage_exclusion(self) -> None:
        # Every first-party line and branch is measured: there is no escape hatch, so the gate
        # must declare no excluded lines.
        coverage = Coverage(config_file=str(BUILD_ROOT / "pyproject.toml"))

        self.assertEqual(coverage.get_option("report:exclude_lines"), [])
        self.assertEqual(coverage.get_option("report:exclude_also"), [])

    def test_first_party_sources_carry_no_coverage_exclusion(self) -> None:
        sources = [
            path
            for pattern in ("main.py", "leaks.py", "licenses.py")
            for path in sorted(BUILD_ROOT.glob(pattern))
        ]

        self.assertTrue(sources)
        for path in sources:
            with self.subTest(module=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("no cover", text)
                # Ask coverage.py's own parser which lines it would exclude: none may exist.
                parser = PythonParser(filename=str(path), exclude="pragma: no cover")
                parser.parse_source()
                self.assertEqual(sorted(parser.excluded), [])


if __name__ == "__main__":
    unittest.main()

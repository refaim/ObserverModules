from __future__ import annotations

import re
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
            ["core/*", "main.py", "native.py", "source_checks.py", "diagnostics.py",
             "packaging_ops.py"],
        )
        self.assertTrue(coverage.get_option("report:show_missing"))
        self.assertFalse(coverage.get_option("report:skip_covered"))

    def test_the_only_exclusion_is_an_explicit_per_line_pragma(self) -> None:
        coverage = Coverage(config_file=str(BUILD_ROOT / "pyproject.toml"))

        self.assertEqual(
            coverage.get_option("report:exclude_lines"), ["pragma: no cover"]
        )
        self.assertEqual(coverage.get_option("report:exclude_also"), [])

    def test_every_excluded_line_is_annotated_or_a_main_guard_body(self) -> None:
        # coverage.py silently widens a pragma on a compound-statement header over the whole
        # body, so reading the source for annotations is not enough: ask the parser which lines
        # the exclusion actually removed, and allow that widening only for a __main__ guard.
        annotation = re.compile(r"#\s*pragma:\s*no cover\s*(?:#.*)?$")
        guard = re.compile(r'^\s*if __name__ == "__main__":')
        sources = [
            path
            for pattern in (
                "core/*.py", "main.py", "native.py", "source_checks.py",
                "diagnostics.py", "packaging_ops.py",
            )
            for path in sorted(BUILD_ROOT.glob(pattern))
        ]

        self.assertTrue(sources)
        for path in sources:
            parser = PythonParser(filename=str(path), exclude="pragma: no cover")
            parser.parse_source()
            lines = path.read_text(encoding="utf-8").splitlines()

            for number in sorted(parser.raw_excluded):
                with self.subTest(path=path.name, line=number):
                    self.assertRegex(lines[number - 1], annotation)
            for number in sorted(parser.excluded - parser.raw_excluded):
                with self.subTest(path=path.name, widened=number):
                    self.assertRegex(lines[number - 2], guard)


if __name__ == "__main__":
    unittest.main()

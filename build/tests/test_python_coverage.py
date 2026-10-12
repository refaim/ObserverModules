"""Worker tests for the retained ``core.python_coverage`` gate.

These assertions were preserved from ``test_python_coverage_graph.py`` when the
retired ``graphs.python_coverage`` composition was dropped.  They build a real
temporary coverage project and run the actual ``core.python_coverage`` CLI, so
the line/branch failure handling and evidence publication stay covered.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.python_coverage import main as coverage_main  # noqa: E402


class PythonCoverageWorkerTests(unittest.TestCase):
    def project(self, root: Path) -> Path:
        for relative, content in (
            ("core/__init__.py", ""),
            ("core/value.py", "VALUE = 1\n"),
            ("graphs/__init__.py", ""),
            ("graphs/value.py", "VALUE = 2\n"),
            ("driver.py", "VALUE = 4\n"),
            ("main.py", "VALUE = 3\n"),
            ("pyproject.toml", (BUILD_ROOT / "pyproject.toml").read_text(encoding="utf-8")),
            ("tests/test_all.py", "import unittest\nfrom core.value import VALUE as CORE\nfrom graphs.value import VALUE as GRAPH\nimport driver\nimport main\nclass T(unittest.TestCase):\n def test_all(self): self.assertEqual((CORE, GRAPH, driver.VALUE, main.VALUE), (1, 2, 4, 3))\n"),
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    def test_exact_cli_publishes_line_branch_reports_from_isolated_work_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, work, output = self.project(root / "project"), root / "work", root / "output"
            work.mkdir()
            output.mkdir()
            environment = {"OBSERVER_BUILD_DIR": str(work), "OBSERVER_OUT_DIR": str(output)}
            coverage = BUILD_ROOT / ".venv/Scripts/coverage.exe"
            with mock.patch.dict(os.environ, environment, clear=False):
                self.assertEqual(0, coverage_main((str(coverage), str(project))))

            document = json.loads((output / "coverage.json").read_text(encoding="utf-8"))
            self.assertEqual(100.0, document["totals"]["percent_covered"])
            self.assertTrue((output / "coverage.xml").is_file())
            self.assertIn("100%", (output / "coverage.txt").read_text(encoding="utf-8"))
            self.assertEqual(
                (output / "coverage.toml").read_bytes(),
                (project / "pyproject.toml").read_bytes(),
            )
            self.assertTrue((work / ".coverage").is_file())
            self.assertEqual((output / ".coverage").read_bytes(), (work / ".coverage").read_bytes())
            self.assertFalse((project / ".coverage").exists())

    def test_below_100_fails_after_publishing_evidence_and_environment_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, work, output = self.project(root / "project"), root / "work", root / "output"
            work.mkdir()
            output.mkdir()
            (project / "tests/test_all.py").write_text(
                "import unittest\nfrom core.value import VALUE as CORE\nfrom graphs.value import VALUE as GRAPH\n"
                "class T(unittest.TestCase):\n def test_packages(self): self.assertEqual((CORE, GRAPH), (1, 2))\n",
                encoding="utf-8",
            )
            coverage = BUILD_ROOT / ".venv/Scripts/coverage.exe"
            with mock.patch.dict(os.environ, {"OBSERVER_BUILD_DIR": str(work), "OBSERVER_OUT_DIR": str(output)}):
                with self.assertRaises(subprocess.CalledProcessError):
                    coverage_main((str(coverage), str(project)))
            self.assertLess(json.loads((output / "coverage.json").read_text())["totals"]["percent_covered"], 100)
            self.assertTrue((output / "coverage.txt").is_file())

            (project / "core/value.py").write_text(
                "def choose(first, second):\n value = 0\n if first:\n  value = 1\n if second:\n  value = 2\n return value\n",
                encoding="utf-8",
            )
            (project / "tests/test_all.py").write_text(
                "import unittest\nfrom core.value import choose\nfrom graphs.value import VALUE\nimport driver\nimport main\n"
                "class T(unittest.TestCase):\n def test_one_branch(self): self.assertEqual((choose(True, True), VALUE, driver.VALUE, main.VALUE), (2, 2, 4, 3))\n",
                encoding="utf-8",
            )
            branch_work, branch_output = root / "branch-work", root / "branch-output"
            branch_work.mkdir()
            branch_output.mkdir()
            with mock.patch.dict(os.environ, {"OBSERVER_BUILD_DIR": str(branch_work),
                                              "OBSERVER_OUT_DIR": str(branch_output)}):
                with self.assertRaises(subprocess.CalledProcessError):
                    coverage_main((str(coverage), str(project)))
            totals = json.loads((branch_output / "coverage.json").read_text())["totals"]
            self.assertEqual(0, totals["missing_lines"])
            self.assertGreater(totals["missing_branches"], 0)

        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(RuntimeError, "OBSERVER_BUILD_DIR"):
            coverage_main((str(BUILD_ROOT / ".venv/Scripts/coverage.exe"), str(BUILD_ROOT)))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work, output, tool, build_file = root / "work", root / "output", root / "coverage", root / "build"
            work.mkdir()
            output.mkdir()
            tool.mkdir()
            build_file.touch()
            environment = {"OBSERVER_BUILD_DIR": str(work), "OBSERVER_OUT_DIR": str(output)}
            for arguments in ((tool, BUILD_ROOT), (BUILD_ROOT / ".venv/Scripts/coverage.exe", build_file)):
                with self.subTest(arguments=arguments), mock.patch.dict(os.environ, environment, clear=False):
                    with self.assertRaises(FileNotFoundError):
                        coverage_main(tuple(map(str, arguments)))
            missing_config = root / "missing-config"
            missing_config.mkdir()
            with mock.patch.dict(os.environ, environment, clear=False), self.assertRaises(FileNotFoundError):
                coverage_main((str(BUILD_ROOT / ".venv/Scripts/coverage.exe"), str(missing_config)))
            with mock.patch.dict(os.environ, {"OBSERVER_BUILD_DIR": str(root / "missing"),
                                              "OBSERVER_OUT_DIR": str(output)}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "OBSERVER_BUILD_DIR"):
                    coverage_main((str(BUILD_ROOT / ".venv/Scripts/coverage.exe"), str(BUILD_ROOT)))


if __name__ == "__main__":
    unittest.main(verbosity=2)

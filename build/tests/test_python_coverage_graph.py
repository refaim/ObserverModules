from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.python_coverage import main as coverage_main  # noqa: E402
from graphs.python_coverage import python_coverage_graph  # noqa: E402


class PythonCoverageGraphTests(unittest.TestCase):
    def repository(self, root: Path, executable: bool = True) -> Path:
        build = root / "build"
        for relative, content in (
            ("core/example.py", "VALUE = 1\n"),
            ("graphs/example.py", "VALUE = 2\n"),
            ("tests/test_example.py", "pass\n"),
            ("driver.py", "VALUE = 4\n"),
            ("main.py", "VALUE = 3\n"),
            ("pyproject.toml", "[project]\nname='fixture'\n"),
            ("uv.lock", "fixture\n"),
        ):
            path = build / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        coverage = build / ".venv/Scripts/coverage.exe"
        package = build / ".venv/Lib/site-packages/coverage"
        package.mkdir(parents=True)
        (package / "version.py").write_text("__version__ = 'fixture'\n", encoding="utf-8")
        cache = package / "__pycache__"
        cache.mkdir()
        (cache / "version.pyc").write_bytes(b"derived")
        if executable:
            coverage.parent.mkdir(parents=True)
            coverage.write_bytes(b"coverage-launcher")
        else:
            coverage.mkdir(parents=True)
        return root

    def test_single_gate_signs_all_first_party_tests_config_lock_and_exact_local_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.repository(Path(temporary) / "repo")
            before = python_coverage_graph(repository)
            node = before.node("python-coverage")

            self.assertEqual(before.targets, (node.name,))
            self.assertEqual(dict(before.pools), {"python-coverage": 1})
            self.assertEqual(dict(node.command.env), {"PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(
                node.command.argv,
                (sys.executable, "-m", "core.python_coverage",
                 str(repository / "build/.venv/Scripts/coverage.exe"), str(repository / "build")),
            )
            (repository / "build/tests/test_example.py").write_text("changed\n", encoding="utf-8")
            changed_test = python_coverage_graph(repository)
            (repository / "build/tests/test_example.py").write_text("pass\n", encoding="utf-8")
            (repository / "build/driver.py").write_text("changed\n", encoding="utf-8")
            changed_driver = python_coverage_graph(repository)
            (repository / "build/driver.py").write_text("VALUE = 4\n", encoding="utf-8")
            config = repository / "build/pyproject.toml"
            original_config = config.read_text(encoding="utf-8")
            config.write_text(original_config + "\n# changed\n", encoding="utf-8")
            changed_config = python_coverage_graph(repository)
            config.write_text(original_config, encoding="utf-8")
            (repository / "build/.venv/Scripts/coverage.exe").write_bytes(b"changed-coverage")
            changed_tool = python_coverage_graph(repository)
            (repository / "build/.venv/Scripts/coverage.exe").write_bytes(b"coverage-launcher")
            (repository / "build/.venv/Lib/site-packages/coverage/version.py").write_text(
                "__version__ = 'changed'\n", encoding="utf-8"
            )
            changed_package = python_coverage_graph(repository)

        self.assertNotEqual(node.uid, changed_test.node(node.name).uid)
        self.assertNotEqual(node.uid, changed_driver.node(node.name).uid)
        self.assertNotEqual(node.uid, changed_config.node(node.name).uid)
        self.assertNotEqual(node.uid, changed_tool.node(node.name).uid)
        self.assertNotEqual(node.uid, changed_package.node(node.name).uid)

    def test_missing_or_non_file_project_coverage_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = self.repository(root / "missing")
            (missing / "build/.venv/Scripts/coverage.exe").unlink()
            with self.assertRaises(FileNotFoundError):
                python_coverage_graph(missing)
            directory = self.repository(root / "directory", executable=False)
            with self.assertRaisesRegex(FileNotFoundError, "not a file"):
                python_coverage_graph(directory)
            bad_package = self.repository(root / "bad-package")
            package = bad_package / "build/.venv/Lib/site-packages/coverage"
            (package / "version.py").unlink()
            (package / "__pycache__/version.pyc").unlink()
            (package / "__pycache__").rmdir()
            package.rmdir()
            package.write_text("not a directory", encoding="utf-8")
            with self.assertRaisesRegex(FileNotFoundError, "not a directory"):
                python_coverage_graph(bad_package)


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

            entry_work, entry_output = root / "entry-work", root / "entry-output"
            entry_work.mkdir()
            entry_output.mkdir()
            with (
                mock.patch.dict(os.environ, {"OBSERVER_BUILD_DIR": str(entry_work),
                                             "OBSERVER_OUT_DIR": str(entry_output)}, clear=False),
                mock.patch.object(sys, "argv", ["python_coverage.py", str(coverage), str(project)]),
                self.assertRaises(SystemExit) as raised,
            ):
                runpy.run_path(str(BUILD_ROOT / "core/python_coverage.py"), run_name="__main__")
            self.assertEqual(0, raised.exception.code)

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

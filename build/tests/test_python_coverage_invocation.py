"""Focused regression for the coverage.py invocation route.

The repository venv once carried a ``coverage.exe`` launcher pinned to a
missing interpreter.  The gate must instead run coverage through the *current*
interpreter (``python -m coverage``), so the pinned project interpreter owns
the run.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core import python_coverage  # noqa: E402


class CompletedProcess:
    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout
        self.returncode = 0

    def check_returncode(self) -> None:
        return None


class CoverageInvocationTests(unittest.TestCase):
    def project(self, root: Path) -> Path:
        (root / "core").mkdir(parents=True)
        (root / "core/__init__.py").write_text("", encoding="utf-8")
        (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
        return root

    def _recording_run(self, work: Path, commands: list[list[str]]):
        def run(command, **kwargs):
            commands.append([str(item) for item in command])
            if "run" in command:
                (work / ".coverage").write_bytes(b"data")
            return CompletedProcess("100%\n")

        return run

    def test_the_gate_runs_through_the_current_interpreter_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.project(root / "project")
            work, output = root / "work", root / "output"
            work.mkdir()
            output.mkdir()
            commands: list[list[str]] = []
            environment = {"OBSERVER_BUILD_DIR": str(work), "OBSERVER_OUT_DIR": str(output)}

            with mock.patch.dict(os.environ, environment, clear=False):
                with mock.patch.object(subprocess, "run", side_effect=self._recording_run(work, commands)):
                    report = python_coverage.run(project)

            self.assertEqual(report, output / "coverage.txt")
            self.assertTrue(commands)
            for command in commands:
                self.assertEqual(command[:3], [sys.executable, "-m", "coverage"])

    def test_the_legacy_cli_keeps_its_compatibility_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.project(root / "project")
            launcher = project / "coverage.exe"
            launcher.write_bytes(b"legacy-launcher")
            work, output = root / "work", root / "output"
            work.mkdir()
            output.mkdir()
            commands: list[list[str]] = []
            environment = {"OBSERVER_BUILD_DIR": str(work), "OBSERVER_OUT_DIR": str(output)}

            with mock.patch.dict(os.environ, environment, clear=False):
                with mock.patch.object(subprocess, "run", side_effect=self._recording_run(work, commands)):
                    self.assertEqual(0, python_coverage.main((str(launcher), str(project))))

            self.assertTrue(commands)
            for command in commands:
                self.assertEqual(command[:3], [sys.executable, "-m", "coverage"])
                self.assertNotIn(str(launcher), command)


if __name__ == "__main__":
    unittest.main()

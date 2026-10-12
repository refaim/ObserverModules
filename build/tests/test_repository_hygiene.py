"""Repository hygiene: the root ``.gitignore`` protects this checkout.

The test runs against a throwaway Git repository that carries only this repository's
own ``.gitignore``.  A private, empty ``core.excludesFile`` disables the ambient global
ignore file, so the assertions exercise exactly the committed rules instead of the
developer's machine.  Generated Python/IDE artefacts must be ignored while production
sources, configuration and committed fixtures stay visible to ``git status``.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class GitIgnoreTests(unittest.TestCase):
    """The tracked ignore rules hide local tooling without hiding project content."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.git = shutil.which("git")

    def setUp(self) -> None:
        if self.git is None:
            self.skipTest("git is not installed")

    def _repository(self, root: Path) -> Path:
        repository = root / "repository"
        repository.mkdir()
        shutil.copy(REPOSITORY_ROOT / ".gitignore", repository / ".gitignore")
        self._git(repository, "init", "-q")
        empty_global = root / "empty-global-ignore"
        empty_global.write_text("", encoding="utf-8")
        self._git(repository, "config", "core.excludesFile", str(empty_global))
        return repository

    def _git(self, repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git, *arguments],
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
        )

    def _ignored(self, repository: Path, relative: str) -> bool:
        result = subprocess.run(
            # ``--no-index`` keeps the check a pure rule evaluation, independent of what a
            # future ``git add`` may have staged.
            [self.git, "check-ignore", "--no-index", "-q", "--", relative],
            cwd=repository,
            capture_output=True,
            text=True,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result.returncode == 0

    def test_generated_artefacts_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            generated = (
                "build/__pycache__/main.cpython-314.pyc",
                "src/modules/renpy/__pycache__/__init__.cpython-314.pyc",
                "build/packaging_ops.pyc",
                "build/tooling.pyd",
                "build/optimised.pyo",
                ".vs/unpackrat/v17/Browse.VC.db",
                "build/projects/renpy.vcxproj.user",
            )
            for relative in generated:
                with self.subTest(relative=relative):
                    self.assertTrue(self._ignored(repository, relative), relative)

    def test_existing_transient_ignores_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            transient = (
                "out/reports/analysis.sarif",
                "build/.venv/Scripts/python.exe",
                "build/.uv-cache/wheels/xxhash.whl",
                "build/.coverage.unit",
                ".idea/workspace.xml",
            )
            for relative in transient:
                with self.subTest(relative=relative):
                    self.assertTrue(self._ignored(repository, relative), relative)

    def test_production_configuration_and_fixtures_stay_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            visible = (
                "build/main.py",
                "build/source_checks.py",
                "build.ps1",
                "src/api.h",
                "src/modules/renpy/pickle.cpp",
                "src/tests/support/archive_fixtures.cpp",
                "vcpkg.json",
                "build/PSScriptAnalyzerSettings.psd1",
                ".gitignore",
                ".gitattributes",
            )
            for relative in visible:
                with self.subTest(relative=relative):
                    self.assertFalse(self._ignored(repository, relative), relative)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

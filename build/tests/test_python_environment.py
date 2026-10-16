"""The pinned Python toolchain for the build driver.

The driver runs under one pinned interpreter with a frozen dependency set.  These
tests read both manifests that must agree, so a bump in one without the other is a
failing requirement rather than silent drift.  Only the observable pins are asserted:
the interpreter, the project version and every dependency version in both files.
"""

from __future__ import annotations

from pathlib import Path
import tomllib
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
BUILD = REPOSITORY / "build"

REQUIRES_PYTHON = "==3.14.7"
PROJECT_VERSION = "0.8.0"
DEPENDENCIES = {
    "coverage": "7.16.2",
    "filelock": "4.1.1",
    "psutil": "7.2.2",
    "pywin32": "312",
}

def _requirement_versions(requirements: list[str]) -> dict[str, str]:
    """Map each ``name==version`` requirement to its pinned version, dropping env markers."""

    versions: dict[str, str] = {}
    for requirement in requirements:
        name, _, specifier = requirement.partition("==")
        versions[name.split(";")[0].strip()] = specifier.split(";")[0].strip()
    return versions


class PythonEnvironmentPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = tomllib.loads((BUILD / "pyproject.toml").read_text(encoding="utf-8"))
        self.lock = tomllib.loads((BUILD / "uv.lock").read_text(encoding="utf-8"))

    def test_project_declares_the_pinned_interpreter_and_version(self) -> None:
        declared = self.project["project"]
        self.assertEqual(declared["requires-python"], REQUIRES_PYTHON)
        self.assertEqual(declared["version"], PROJECT_VERSION)

    def test_project_dependencies_are_exactly_the_pinned_versions(self) -> None:
        self.assertEqual(
            _requirement_versions(self.project["project"]["dependencies"]),
            DEPENDENCIES,
        )

    def test_lockfile_agrees_with_the_project_interpreter(self) -> None:
        self.assertEqual(self.lock["requires-python"], REQUIRES_PYTHON)

    def test_lockfile_matches_every_pinned_dependency_and_the_project_version(self) -> None:
        packages = {entry["name"]: entry["version"] for entry in self.lock["package"]}
        for name, version in DEPENDENCIES.items():
            with self.subTest(package=name):
                self.assertEqual(packages.get(name), version)
        self.assertEqual(packages.get("unpackrat-build"), PROJECT_VERSION)


if __name__ == "__main__":
    unittest.main()

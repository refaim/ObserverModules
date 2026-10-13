"""Focused regression contracts for the CI workflow and lock findings.

These tests encode the observable behaviour the workflow must have after the
2026-10-12 review and are expected to fail against the frozen workflow and lock
as they stand.  Every assertion is about a composition of the workflow's own
declarations (artifact names, paths, conditionals) or about the effect of running
the release step's own PowerShell, never about a particular source keyword.

Nothing here reaches the network: the release-notes test runs only the local
notes-building statements against fixture change logs, and the artifact test is
a pure simulation of the upload/download layout.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPOSITORY / ".github" / "workflows" / "main.yml"
CHECK_WORKFLOW_PATH = REPOSITORY / ".github" / "workflows" / "ci-check.yml"


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _check_workflow() -> str:
    return CHECK_WORKFLOW_PATH.read_text(encoding="utf-8")


def _job_lines(text: str, job: str) -> list[str]:
    lines = text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if re.fullmatch(rf"  {re.escape(job)}:\s*", line)),
        None,
    )
    if start is None:
        raise AssertionError(f"workflow has no {job!r} job")
    block: list[str] = []
    for line in lines[start + 1:]:
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            break
        block.append(line)
    return block


def _assign(step: dict, key: str, value: str) -> None:
    value = value.strip()
    if key in ("uses", "name", "if"):
        step[key] = value


def _steps(block: list[str]) -> list[dict]:
    steps: list[dict] = []
    current: dict | None = None
    in_run = False
    for line in block:
        started = re.match(r"^      - (.*)$", line)
        if started:
            current = {"uses": None, "name": None, "if": None, "with": {}, "run": []}
            in_run = False
            inline = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", started.group(1))
            if inline:
                if inline.group(1) == "run" and inline.group(2).strip() in ("", "|", ">", "|-", ">-"):
                    in_run = True
                else:
                    _assign(current, inline.group(1), inline.group(2))
            steps.append(current)
            continue
        if current is None:
            continue
        if in_run:
            if re.match(r"^        [A-Za-z0-9_-]+:", line):
                in_run = False
            else:
                current["run"].append(line)
                continue
        key = re.match(r"^        ([A-Za-z0-9_-]+):\s*(.*)$", line)
        if key:
            if key.group(1) == "run" and key.group(2).strip() in ("", "|", ">", "|-", ">-"):
                in_run = True
            else:
                _assign(current, key.group(1), key.group(2))
            continue
        with_key = re.match(r"^          ([A-Za-z0-9_-]+):\s*(.*)$", line)
        if with_key:
            current["with"][with_key.group(1)] = with_key.group(2).strip()
    return steps


class ArtifactLayoutTests(unittest.TestCase):
    """Finding 1: the downloaded Release artifacts must land where packaging reads them."""

    def test_release_binaries_land_in_the_directories_packaging_reads(self) -> None:
        # The reusable native slice uploads the whole per-architecture Release directory.
        uploads: list[str] = []
        for step in _steps(_job_lines(_check_workflow(), "check")):
            if not step["uses"] or "upload-artifact@" not in step["uses"]:
                continue
            if step["with"].get("name") == "release-${{ inputs.arch }}":
                uploads.append(step["with"].get("path", ""))
        self.assertEqual(uploads, ["out/native/bin/${{ inputs.arch }}/Release"])

        # Packaging downloads each verified architecture into the directory it reads.
        text = _workflow()
        landings: dict[str, str] = {}
        found_download = False
        for step in _steps(_job_lines(_workflow(), "packaging")):
            if not step["uses"] or "download-artifact@" not in step["uses"]:
                continue
            found_download = True
            name = step["with"].get("name")
            for arch in ("x86", "x64"):
                if name == f"release-{arch}":
                    landings[arch] = step["with"].get("path", ".")
        self.assertTrue(found_download, "the packaging job must download the Release artifacts")
        # `main.package` reads `<repo>/out/native/bin/<arch>/Release/<module>.so`.
        self.assertEqual(
            landings,
            {"x86": "out/native/bin/x86/Release", "x64": "out/native/bin/x64/Release"},
        )


class DiagnosticsUploadTests(unittest.TestCase):
    """Finding 9: CI diagnostics must be uploaded unconditionally."""

    @staticmethod
    def _covers(glob: str, target: str) -> bool:
        base = glob.strip().split("*")[0].rstrip("/")
        return bool(base) and (target == base or target.startswith(base + "/") or base.startswith(target + "/"))

    def test_ci_diagnostics_are_uploaded_with_always(self) -> None:
        uploads = [
            step
            for step in _steps(_job_lines(_check_workflow(), "check"))
            if step["uses"] and "upload-artifact@" in step["uses"]
        ]
        unconditional = [step for step in uploads if (step["if"] or "").replace("${{", "").replace("}}", "").strip() == "always()"]
        self.assertTrue(unconditional, "no upload-artifact step runs with if: always()")

        covered = {
            target: any(self._covers(step["with"].get("path", ""), target) for step in unconditional)
            for target in ("out/native/reports", "out/native/coverage")
        }
        self.assertTrue(
            covered["out/native/reports"],
            "the JUnit reports under out/native/reports are not uploaded by an always() step",
        )
        self.assertTrue(
            covered["out/native/coverage"],
            "the coverage summary under out/native/coverage is not uploaded by an always() step",
        )


class ReleaseNotesTests(unittest.TestCase):
    """Finding 2: the release notes carry only the latest EN/RU section."""

    EN = (
        "Version 3.1.0\n"
        "-------------\n"
        " + latest english note.\n"
        " * latest english fix.\n"
        "\n"
        "Version 3.0.0\n"
        "-------------\n"
        " + old english note.\n"
    )
    RU = (
        "\u0412\u0435\u0440\u0441\u0438\u044f 3.1.0\n"
        "------------\n"
        " + latest russian note.\n"
        "\n"
        "\u0412\u0435\u0440\u0441\u0438\u044f 3.0.0\n"
        "------------\n"
        " + old russian note.\n"
    )

    @staticmethod
    def _notes_script(text: str) -> str:
        for step in _steps(_job_lines(text, "release")):
            body = "\n".join(step["run"])
            if "gh release create" not in body:
                continue
            prefix: list[str] = []
            for line in step["run"]:
                stripped = line.strip()
                if stripped.startswith("$archives") or "gh release create" in stripped:
                    break
                prefix.append(line)
            return "\n".join(prefix)
        raise AssertionError("the release job has no notes-building step")

    def test_release_notes_keep_only_the_latest_section(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is not available")

        script = self._notes_script(_workflow())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "src" / "modules" / "renpy"
            module.mkdir(parents=True)
            (module / "ChangeLog.en.txt").write_text(self.EN, encoding="utf-8")
            (module / "ChangeLog.ru.txt").write_text(self.RU, encoding="utf-8")
            runner_temp = root / "runner"
            runner_temp.mkdir()
            script_path = root / "notes.ps1"
            script_path.write_text(script + "\n", encoding="utf-8")
            environment = {
                **os.environ,
                "GITHUB_REF_NAME": "renpy/v3.1.0",
                "RUNNER_TEMP": str(runner_temp),
            }
            completed = subprocess.run(
                [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script_path)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            notes = (runner_temp / "notes.md").read_text(encoding="utf-8-sig")

        self.assertIn("Version 3.1.0", notes)
        self.assertIn("\u0412\u0435\u0440\u0441\u0438\u044f 3.1.0", notes)
        self.assertIn("latest english note.", notes)
        self.assertIn("latest russian note.", notes)
        self.assertNotIn("Version 3.0.0", notes)
        self.assertNotIn("\u0412\u0435\u0440\u0441\u0438\u044f 3.0.0", notes)
        self.assertNotIn("old english note.", notes)
        self.assertNotIn("old russian note.", notes)


class ReleaseArchiveSelectionTests(unittest.TestCase):
    """Finding: the release step passes the three selected archive paths literally to gh."""

    @staticmethod
    def _release_body(text: str) -> str:
        for step in _steps(_job_lines(text, "release")):
            body = "\n".join(step["run"])
            if "gh release create" in body:
                return body
        raise AssertionError("the release job has no publish step")

    def test_stale_archives_are_ignored_and_exactly_three_paths_are_passed(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is not available")

        body = self._release_body(_workflow())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "src" / "modules" / "renpy"
            module.mkdir(parents=True)
            (module / "ChangeLog.en.txt").write_text("Version 1.2.3\n---\n + note.\n", encoding="utf-8")
            (module / "ChangeLog.ru.txt").write_text(
                "\u0412\u0435\u0440\u0441\u0438\u044f 1.2.3\n---\n + note.\n", encoding="utf-8"
            )
            packages = root / "packages"
            packages.mkdir()
            for name in ("renpy-1.2.3-bin-x64.zip", "renpy-1.2.3-bin-x86.zip", "renpy-1.2.3-pdb.zip"):
                (packages / name).write_bytes(b"zip")
            # A stale archive that a glob would pick up but the literal list must ignore.
            (packages / "renpy-0.9.0-pdb.zip").write_bytes(b"stale")

            runner_temp = root / "runner"
            runner_temp.mkdir()
            args_file = root / "gh-args.txt"
            script = (
                "function gh {\n"
                "  $args | Set-Content -LiteralPath $env:GH_ARGS_FILE -Encoding utf8\n"
                "  $global:LASTEXITCODE = 0\n"
                "}\n"
                + body
                + "\n"
            )
            script_path = root / "release.ps1"
            script_path.write_text(script, encoding="utf-8")
            environment = {
                **os.environ,
                "GITHUB_REF_NAME": "renpy/v1.2.3",
                "RUNNER_TEMP": str(runner_temp),
                "GH_ARGS_FILE": str(args_file),
            }
            completed = subprocess.run(
                [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script_path)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            recorded = args_file.read_text(encoding="utf-8-sig").splitlines()

        self.assertEqual(
            [line for line in recorded if line.endswith(".zip")],
            [
                "packages/renpy-1.2.3-bin-x64.zip",
                "packages/renpy-1.2.3-bin-x86.zip",
                "packages/renpy-1.2.3-pdb.zip",
            ],
        )
        self.assertIn("create", recorded)
        self.assertIn("renpy/v1.2.3", recorded)
        self.assertIn("--verify-tag", recorded)


class LockMetadataTests(unittest.TestCase):
    """Finding 8: the uv lock's root version must match the project metadata."""

    def test_lock_root_version_matches_the_project_version(self) -> None:
        project = tomllib.loads((REPOSITORY / "build" / "pyproject.toml").read_text(encoding="utf-8"))
        lock = tomllib.loads((REPOSITORY / "build" / "uv.lock").read_text(encoding="utf-8"))
        roots = [package for package in lock["package"] if package["name"] == project["project"]["name"]]
        self.assertEqual(len(roots), 1, "the lock must describe exactly one root package")
        self.assertEqual(
            roots[0]["version"],
            project["project"]["version"],
            "the uv lock's root version is stale relative to build/pyproject.toml",
        )


if __name__ == "__main__":
    unittest.main()

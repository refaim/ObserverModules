"""The workflow pins modern runners/actions and verifies every provisioned tool.

Each downloaded or bootstrapped tool must be an exact, hash-verified pin, and the tool
caches must be restored early and saved *before* the build gates so a failing gate still
leaves the verified tools cached for the next run.  Only a same-run, verified tool is
cached; nothing here reaches a released binary from another run or invents a CodeQL step.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]

# Import the real build driver so the workflow's toolset selection is checked against the
# selector main.py actually uses instead of a re-implementation of it.
sys.path.insert(0, str(REPOSITORY / "build"))
import main  # noqa: E402  (the path above must be set up before this import)

RUNNER = "windows-2025-vs2026"
ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv": "1c37ad07a6a961277cf70c0d37d6f313000f5884",
    "actions/cache/restore": "55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
    "actions/cache/save": "55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
    "actions/upload-artifact": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    "actions/download-artifact": "9000827ccba6bdab643e8b6fd33ac0654aef8333",
    "codecov/codecov-action": "303a32d7a59b442fa8d48b6a1cc6825c09c847a5",
}
CPPCHECK_MSI_SHA = "3929162bab7e71d1b9b18708ec054fb0188bfc8ea1660979ed95837f5a9f547b"
VCPKG_EXE_SHA = "2a4ae1467b9e797964c60c919d057b9e5dc1ec03f4b9632c1150026db25201f2"
VCPKG_BASELINE = "20ceb8adbf0504d4b0d5e4058a32f9d4fc896391"
LLVM_ASSET_SHA = "9ee6a2192fbb30c776415af529927f76ca6b5d1d55699bb1315f9878e610b5b8"



class WorkflowProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.check = (REPOSITORY / ".github" / "workflows" / "ci-check.yml").read_text(encoding="utf-8")
        self.all_workflows = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((REPOSITORY / ".github" / "workflows").glob("*.yml"))
        )

    def _workflow_step(self, name: str) -> str:
        lines = self.check.splitlines()
        for index, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped != f"- name: {name}":
                continue
            indent = line[: len(line) - len(stripped)]
            block = [line]
            for follow in lines[index + 1:]:
                if follow.startswith(f"{indent}- name: "):
                    break
                block.append(follow)
            return "\n".join(block)
        self.fail(f"workflow step not found: {name}")

    def test_every_action_uses_the_exact_pinned_revision(self) -> None:
        for action, revision in ACTIONS.items():
            with self.subTest(action=action):
                self.assertIn(f"uses: {action}@{revision}", self.all_workflows)

    def test_the_existing_v145_toolset_is_verified_without_an_installer(self) -> None:
        step = self._workflow_step("Verify the installed MSVC v145 toolset")
        self.assertIn("vswhere.exe", step)
        self.assertIn("[18.0,19.0)", step)
        self.assertIn("Microsoft.VisualStudio.Component.VC.Tools.x86.x64", step)
        self.assertNotIn("setup.exe", step)
        self.assertNotIn("Spectre", step)
        self.assertNotIn("14.44", step)
        self.assertIn("'x86'", step)
        self.assertIn("'x64'", step)
        condition = next(line.strip() for line in step.splitlines() if line.strip().startswith("if:"))
        stages = json.loads(re.search(r"fromJSON\('([^']+)'\)", condition).group(1))
        self.assertEqual(set(stages), {"source", "native-debug", "native-release", "msvc-analysis",
                                     "clang-tidy", "coverage", "asan", "ubsan", "fuzz"})

    def test_the_source_installer_runs_hidden_and_checks_the_exact_cppcheck_version(self) -> None:
        step = self._workflow_step("Provision source-check tools")

        self.assertIn("Start-Process", step)
        self.assertIn("-WindowStyle Hidden", step)
        self.assertIn("-Wait", step)
        self.assertIn("-PassThru", step)
        # The version regex is anchored to the exact tested CLI line, so a longer version such
        # as 2.22.00 can never satisfy it.
        self.assertIn(r"Cppcheck 2\.22\.0\s*$", step)

    def test_workflow_step_helper_locates_the_final_block_and_reports_a_missing_step(self) -> None:
        # The helper isolates one step by the next ``- name:`` line; the final step has none, so
        # its block must run to the end of the file, and an absent name must fail rather than
        # return an empty block.
        final = self._workflow_step("Upload the native coverage to Codecov")
        self.assertIn("codecov/codecov-action@", final)
        # Only the step's own header mentions a name; no following step was captured.
        self.assertEqual(final.count("- name:"), 1)
        with self.assertRaises(AssertionError):
            self._workflow_step("a step that does not exist")

    def test_the_runner_is_the_modern_vs2026_image(self) -> None:
        self.assertIn(f"runs-on: {RUNNER}", self.check)

    def test_cppcheck_is_an_hash_verified_2_22_msi(self) -> None:
        self.assertIn("2.22", self.check)
        self.assertIn(CPPCHECK_MSI_SHA, self.check.lower())
        self.assertIn("Get-FileHash", self.check)

    def test_vcpkg_is_a_pinned_binary_with_the_unchanged_manifest_baseline(self) -> None:
        self.assertIn(VCPKG_EXE_SHA, self.check.lower())
        self.assertIn("2026-09-26", self.check)
        # The manifest baseline is a compatibility pin shared with the pinned vcpkg binary.
        manifest = json.loads((REPOSITORY / "vcpkg.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["builtin-baseline"], VCPKG_BASELINE)

    def test_llvm_23_is_hash_verified_and_caches_tidy_format_and_headers(self) -> None:
        self.assertIn("23.1.3", self.check)
        self.assertIn(LLVM_ASSET_SHA, self.check.lower())
        for member in ("bin/clang-tidy.exe", "bin/clang-format.exe", "lib/clang/23/include/"):
            with self.subTest(member=member):
                self.assertIn(member, self.check)

    def test_tool_caches_are_saved_before_the_build_gates(self) -> None:
        self.assertIn("actions/cache/save@", self.check)
        save = self.check.index("actions/cache/save@")
        for gate in ("/build.ps1 ci-source", "/build.ps1 ci-arch"):
            with self.subTest(gate=gate):
                self.assertIn(gate, self.check)
                self.assertLess(save, self.check.index(gate))

    def test_code_scanning_upload_does_not_invent_codeql_analysis(self) -> None:
        self.assertIn("github/codeql-action/upload-sarif@", self.all_workflows)
        self.assertNotIn("github/codeql-action/init@", self.all_workflows)
        self.assertNotIn("github/codeql-action/analyze@", self.all_workflows)


class InstalledToolsetPreflightTests(unittest.TestCase):
    def test_real_preflight_selects_numeric_v145_and_checks_both_compilers(self) -> None:
        check = (REPOSITORY / ".github/workflows/ci-check.yml").read_text(encoding="utf-8")
        body = check.split("          # Select the same numeric servicing version as the driver.\n", 1)[1]
        body = body.split("\n      - name:", 1)[0]
        for missing in (None, "x86", "x64", "toolset"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                installation = root / "VS ü with spaces"
                selected = installation / "VC/Tools/MSVC/14.51.10"
                if missing != "toolset":
                    (installation / "VC/Tools/MSVC/14.51.9").mkdir(parents=True)
                    for arch in ("x86", "x64"):
                        if arch != missing:
                            compiler = selected / "bin/Hostx64" / arch / "cl.exe"
                            compiler.parent.mkdir(parents=True, exist_ok=True)
                            compiler.write_bytes(b"fixture")
                    (installation / "VC/Tools/MSVC/14.51.999.backup").mkdir(parents=True)
                output = root / "github-output"
                script = "$ErrorActionPreference='Stop'\n$installation=$env:FIXTURE_INSTALL\n" + body
                completed = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
                    env={**os.environ, "FIXTURE_INSTALL": str(installation), "GITHUB_OUTPUT": str(output)},
                    capture_output=True, text=True, check=False)
                self.assertEqual(completed.returncode == 0, missing is None, completed.stderr)
                if missing is None:
                    self.assertIn("version=14.51.10", output.read_text(encoding="utf-8"))
                    self.assertEqual(main._msvc_toolset_version(installation), "14.51.10")
                else:
                    self.assertIn("MSVC", completed.stderr)


if __name__ == "__main__":
    unittest.main()

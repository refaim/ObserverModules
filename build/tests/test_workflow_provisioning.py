"""The workflow pins modern runners/actions and verifies every provisioned tool.

Each downloaded or bootstrapped tool must be an exact, hash-verified pin, and the tool
caches must be restored early and saved *before* the build gates so a failing gate still
leaves the verified tools cached for the next run.  Only a same-run, verified tool is
cached; nothing here reaches a released binary from another run or invents a CodeQL step.
"""

from __future__ import annotations

import json
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

# A Visual Studio install under a spaced path is the case the workflow must survive: PowerShell
# joins the -ArgumentList array with single spaces, so the --installPath value has to carry its own
# literal quotes or setup.exe receives only "C:\Program" as the path.
SPACED_INSTALL_PATH = r"C:\Program Files\Microsoft Visual Studio\2026\BuildTools"


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

    def test_the_vs2026_legacy_toolset_and_spectre_components_are_provisioned(self) -> None:
        step = self._workflow_step("Verify the legacy MSVC toolset and Spectre libraries")

        # The selected host instance is inspected through the installed vswhere, and only the
        # component IDs that are actually missing are added by the installed VS Installer.
        self.assertIn("vswhere.exe", step)
        self.assertIn("Microsoft.VisualStudio.Component.VC.14.44.17.14.x86.x64", step)
        self.assertIn("Microsoft.VisualStudio.Component.VC.14.44.17.14.x86.x64.Spectre", step)
        self.assertIn("setup.exe", step)
        self.assertIn("'modify'", step)
        # The installer runs hidden and synchronous, and a reboot-only result is not a ready
        # toolset: only a clean exit is accepted.
        self.assertIn("-WindowStyle Hidden", step)
        self.assertIn("-Wait", step)
        self.assertIn("-PassThru", step)
        self.assertIn("ExitCode -ne 0", step)
        self.assertNotIn("3010", step)
        # A physical, both-architecture Spectre library check backs the vswhere report.
        self.assertIn("'x86'", step)
        self.assertIn("'x64'", step)
        self.assertIn("spectre", step)
        # The pinned Windows SDK stays put; the fix never raises the target platform version.
        self.assertNotIn("28000", self.check)

    def test_the_legacy_msvc_preflight_gate_covers_the_source_stage(self) -> None:
        # ci_source restores the pinned x64 vcpkg dependencies through the same MSVC toolset, so a
        # clean runner must not reach a source gate before this preflight has guaranteed the legacy
        # 14.44 toolset and its Spectre libraries.  The gate is the reviewed ``contains`` shape over
        # a ``fromJSON`` stage array, so read that array instead of re-evaluating expressions.
        step = self._workflow_step("Verify the legacy MSVC toolset and Spectre libraries")
        condition = next(
            line.strip()
            for line in step.splitlines()
            if line.strip().startswith("if:")
        )
        match = re.fullmatch(
            r"if:\s*contains\(fromJSON\('(\[.*\])'\),\s*inputs\.stage\)",
            condition,
        )
        self.assertIsNotNone(match, f"unexpected stage gate: {condition}")
        assert match is not None  # narrow for the type checker; the assertion above is the check
        stages = json.loads(match.group(1))
        self.assertEqual(
            set(stages),
            {
                "source", "native-debug", "native-release", "msvc-analysis", "clang-tidy",
                "coverage", "asan", "ubsan", "fuzz",
            },
            "the MSVC/Spectre preflight must cover every MSVC-driven stage, including source",
        )
        # The leak stage measures an already-built artifact and must never provision MSVC.
        self.assertNotIn("leaks", stages)

    def test_the_preflight_requires_spectre_for_the_release_stages_only(self) -> None:
        # Only the stages that link the Spectre-mitigated static runtime need its component
        # installed, so the requirement is a boolean over the stage.  Reading the equalities keeps
        # the assertion on the real expression rather than a re-implementation.
        step = self._workflow_step("Verify the legacy MSVC toolset and Spectre libraries")
        line = next(
            line.strip()
            for line in step.splitlines()
            if line.strip().startswith("UNPACKRAT_REQUIRE_SPECTRE:")
        )
        self.assertIn("${{", line)
        self.assertEqual(
            set(re.findall(r"inputs\.stage == '([^']+)'", line)),
            {"native-release"},
        )

    def _required_components(self, require_spectre: bool) -> list[str]:
        # Run the production array builder, not a re-implementation, in a fresh pwsh with the
        # requirement boolean in scope, and read back the exact component IDs it would request.
        step = self._workflow_step("Verify the legacy MSVC toolset and Spectre libraries")
        fragment = _required_components_fragment(step)
        self.assertNotIn("vswhere", fragment)
        script = (
            f"$requiresSpectre = ${'true' if require_spectre else 'false'}\n"
            f"{fragment}\n"
            "ConvertTo-Json -InputObject @($required) -Compress\n"
        )
        completed = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return json.loads(completed.stdout.strip().lstrip("\ufeff"))

    def test_the_required_components_add_spectre_only_when_the_stage_requires_it(self) -> None:
        normal = "Microsoft.VisualStudio.Component.VC.14.44.17.14.x86.x64"
        spectre = "Microsoft.VisualStudio.Component.VC.14.44.17.14.x86.x64.Spectre"
        # Every MSVC-driven stage keeps the plain v143 toolset component; only a Release-linking
        # stage adds the matching Spectre component.
        self.assertEqual(self._required_components(False), [normal])
        self.assertEqual(self._required_components(True), [normal, spectre])

    def test_the_source_installer_runs_hidden_and_checks_the_exact_cppcheck_version(self) -> None:
        step = self._workflow_step("Provision source-check tools")

        self.assertIn("Start-Process", step)
        self.assertIn("-WindowStyle Hidden", step)
        self.assertIn("-Wait", step)
        self.assertIn("-PassThru", step)
        # The version regex is anchored to the exact tested CLI line, so a longer version such
        # as 2.22.00 can never satisfy it.
        self.assertIn(r"Cppcheck 2\.22\.0\s*$", step)

    def _evaluate_installer_arguments(self, array_expression: str) -> list[str]:
        # Evaluate the exact PowerShell array expression with the spaced install path in scope, so
        # the assertion observes what setup.exe would actually receive instead of the source text.
        script = (
            f"$installation = '{SPACED_INSTALL_PATH}'\n"
            f"$arguments = {array_expression}\n"
            "ConvertTo-Json -InputObject @($arguments) -Compress\n"
        )
        completed = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return json.loads(completed.stdout.strip().lstrip("\ufeff"))

    def _installer_argument_expression(self) -> str:
        step = self._workflow_step("Verify the legacy MSVC toolset and Spectre libraries")
        line = next(
            line.strip()
            for line in step.splitlines()
            if line.strip().startswith("$arguments = ")
        )
        return line.split("= ", 1)[1]

    def test_the_vs_installer_keeps_a_spaced_install_path_as_one_argument(self) -> None:
        arguments = self._evaluate_installer_arguments(self._installer_argument_expression())
        self.assertEqual(arguments[0], "modify")
        self.assertEqual(arguments[1], "--installPath")
        self.assertEqual(
            arguments[2],
            f'"{SPACED_INSTALL_PATH}"',
            "the --installPath value must carry literal quotes so the spaced path survives "
            "the -ArgumentList join",
        )
        self.assertIn("--quiet", arguments)
        self.assertIn("--norestart", arguments)

    def test_the_install_path_check_rejects_an_unquoted_interpolation(self) -> None:
        # A source-level quote check is a false green: PowerShell interpolates both
        # ``"$installation"`` and a bare ``$installation`` into an *unquoted* path, so only the
        # backtick-escaped ``"`"$installation`""`` form yields literal quotes at runtime.
        escaped = self._evaluate_installer_arguments(
            "@('modify', '--installPath', \"`\"$installation`\"\", '--quiet', '--norestart')"
        )
        self.assertEqual(escaped[2], f'"{SPACED_INSTALL_PATH}"')
        for unquoted in (
            "@('modify', '--installPath', \"$installation\", '--quiet', '--norestart')",
            "@('modify', '--installPath', $installation, '--quiet', '--norestart')",
        ):
            with self.subTest(expression=unquoted):
                value = self._evaluate_installer_arguments(unquoted)[2]
                self.assertEqual(value, SPACED_INSTALL_PATH)
                self.assertNotEqual(value, f'"{SPACED_INSTALL_PATH}"')

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


SPECTRE_STEP = "Verify the legacy MSVC toolset and Spectre libraries"
# The production block starts at this comment and runs to the end of its step, so the tests below
# run the workflow's own PowerShell instead of a hand-copied duplicate.
SPECTRE_ANCHOR = "# And the Spectre-mitigated static libraries must exist on disk for both architectures."


def _spectre_library_block(check: str) -> str:
    """Return the workflow's physical Spectre filesystem-check block, verbatim."""

    lines = check.splitlines()
    header = next(
        index for index, line in enumerate(lines) if line.lstrip() == f"- name: {SPECTRE_STEP}"
    )
    indent = lines[header][: len(lines[header]) - len(lines[header].lstrip())]
    start = next(index for index, line in enumerate(lines) if line.strip() == SPECTRE_ANCHOR)
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith(f"{indent}- name: ")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _required_components_fragment(step: str) -> str:
    """Return the workflow's required-component array builder, verbatim.

    It reads ``$requiresSpectre`` from scope and never touches vswhere or the installer, so the
    tests can execute the production fragment in a fresh pwsh and observe the exact list a stage
    would request.
    """

    return "$required =" + step.split("$required =", 1)[1].split("$missing =", 1)[0]


class SpectrePhysicalLibraryTests(unittest.TestCase):
    """The physical Spectre check must read the installed ``lib\\spectre\\<arch>\\libcmt.lib`` files.

    The hosted image keeps the legacy 14.44 v143 toolset under
    ``VC\\Tools\\MSVC\\<version>\\lib\\spectre\\<arch>\\libcmt.lib``; the earlier check probed a
    ``...\\lib\\<arch>\\spectre`` directory the image never creates, so every MSVC-driven job
    failed after a successful VS Installer run.  The check must also validate the one
    numeric-maximum 14.44 servicing directory the build driver forwards, so a stale or
    mixed-version x86/x64 pair can never pass.  Each test runs the extracted production
    PowerShell fragment against a temporary fake installation; the VS Installer is never touched.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.installation = self.root / "installation"
        self.block = _spectre_library_block(
            (REPOSITORY / ".github" / "workflows" / "ci-check.yml").read_text(encoding="utf-8")
        )

    def _library(self, version: str, architecture: str, *, as_directory: bool = False) -> Path:
        directory = (
            self.installation / "VC" / "Tools" / "MSVC" / version / "lib" / "spectre" / architecture
        )
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "libcmt.lib"
        if as_directory:
            target.mkdir()
        else:
            target.write_bytes(b"!<arch>\n")  # a small, synthetic static-library payload
        return target

    def _run(self, *, require_spectre: bool = True) -> subprocess.CompletedProcess[str]:
        literal = "$true" if require_spectre else "$false"
        script = (
            f"$installation = '{self.installation}'\n"
            f"$requiresSpectre = {literal}\n"
            + self.block + "\n"
        )
        path = self.root / "spectre-check.ps1"
        path.write_text(script, encoding="utf-8")
        return subprocess.run(
            ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_the_installed_lib_spectre_layout_passes_and_tracks_the_driver_selector(self) -> None:
        # The older 14.44 servicing directory is empty and must not satisfy the check: only the
        # numeric maximum the driver forwards may carry the libraries.  A string sort would pick
        # the wrong directory here, since "14.44.9" sorts after "14.44.10" as text.
        (self.installation / "VC" / "Tools" / "MSVC" / "14.44.9").mkdir(parents=True)
        self._library("14.44.10", "x86")
        self._library("14.44.10", "x64")

        self.assertEqual(main._msvc_toolset_version(self.installation), "14.44.10")
        completed = self._run()

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_a_missing_x86_library_fails_by_naming_the_expected_file(self) -> None:
        self._library("14.44.35207", "x64")
        missing = (
            self.installation / "VC" / "Tools" / "MSVC" / "14.44.35207"
            / "lib" / "spectre" / "x86" / "libcmt.lib"
        )

        completed = self._run()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(str(missing), completed.stderr + completed.stdout)

    def test_a_missing_x64_library_fails_by_naming_the_expected_file(self) -> None:
        self._library("14.44.35207", "x86")
        missing = (
            self.installation / "VC" / "Tools" / "MSVC" / "14.44.35207"
            / "lib" / "spectre" / "x64" / "libcmt.lib"
        )

        completed = self._run()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(str(missing), completed.stderr + completed.stdout)

    def test_an_empty_library_directory_cannot_stand_in_for_a_static_library(self) -> None:
        self._library("14.44.35207", "x86", as_directory=True)
        self._library("14.44.35207", "x64")
        missing = (
            self.installation / "VC" / "Tools" / "MSVC" / "14.44.35207"
            / "lib" / "spectre" / "x86" / "libcmt.lib"
        )

        completed = self._run()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(str(missing), completed.stderr + completed.stdout)

    def test_mixed_version_libraries_never_satisfy_the_check(self) -> None:
        # x86 comes from the stale 14.44.9 toolset and x64 from the 14.44.10 the driver selects;
        # the pair must not pass because both architectures have to share the selected directory.
        self._library("14.44.9", "x86")
        self._library("14.44.10", "x64")
        missing = (
            self.installation / "VC" / "Tools" / "MSVC" / "14.44.10"
            / "lib" / "spectre" / "x86" / "libcmt.lib"
        )

        self.assertEqual(main._msvc_toolset_version(self.installation), "14.44.10")
        completed = self._run()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(str(missing), completed.stderr + completed.stdout)

    def test_a_root_without_a_legacy_toolset_fails_by_naming_the_root(self) -> None:
        # The filter keeps only ``^14.44(?:\.\d+)+$`` names, so an empty MSVC root and a root
        # holding just non-matching names (a v14x alias or a malformed 14.44 value) both leave
        # $toolsets empty.  The check must fail and name the exact root it searched rather than
        # index an empty array.
        root = self.installation / "VC" / "Tools" / "MSVC"
        diagnostic = f"No legacy 14.44 MSVC toolset below {root}"

        # No MSVC directory at all: Get-ChildItem resolves nothing and the count stays zero.
        completed = self._run()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(diagnostic, completed.stderr + completed.stdout)

        # A present root whose only entries are rejected by the version filter.
        root.mkdir(parents=True, exist_ok=True)
        for name in ("v145", "14.44", "14.44.x", "14.449"):
            (root / name).mkdir()
        completed = self._run()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(diagnostic, completed.stderr + completed.stdout)

    def test_a_stage_that_does_not_require_spectre_passes_without_the_libraries(self) -> None:
        # A Debug or instrumented stage never links the Spectre-mitigated runtime, so the physical
        # probe must not demand its libraries; only the numeric toolset directory is needed.
        (self.installation / "VC" / "Tools" / "MSVC" / "14.44.10").mkdir(parents=True)

        completed = self._run(require_spectre=False)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_a_stage_that_does_not_require_spectre_still_needs_the_numeric_toolset(self) -> None:
        # Dropping the Spectre probe must not drop the numeric-maximum 14.44 directory check.
        root = self.installation / "VC" / "Tools" / "MSVC"

        completed = self._run(require_spectre=False)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(f"No legacy 14.44 MSVC toolset below {root}", completed.stderr + completed.stdout)


if __name__ == "__main__":
    unittest.main()

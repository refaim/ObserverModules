from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]
MSBUILD = "{http://schemas.microsoft.com/developer/msbuild/2003}"
# The exact release provenance line the owner's installed clang-tidy/clang-format print.
VCS_SUFFIX = "(https://github.com/llvm/llvm-project 0d261d1ca552c95a8f007e061c787ac7132fbcbc)"


class ClangTidySelectionTests(unittest.TestCase):
    def _workflow_step(self, name: str) -> str:
        workflow = (ROOT / ".github" / "workflows" / "ci-check.yml").read_text(encoding="utf-8")
        lines = workflow.splitlines()
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

    def setUp(self) -> None:
        # A CI runner may export an inherited override; empty means "no explicit override",
        # so the scoped default resolution the cases below assert stays authoritative.
        ambient = mock.patch.dict(os.environ, {"UNPACKRAT_CLANG_TIDY": ""}, clear=False)
        ambient.start()
        self.addCleanup(ambient.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.exe = self.root / "llvm" / "bin" / "clang-tidy.exe"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b"test executable")

    def _version(self, text: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, text)

    def test_default_resolves_the_pinned_tool_from_the_standard_path(self) -> None:
        with mock.patch("shutil.which", return_value=str(self.exe)) as which, \
             mock.patch.object(
                 main, "run",
                 return_value=self._version("LLVM (https://llvm.org/):\n  LLVM version 23.1.3\n"),
             ) as invoked:
            self.assertEqual(main._clang_tidy(), self.exe.resolve())
        which.assert_called_once_with("clang-tidy.exe")
        invoked.assert_called_once_with([str(self.exe.resolve()), "--version"], capture=True)

    def test_default_never_reads_or_creates_a_user_cache(self) -> None:
        # The owner forbids the private %LOCALAPPDATA% tool cache, so the default lookup must
        # be the standard PATH and must not touch that directory at all.
        cache = self.root / "localappdata"
        with mock.patch("shutil.which", return_value=str(self.exe)), \
             mock.patch.object(main, "run", return_value=self._version("LLVM version 23.1.3")), \
             mock.patch.dict(os.environ, {"LOCALAPPDATA": str(cache)}, clear=True):
            self.assertEqual(main._clang_tidy(), self.exe.resolve())
        self.assertFalse(cache.exists())

    def test_override_selects_exact_executable_without_path_fallback(self) -> None:
        override = self.root / "custom" / "clang-tidy.exe"
        override.parent.mkdir()
        override.write_bytes(b"test executable")
        with mock.patch("shutil.which") as which, \
             mock.patch.object(main, "run", return_value=self._version("LLVM version 23.1.3")):
            with mock.patch.dict(os.environ, {"UNPACKRAT_CLANG_TIDY": str(override)}, clear=True):
                self.assertEqual(main._clang_tidy(), override.resolve())
        which.assert_not_called()

    def test_relative_override_keeps_same_executable_when_msbuild_changes_cwd(self) -> None:
        override = self.root / "custom" / "clang-tidy.exe"
        override.parent.mkdir()
        override.write_bytes(b"test executable")
        original = Path.cwd()
        try:
            os.chdir(self.root)
            with mock.patch.dict(os.environ, {"UNPACKRAT_CLANG_TIDY": "custom/clang-tidy.exe"}, clear=True), \
                 mock.patch.object(main, "run", return_value=self._version("LLVM version 23.1.3")):
                self.assertEqual(main._clang_tidy(), override.resolve())
        finally:
            os.chdir(original)

    def test_missing_default_tool_or_missing_override_fails_closed(self) -> None:
        with mock.patch("shutil.which", return_value=None), self.assertRaises(FileNotFoundError):
            main._clang_tidy()
        with mock.patch.dict(os.environ, {"UNPACKRAT_CLANG_TIDY": str(self.root / "missing.exe")}, clear=True), \
             self.assertRaises(FileNotFoundError):
            main._clang_tidy()

    def test_wrong_or_missing_version_fails_closed(self) -> None:
        for output in (
            "LLVM version 19.1.5",
            "LLVM version 23.1.30",
            "LLVM version 23.1.80",
            "clang-tidy unknown",
        ):
            with self.subTest(output=output), \
                 mock.patch("shutil.which", return_value=str(self.exe)), \
                 mock.patch.object(main, "run", return_value=self._version(output)), \
                 self.assertRaises(main.BuildError):
                main._clang_tidy()

    def test_accepts_the_official_release_vcs_suffix(self) -> None:
        # clang-tidy reports the bare tested line; the same official build may append its
        # git commit, which must still satisfy the pinned-version contract.
        output = f"LLVM (https://llvm.org/):\n  LLVM version 23.1.3 {VCS_SUFFIX}\n"
        with mock.patch("shutil.which", return_value=str(self.exe)), \
             mock.patch.object(main, "run", return_value=self._version(output)):
            self.assertEqual(main._clang_tidy(), self.exe.resolve())

    def test_release_uses_only_pinned_tidy_without_changing_compiler(self) -> None:
        args = (self.root, self.root / "tests.vcxproj", "x64", "Release", 2,
                self.root / "MSBuild.exe", self.root / "vcpkg.exe")
        with mock.patch.object(main, "_clang_tidy", return_value=self.exe) as lookup:
            command = main._msbuild_command(*args)
        lookup.assert_called_once_with()
        self.assertIn(f"/p:ClangTidyToolPath={self.exe.parent}{os.sep}", command)
        self.assertFalse(any(arg.startswith("/p:LLVMInstallDir=") for arg in command))
        with mock.patch.object(main, "_clang_tidy", side_effect=AssertionError("Debug must not look up clang-tidy")):
            debug = main._msbuild_command(args[0], args[1], args[2], "Debug", *args[4:])
        self.assertFalse(any(arg.startswith("/p:ClangTidyToolPath=") for arg in debug))

    def test_vcpkg_is_system_only_in_tidy_analysis(self) -> None:
        project = ET.parse(ROOT / "build" / "unpackrat-project.props").getroot()
        options = project.find(f".//{MSBUILD}ClangTidyAdditionalOptions")
        self.assertIsNotNone(options)
        self.assertIn("unpackratEnableClangTidy", options.attrib["Condition"])
        self.assertIn("-imsvc", options.text)
        # VS feeds /c to clang-tidy as if it were compiling; the analyzer must accept that
        # driver-only flag without weakening any C++ source checks.
        self.assertIn("-Qunused-arguments", options.text)
        self.assertIn("$(VcpkgInstalledDir)$(VcpkgTriplet)", options.text)
        self.assertIn("$(ClangTidyAdditionalOptions)", options.text)
        self.assertNotIn("$(RepositoryRoot)src", options.text)

    def test_existing_ci_documentation_remains_intact(self) -> None:
        docs = (ROOT / "docs" / "build-system.md").read_text(encoding="utf-8")
        # The old monolithic ``native`` slices are documented as replaced by the four real
        # ``native-debug``/``native-release``/``msvc-analysis``/``clang-tidy`` stages, while the
        # still-valid Codecov ``native-<arch>`` flag token remains.
        for token in (
            "`native`", "`native-x86`", "`native-x64`", "`native-<arch>`", "`needs`",
            "`native-debug`", "`native-release`", "`msvc-analysis`", "`clang-tidy`",
        ):
            with self.subTest(token=token):
                self.assertIn(token, docs)

    def test_local_all_order_and_ci_job_limit_are_documented_separately(self) -> None:
        docs = (ROOT / "docs" / "build-system.md").read_text(encoding="utf-8")
        self.assertIn(
            "`all` runs every stage in the fixed order `native`, `coverage`, `asan`, "
            "`ubsan`, `fuzz`, `leaks`",
            docs,
        )
        self.assertIn("CI passes `-Jobs 4`", docs)
        self.assertIn("local runs use the caller's `-Jobs` value", " ".join(docs.split()))

    def test_ci_provisions_only_native_stage_and_pins_archive(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci-check.yml").read_text(encoding="utf-8")
        # The standalone clang-tidy/clang-format tools are provisioned only for the clang-tidy
        # analysis slice and the source format gate; the MSVC build and analysis slices never
        # resolve them.
        self.assertIn("if: inputs.stage == 'clang-tidy' || inputs.stage == 'source'", workflow)
        self.assertIn("UNPACKRAT_CLANG_TIDY", workflow)
        self.assertIn("23.1.3", workflow)
        self.assertIn("9ee6a2192fbb30c776415af529927f76ca6b5d1d55699bb1315f9878e610b5b8", workflow.lower())
        # The verified tool tree is preserved through the split restore/save cache pair: the
        # later save must not replace the completed restore action the cache step now uses.
        self.assertIn("actions/cache/restore@", workflow)
        self.assertIn("actions/cache/save@", workflow)
        self.assertNotIn("actions/cache@", workflow)
        self.assertIn("'bin/run-clang-tidy'", workflow)
        self.assertIn("(root / 'bin/run-clang-tidy').is_file()", workflow)
        self.assertIn("$runner = Join-Path $root 'bin/run-clang-tidy'", workflow)
        self.assertEqual(workflow.count("parallel-cache-v2-9ee6a2192fbb30c776415af529927f76ca6b5d1d55699bb1315f9878e610b5b8"), 2)

    def test_workflow_step_helper_locates_the_final_block_and_reports_a_missing_step(self) -> None:
        # The helper carves one step out by the next ``- name:`` line; the final step has no
        # successor, so its block must reach the end of the file, and a missing name must fail
        # instead of returning an empty block.
        final = self._workflow_step("Upload the native coverage to Codecov")
        self.assertIn("codecov/codecov-action@", final)
        # Only the step's own header mentions a name; no following step was captured.
        self.assertEqual(final.count("- name:"), 1)
        with self.assertRaises(AssertionError):
            self._workflow_step("a step that does not exist")

    def test_ci_scope_probe_classifies_the_foreign_header_dir_with_cl_imsvc(self) -> None:
        step = self._workflow_step("Verify pinned clang-tidy and system-header skipping")
        # Production marks restored vcpkg headers with the cl driver's -imsvc, so the probe
        # must model that exact classification, not the gcc-style -isystem.
        self.assertNotIn("-isystem", step)
        self.assertGreaterEqual(step.count("-imsvc"), 2)
        self.assertGreaterEqual(step.count("--driver-mode=cl"), 2)
        # VS also feeds the restored vcpkg include directory as an ordinary compile-db /I entry,
        # so the same foreign directory must be present both ways and still classify as system.
        self.assertGreaterEqual(step.count("/I$foreign"), 2)

    def test_production_property_marks_only_vcpkg_as_system(self) -> None:
        project = ET.parse(ROOT / "build" / "unpackrat-project.props").getroot()
        options = project.find(f".//{MSBUILD}ClangTidyAdditionalOptions")
        self.assertIsNotNone(options)
        self.assertEqual(
            ['-imsvc"$(VcpkgInstalledDir)$(VcpkgTriplet)\\include"'],
            re.findall(r'-imsvc"[^"]*"', options.text),
        )
        self.assertNotIn("$(RepositoryRoot)src", options.text)


if __name__ == "__main__":
    unittest.main()

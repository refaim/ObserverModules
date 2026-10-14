"""Focused contracts for the independent CI stage dispatch in ``build/main.py``.

The public ``ci-arch`` surface grows four standalone stages beside the legacy
``all`` pipeline: ``native-debug``, ``native-release``, ``msvc-analysis`` and
``clang-tidy``.  These tests pin the observable behaviour of that dispatch:
which suites each stage drives, the exact MSBuild project set and command shape
of the two analysis stages, the analyzer/tidy lookup boundaries, and the frozen
legacy ``all`` order.  Everything external is faked through ``main.run`` and the
tool-lookup seams, so no real compiler, analyzer or network is ever touched.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import main  # noqa: E402


def _properties(argv: tuple[str, ...]) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in argv:
        if item.startswith("/p:") and "=" in item:
            name, _, value = item[len("/p:"):].partition("=")
            found[name] = value
    return found


class RecordingRun:
    """A checked ``main.run`` stand-in that records every invocation."""

    def __init__(self, *, fail_when=None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._fail_when = fail_when

    def __call__(self, argv, cwd=None, env=None, capture=False, **_options: object):
        recorded = tuple(str(item) for item in argv)
        self.calls.append(recorded)
        if self._fail_when is not None and self._fail_when(recorded):
            raise subprocess.CalledProcessError(3, list(recorded), "")
        output = None
        if capture:
            project = Path(recorded[1])
            output = json.dumps({
                "Properties": {"IncludePath": "C:\\sdk\\include"},
                "TargetResults": {"GetCompileCommands": {"Items": [{
                    "ToolPath": "C:\\Windows\\system32\\CL.exe",
                    "WorkingDirectory": str(project.parent),
                    "Identity": "/nologo /c",
                    "Files": str(project.with_suffix(".cpp")),
                }]}}
            })
        return subprocess.CompletedProcess(list(recorded), 0, output)

    def by_tool(self, tool: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if os.path.basename(call[0]).lower() == tool]


class StageCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = mock.patch.dict(os.environ, {"UNPACKRAT_CLANG_TIDY_CACHE": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.msbuild = self.root / "MSBuild.exe"
        self.vcpkg = self.root / "vcpkg.exe"
        self.tidy = self.root / "clang-tidy.exe"
        self.tidy.with_name("run-clang-tidy").write_text("# installed LLVM script", encoding="utf-8")
        # ``_msbuild_command`` forwards the selected legacy servicing toolset.
        self.vs = self.root / "vs"
        (self.vs / "VC" / "Tools" / "MSVC" / "14.44.35307").mkdir(parents=True)

    def tools(self, run: RecordingRun):
        """Patch the tool-lookup seams and the single ``main.run`` entry."""

        self.clang_tidy = mock.Mock(return_value=self.tidy)
        self.cleanup_probe = mock.Mock()
        return mock.patch.multiple(
            main,
            run=run,
            _native_tools=mock.Mock(return_value=(self.msbuild, self.vcpkg)),
            _vs_installation=mock.Mock(return_value=self.vs),
            _clang_tidy=self.clang_tidy,
            _cleanup_fatal_probe=self.cleanup_probe,
            _llvm=mock.Mock(return_value=mock.Mock()),
        )

    # ----- the analysis flag on the ordinary MSBuild command ------------------------------

    def test_release_analysis_flag_gates_the_analyzer_and_the_tidy_lookup(self) -> None:
        run = RecordingRun()
        with self.tools(run):
            analyzed = main._msbuild_command(
                self.repo, self.repo / "build" / "projects" / "tests.vcxproj", "x64", "Release",
                2, self.msbuild, self.vcpkg)
        self.clang_tidy.assert_called_once_with()
        properties = _properties(analyzed)
        self.assertEqual(properties["RunCodeAnalysis"], "true")
        self.assertEqual(properties["unpackratRunCodeAnalysis"], "true")
        self.assertEqual(properties["EnableClangTidyCodeAnalysis"], "true")
        self.assertEqual(properties["unpackratEnableClangTidy"], "true")
        self.assertEqual(properties["ClangTidyToolExeAdditionalOptions"], "-warnings-as-errors=*")

        run = RecordingRun()
        with self.tools(run):
            plain = main._msbuild_command(
                self.repo, self.repo / "build" / "projects" / "tests.vcxproj", "x64", "Release",
                2, self.msbuild, self.vcpkg, analysis=False)
        self.clang_tidy.assert_not_called()
        self.assertFalse(any("CodeAnalysis" in item for item in plain))
        self.assertFalse(any("ClangTidy" in item for item in plain))
        self.assertNotIn("SpectreMitigation", _properties(plain))

    def test_debug_never_carries_the_analyzer(self) -> None:
        run = RecordingRun()
        with self.tools(run):
            command = main._msbuild_command(
                self.repo, self.repo / "build" / "projects" / "tests.vcxproj", "x64", "Debug",
                2, self.msbuild, self.vcpkg)
        self.clang_tidy.assert_not_called()
        self.assertFalse(any("CodeAnalysis" in item for item in command))

    def test_build_and_leak_probe_forward_the_analysis_flag(self) -> None:
        run = RecordingRun()
        with self.tools(run):
            main.build("x64", "Release", repository=self.repo, analysis=False)
        command = run.by_tool("msbuild.exe")[0]
        self.assertFalse(any("CodeAnalysis" in item for item in command))
        self.clang_tidy.assert_not_called()

        run = RecordingRun()
        with self.tools(run):
            main._prepare_leak_probe(self.repo, self.msbuild, self.vcpkg, analysis=False)
        command = run.by_tool("msbuild.exe")[0]
        self.assertIn("/p:Configuration=Release", command)
        self.assertFalse(any("CodeAnalysis" in item for item in command))
        self.clang_tidy.assert_not_called()

    def test_debug_suite_keeps_the_fatal_cleanup_probe_without_analysis(self) -> None:
        run = RecordingRun()
        with self.tools(run):
            main.test("x64", "Debug", repository=self.repo, analysis=False)
        self.cleanup_probe.assert_called_once()
        command = run.by_tool("msbuild.exe")[0]
        self.assertFalse(any("CodeAnalysis" in item for item in command))

        run = RecordingRun()
        with self.tools(run):
            main.test("x64", "Release", repository=self.repo, analysis=False)
        self.cleanup_probe.assert_not_called()

    # ----- the standalone analysis command -------------------------------------------------

    def test_analysis_project_set_is_the_four_modules_tests_and_x64_probe(self) -> None:
        x64 = [path.relative_to(self.repo).as_posix() for path in main._analysis_projects(self.repo, "x64")]
        self.assertEqual(x64, [
            "build/projects/renpy.vcxproj",
            "build/projects/rpgmaker.vcxproj",
            "build/projects/zanzarah.vcxproj",
            "build/projects/godot.vcxproj",
            "build/projects/tests.vcxproj",
            "build/projects/leak-probe.vcxproj",
        ])
        x86 = [path.relative_to(self.repo).as_posix() for path in main._analysis_projects(self.repo, "x86")]
        self.assertEqual(x86, [
            "build/projects/renpy.vcxproj",
            "build/projects/rpgmaker.vcxproj",
            "build/projects/zanzarah.vcxproj",
            "build/projects/godot.vcxproj",
            "build/projects/tests.vcxproj",
        ])

    def test_analysis_command_is_compile_only_with_one_msbuild_node(self) -> None:
        project = self.repo / "build" / "projects" / "renpy.vcxproj"
        run = RecordingRun()
        with self.tools(run):
            msvc = main._analysis_command(
                self.repo, project, "x64", self.msbuild, self.vcpkg,
                tidy=False, clang_tidy=None, jobs=4)
        self.assertIn("/t:ClCompile", msvc)
        self.assertNotIn("/t:Build", msvc)
        self.assertIn("/m:1", msvc)
        self.assertIn("/p:BuildProjectReferences=false", msvc)
        properties = _properties(msvc)
        self.assertEqual(properties["unpackratCompileAnalysis"], "true")
        self.assertEqual(properties["SpectreMitigation"], "false")
        self.assertEqual(properties["unpackratRunCodeAnalysis"], "true")
        self.assertEqual(properties["RunCodeAnalysis"], "true")
        self.assertEqual(properties["unpackratEnableClangTidy"], "false")
        self.assertEqual(properties["EnableClangTidyCodeAnalysis"], "false")
        self.assertNotIn("ClangTidyToolPath", properties)
        self.assertNotIn("MaxNumberOfProcesses", properties)

        with self.tools(run):
            tidy = main._analysis_command(
                self.repo, project, "x64", self.msbuild, self.vcpkg,
                tidy=True, clang_tidy=self.tidy, jobs=4)
        self.assertIn("/t:GetCompileCommands", tidy)
        self.assertIn("/getTargetResult:GetCompileCommands", tidy)
        self.assertIn("/getProperty:IncludePath", tidy)
        self.assertIn("/m:1", tidy)
        self.assertIn("/p:BuildProjectReferences=false", tidy)
        properties = _properties(tidy)
        self.assertEqual(properties["unpackratRunCodeAnalysis"], "false")
        self.assertEqual(properties["RunCodeAnalysis"], "false")
        self.assertEqual(properties["EnableMicrosoftCodeAnalysis"], "false")
        self.assertEqual(properties["unpackratEnableClangTidy"], "true")
        self.assertEqual(properties["EnableClangTidyCodeAnalysis"], "true")
        self.assertNotIn("MaxNumberOfProcesses", properties)
        self.assertNotIn("ClangTidyToolPath", properties)
        self.assertNotIn("ClangTidyToolExe", properties)

    def test_msvc_analysis_limits_vendor_parallel_compilers_without_enabling_mp(self) -> None:
        project = self.repo / "build" / "projects" / "tests.vcxproj"
        with self.tools(RecordingRun()), mock.patch.object(main.os, "cpu_count", return_value=3):
            for jobs, expected in ((2, "2"), (None, "3")):
                with self.subTest(jobs=jobs):
                    command = main._analysis_command(
                        self.repo, project, "x64", self.msbuild, self.vcpkg,
                        tidy=False, clang_tidy=None, jobs=jobs)
                    properties = _properties(command)
                    self.assertIn("/m:1", command)
                    self.assertEqual(properties["UseMultiToolTask"], "true")
                    self.assertEqual(properties["MultiProcMaxCount"], expected)
                    self.assertEqual(properties["EnforceProcessCountAcrossBuilds"], "true")
                    self.assertEqual(properties["EnableClServerMode"], "false")
                    self.assertEqual(properties["unpackratCompileAnalysis"], "true")
                    self.assertEqual(properties["SpectreMitigation"], "false")

            tidy = main._analysis_command(
                self.repo, project, "x64", self.msbuild, self.vcpkg,
                tidy=True, clang_tidy=self.tidy, jobs=2)
            release = main._msbuild_command(
                self.repo, project, "x64", "Release", 2, self.msbuild, self.vcpkg)
        for command in (tidy, release):
            for name in ("UseMultiToolTask", "MultiProcMaxCount",
                         "EnforceProcessCountAcrossBuilds", "EnableClServerMode"):
                self.assertNotIn(name, _properties(command))

    def test_analyze_restores_once_and_resolves_tidy_only_when_requested(self) -> None:
        run = RecordingRun()
        with self.tools(run):
            main._analyze(self.repo, "x64", self.msbuild, self.vcpkg, 3, tidy=False)
        self.clang_tidy.assert_not_called()
        self.assertEqual(len(run.by_tool("vcpkg.exe")), 1)
        builds = run.by_tool("msbuild.exe")
        self.assertEqual(len(builds), len(main._analysis_projects(self.repo, "x64")))
        self.assertTrue(all("/t:ClCompile" in call for call in builds))

        run = RecordingRun()
        with self.tools(run):
            main._analyze(self.repo, "x64", self.msbuild, self.vcpkg, 3, tidy=True)
        self.clang_tidy.assert_called_once_with()
        builds = run.by_tool("msbuild.exe")
        self.assertEqual(len(builds), len(main._analysis_projects(self.repo, "x64")))
        self.assertTrue(all("/t:GetCompileCommands" in call for call in builds))

    # ----- stage dispatch ------------------------------------------------------------------

    def test_native_debug_stage_runs_the_debug_suite_without_analysis(self) -> None:
        captured: list[tuple[str, str, bool]] = []

        def fake_test(architecture, configuration, *, repository, corpus=None, jobs=None, analysis=True):
            captured.append((architecture, configuration, analysis))

        probe = mock.Mock()
        audit = mock.Mock()
        binskim = mock.Mock()
        with mock.patch.multiple(
            main,
            test=fake_test,
            _prepare_leak_probe=probe,
            _audit=audit,
            _binskim=binskim,
            _native_tools=mock.Mock(return_value=(self.msbuild, self.vcpkg)),
        ):
            main.ci_arch(["x86", "x64"], stage="native-debug", repository=self.repo)
        self.assertEqual(captured, [("x86", "Debug", False), ("x64", "Debug", False)])
        probe.assert_not_called()
        audit.assert_not_called()
        binskim.assert_not_called()

    def test_native_release_stage_tests_audits_and_needs_the_x64_probe(self) -> None:
        captured: list[tuple[str, str, bool]] = []

        def fake_test(architecture, configuration, *, repository, corpus=None, jobs=None, analysis=True):
            captured.append((architecture, configuration, analysis))

        probe = mock.Mock()
        audit = mock.Mock()
        binskim = mock.Mock()
        patches = dict(
            test=fake_test,
            _prepare_leak_probe=probe,
            _audit=audit,
            _binskim=binskim,
            _native_tools=mock.Mock(return_value=(self.msbuild, self.vcpkg)),
        )
        with mock.patch.multiple(main, **patches):
            main.ci_arch(["x64"], stage="native-release", repository=self.repo)
        self.assertEqual(captured, [("x64", "Release", False)])
        self.assertEqual(probe.call_args.kwargs["analysis"], False)
        audit.assert_called_once_with(self.repo, "x64")
        binskim.assert_called_once_with(self.repo, "x64")

        captured.clear()
        probe.reset_mock()
        with mock.patch.multiple(main, **patches):
            main.ci_arch(["x86"], stage="native-release", repository=self.repo)
        self.assertEqual(captured, [("x86", "Release", False)])
        probe.assert_not_called()

    def test_msvc_analysis_stage_is_compile_only_and_never_resolves_tidy(self) -> None:
        run = RecordingRun()
        with self.tools(run):
            main.ci_arch(["x86", "x64"], stage="msvc-analysis", repository=self.repo)
        self.clang_tidy.assert_not_called()
        builds = run.by_tool("msbuild.exe")
        self.assertEqual(len(builds), 5 + 6)
        self.assertTrue(all("/t:ClCompile" in call for call in builds))
        self.assertTrue(all("/p:BuildProjectReferences=false" in call for call in builds))
        self.assertTrue(all("/m:1" in call for call in builds))
        self.assertEqual(len(run.by_tool("vcpkg.exe")), 2)
        properties = _properties(builds[0])
        self.assertEqual(properties["unpackratCompileAnalysis"], "true")
        self.assertEqual(properties["EnableClangTidyCodeAnalysis"], "false")

    def test_clang_tidy_stage_targets_tidy_and_resolves_the_pinned_tool(self) -> None:
        run = RecordingRun()
        with self.tools(run):
            main.ci_arch(["x64"], stage="clang-tidy", repository=self.repo, jobs=5)
        self.clang_tidy.assert_called_once_with()
        builds = run.by_tool("msbuild.exe")
        self.assertEqual(len(builds), 6)
        self.assertTrue(all("/t:GetCompileCommands" in call for call in builds))
        properties = _properties(builds[0])
        self.assertNotIn("MaxNumberOfProcesses", properties)
        self.assertEqual(properties["EnableClangTidyCodeAnalysis"], "true")
        runners = [call for call in run.calls if any("run-clang-tidy" in item for item in call)]
        self.assertEqual(len(runners), 6)
        self.assertTrue(all(("-j", "5") == call[call.index("-j"):call.index("-j") + 2]
                            for call in runners))

    def test_legacy_all_runs_the_frozen_order_once_and_never_the_new_stages(self) -> None:
        self.assertEqual(
            main._LEGACY_ALL_STAGES,
            ("native", "coverage", "asan", "ubsan", "fuzz", "leaks"),
        )
        for stage in ("native-debug", "native-release", "msvc-analysis", "clang-tidy"):
            self.assertIn(stage, main.CI_STAGES)
            self.assertNotIn(stage, main._LEGACY_ALL_STAGES)
        seen: list[str] = []
        with mock.patch.object(main, "_dispatch", side_effect=lambda name, *_args: seen.append(name)):
            main.ci_arch(["x86", "x64"], stage="all", repository=self.repo)
        self.assertEqual(seen, ["native", "coverage", "asan", "ubsan", "fuzz", "leaks"])

    def test_analysis_tool_failure_propagates(self) -> None:
        run = RecordingRun(fail_when=lambda argv: os.path.basename(argv[0]).lower() == "msbuild.exe")
        with self.tools(run), self.assertRaises(subprocess.CalledProcessError):
            main.ci_arch(["x64"], stage="msvc-analysis", repository=self.repo)

    def test_analysis_stages_validate_jobs(self) -> None:
        with self.assertRaises(main.BuildError):
            main.ci_arch(["x64"], stage="clang-tidy", repository=self.repo, jobs=0)
        with self.assertRaises(main.BuildError):
            main.ci_arch(["x64"], stage="msvc-analysis", repository=self.repo, jobs=-2)

    # ----- CLI -----------------------------------------------------------------------------

    def test_cli_accepts_the_new_stage_selection(self) -> None:
        with mock.patch.object(main, "ci_arch", return_value=self.repo) as handler:
            self.assertEqual(main.main(["ci-arch", "-Arch", "x64", "-Stage", "msvc-analysis"]), 0)
        self.assertEqual(handler.call_args.kwargs["stage"], "msvc-analysis")
        with mock.patch.object(main, "ci_arch", return_value=self.repo) as handler:
            self.assertEqual(main.main(["ci-arch", "-Arch", "x64", "-Stage", "clang-tidy"]), 0)
        self.assertEqual(handler.call_args.kwargs["stage"], "clang-tidy")


if __name__ == "__main__":
    unittest.main()

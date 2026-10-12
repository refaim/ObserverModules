from __future__ import annotations

from dataclasses import dataclass
from importlib import util as importlib_util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SUPPORT_ROOT = HERE.parent
if str(SUPPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPPORT_ROOT))

# Load exactly the module under test from an explicit path.  A RED harness can pin a frozen copy
# through OBSERVER_SOURCE_MODULE; the default is this tree's sibling, and the assertion fails
# loudly if anything else (for example a stale repository copy) would be imported instead.
MODULE_UNDER_TEST = Path(
    os.environ.get("OBSERVER_SOURCE_MODULE", SUPPORT_ROOT / "source_checks.py")
).resolve()
PSSA_SETTINGS = SUPPORT_ROOT / "PSScriptAnalyzerSettings.psd1"

import native  # noqa: E402


def _load_module_under_test():
    spec = importlib_util.spec_from_file_location("source_checks", MODULE_UNDER_TEST)
    assert spec is not None and spec.loader is not None
    module = importlib_util.module_from_spec(spec)
    sys.modules["source_checks"] = module
    spec.loader.exec_module(module)
    loaded = Path(module.__file__).resolve()
    if loaded != MODULE_UNDER_TEST:
        raise AssertionError(f"loaded {loaded}, expected {MODULE_UNDER_TEST}")
    return module


source_checks = _load_module_under_test()


_SARIF = {"version": "2.1.0", "runs": [{"results": []}]}


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


@dataclass(frozen=True)
class FakeTools:
    pwsh: Path
    clang_format: Path
    cppcheck: Path
    psscriptanalyzer: Path
    llvm_dir: Path
    native: object


@dataclass
class RecordedCall:
    argv: tuple[str, ...]
    cwd: Path | None
    env: dict[str, str] | None


class StubRunner:
    """Record every invocation and materialise the reports the real tools would write."""

    def __init__(self, *, cppcheck: bool = True, msvc: bool = True, tidy: bool = True,
                 failure: type[BaseException] | None = None) -> None:
        self.calls: list[RecordedCall] = []
        self.cppcheck = cppcheck
        self.msvc = msvc
        self.tidy = tidy
        self.failure = failure

    def __call__(self, argv, cwd=None, env=None) -> None:
        recorded = tuple(str(item) for item in argv)
        self.calls.append(RecordedCall(
            recorded,
            None if cwd is None else Path(cwd),
            None if env is None else dict(env),
        ))
        if self.failure is not None:
            raise self.failure
        self.materialise(recorded)

    def materialise(self, argv: tuple[str, ...]) -> None:
        options = {}
        for argument in argv:
            if argument.startswith("/p:") and "=" in argument:
                key, _, value = argument[3:].partition("=")
                options[key] = value
        if "-File" in argv and argv[argv.index("-File") + 1].endswith("psscriptanalyzer.ps1"):
            self.write(Path(argv[argv.index("-Output") + 1]), "[]")
        if self.cppcheck:
            for argument in argv:
                if argument.startswith("--output-file="):
                    self.write(Path(argument.split("=", 1)[1]), json.dumps(_SARIF))
        if self.msvc and "ObserverAnalysisReportPath" in options:
            self.write(Path(options["ObserverAnalysisReportPath"]), json.dumps(_SARIF))
        if self.tidy and "ClangTidyLogFile" in options:
            self.write(Path(options["IntDir"]) / options["ClangTidyLogFile"], "")

    @staticmethod
    def write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class ToolsMixin:
    def tools(self, root: Path) -> FakeTools:
        directory = root / "tools"
        (directory / "llvm").mkdir(parents=True, exist_ok=True)
        return FakeTools(
            pwsh=executable(directory / "pwsh.exe"),
            clang_format=executable(directory / "clang-format.exe"),
            cppcheck=executable(directory / "cppcheck.exe"),
            psscriptanalyzer=executable(directory / "PSScriptAnalyzer.psd1"),
            llvm_dir=directory / "llvm",
            native=native.NativeTools(
                msbuild=executable(directory / "MSBuild.exe"),
                vcpkg=executable(directory / "vcpkg" / "vcpkg.exe"),
            ),
        )


class ValidationTests(ToolsMixin, unittest.TestCase):
    def test_unsupported_empty_and_duplicate_architectures_are_rejected_without_running_a_tool(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            tools = self.tools(root)
            operations = (source_checks.source_checks, source_checks.compiler_analysis)
            cases = (
                ((), "at least one architecture"),
                (("mips",), "unsupported architecture"),
                (("x64", "x64"), "must be unique"),
            )
            for operation in operations:
                for architectures, message in cases:
                    with self.subTest(operation=operation.__name__, architectures=architectures):
                        runner = StubRunner()
                        with self.assertRaisesRegex(ValueError, message):
                            operation(
                                repository, architectures, root / "out",
                                runner=runner, tools=tools,
                            )
                        self.assertEqual(runner.calls, [])

    def test_a_job_count_must_be_a_positive_integer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            tools = self.tools(root)
            for operation in (source_checks.source_checks, source_checks.compiler_analysis):
                with self.subTest(operation=operation.__name__):
                    runner = StubRunner()
                    with self.assertRaisesRegex(ValueError, "jobs"):
                        operation(
                            repository, ("x64",), root / "out",
                            jobs=0, runner=runner, tools=tools,
                        )
                    self.assertEqual(runner.calls, [])


class SourceChecksTests(ToolsMixin, unittest.TestCase):
    def repository(self, root: Path) -> Path:
        files = {
            "src/modules/renpy/pickle.cpp": '#include "pickle.h"\n',
            "src/modules/renpy/pickle.h": "#pragma once\n",
            "build.ps1": "#requires -Version 7.4\n",
            "build/checks/example.ps1": "Set-StrictMode -Version Latest\n",
            "build/tests/example.Tests.ps1": "#requires -Version 7.4\n",
            "build/PSScriptAnalyzerSettings.psd1": "@{}\n",
            "out/ignored.ps1": "Set-StrictMode -Version Latest\n",
            "build/.venv/ignored.ps1": "Set-StrictMode -Version Latest\n",
            "notes.txt": "scratch\n",
        }
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    def test_source_checks_restores_analyzes_formats_pssa_contracts_and_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo").resolve()
            tools = self.tools(root)
            output = root / "out"
            resolved = output.resolve()
            runner = StubRunner()

            reports = source_checks.source_checks(
                repository, ("x86", "x64"), output, jobs=3, runner=runner, tools=tools,
            )

            # The pinned third-party tree is restored before any analyzer that reads it.
            self.assertEqual(
                tuple(call.argv for call in runner.calls[:2]),
                tuple(
                    native.restore_command(repository, architecture, tools.native)
                    for architecture in ("x86", "x64")
                ),
            )
            self.assertTrue(all(call.cwd == repository for call in runner.calls[:2]))

            # One clang-format command over every owned C++ source.
            formatted = next(
                call for call in runner.calls if call.argv[0] == str(tools.clang_format)
            )
            cpp_sources = sorted(
                str(path)
                for path in (repository / "src").rglob("*")
                if path.is_file() and path.suffix in {".cpp", ".h", ".hpp"}
            )
            self.assertEqual(
                formatted.argv,
                (str(tools.clang_format), "--dry-run", "--Werror", *cpp_sources),
            )
            self.assertEqual(formatted.cwd, repository)

            # Cppcheck keeps the per-architecture platform, define, third-party include and gate
            # inputs, and its SARIF run is stamped for the merge.
            cppcheck_calls = [
                call for call in runner.calls if call.argv[0] == str(tools.cppcheck)
            ]

            def expected_cppcheck(architecture: str, platform: str, define: str):
                triplet = native.triplet(architecture)
                include = native.restore_root(repository, architecture) / triplet / "include"
                return (
                    str(tools.cppcheck),
                    str(repository / "src"),
                    "--std=c++23",
                    f"--platform={platform}",
                    "-DWIN32=1",
                    "-D_WIN32=1",
                    "-DUNICODE=1",
                    "-D_UNICODE=1",
                    f"-D{define}",
                    f"-I{repository / 'src'}",
                    f"-I{include}",
                    "--enable=warning,style,performance,portability",
                    "--check-level=exhaustive",
                    "--inconclusive",
                    "--inline-suppr",
                    "--suppress=missingIncludeSystem",
                    "--suppress=uninitMemberVarNoCtor:src/api.h",
                    f"--suppress=*:*\\{triplet}\\include\\*",
                    "--suppress=functionStatic",
                    f"--relative-paths={repository}",
                    "--output-format=sarif",
                    f"--output-file={resolved / f'cppcheck-{architecture}.sarif'}",
                    f"--cppcheck-build-dir={resolved / f'cppcheck-{architecture}-build'}",
                )

            self.assertEqual(len(cppcheck_calls), 2)
            self.assertEqual(cppcheck_calls[0].argv, expected_cppcheck("x86", "win32W", "_M_IX86=600"))
            self.assertEqual(cppcheck_calls[1].argv, expected_cppcheck("x64", "win64", "_M_X64=100"))
            self.assertEqual(cppcheck_calls[0].cwd, repository)
            stamped = json.loads((resolved / "cppcheck-x86.sarif").read_text(encoding="utf-8"))
            self.assertEqual(
                stamped["runs"][0]["automationDetails"]["id"], "cppcheck/x86/0/"
            )

            # One PSScriptAnalyzer script over the whole owned PowerShell list, rendered scripts
            # and transient build state excluded.
            pssa = next(
                call
                for call in runner.calls
                if call.argv[0] == str(tools.pwsh)
                and "-File" in call.argv
                and call.argv[call.argv.index("-File") + 1].endswith("psscriptanalyzer.ps1")
            )
            self.assertIn(
                str(repository / "build" / "PSScriptAnalyzerSettings.psd1"), pssa.argv
            )
            self.assertEqual(
                pssa.argv[-3:],
                (
                    str(repository / "build.ps1"),
                    str(repository / "build" / "checks" / "example.ps1"),
                    str(repository / "build" / "tests" / "example.Tests.ps1"),
                ),
            )

            # Every repository contract runs, and nothing under ``out`` or ``.venv`` is scanned.
            contract = next(
                call
                for call in runner.calls
                if len(call.argv) == 6
                and call.argv[5] == str(repository / "build" / "tests" / "example.Tests.ps1")
            )
            self.assertEqual(
                contract.argv,
                (
                    str(tools.pwsh),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(repository / "build" / "tests" / "example.Tests.ps1"),
                ),
            )
            scanned = pssa.argv[pssa.argv.index("-Output") + 2:]
            self.assertNotIn(str(repository / "out" / "ignored.ps1"), scanned)
            self.assertNotIn(str(repository / "build" / ".venv" / "ignored.ps1"), scanned)

            # The findings are normalized, merged and gated through the shared core.sarif CLI.
            merge = next(call for call in runner.calls if call.argv[3] == "merge")
            self.assertEqual(merge.argv[:4], (sys.executable, "-m", "core.sarif", "merge"))
            self.assertEqual(
                merge.argv[4:-2],
                (
                    str(resolved / "cppcheck-x86.sarif"),
                    str(resolved / "cppcheck-x64.sarif"),
                    str(resolved / "psscriptanalyzer.sarif"),
                ),
            )
            self.assertEqual(merge.argv[-2:], ("--output-name", "analysis.sarif"))
            self.assertEqual(merge.cwd, repository / "build")
            self.assertEqual(merge.env["OBSERVER_OUT_DIR"], str(resolved))
            gate = next(call for call in runner.calls if call.argv[3] == "gate")
            self.assertEqual(
                gate.argv,
                (sys.executable, "-m", "core.sarif", "gate", str(resolved / "analysis.sarif")),
            )
            self.assertEqual(reports, (resolved / "analysis.sarif",))

    def test_source_checks_skip_format_and_pssa_when_no_sources_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            tools = self.tools(root)
            output = root / "out"
            runner = StubRunner()

            reports = source_checks.source_checks(
                repository, ("x64",), output, runner=runner, tools=tools,
            )

            self.assertFalse(
                any(call.argv[0] == str(tools.clang_format) for call in runner.calls)
            )
            self.assertFalse(any(call.argv[0] == str(tools.pwsh) for call in runner.calls))
            self.assertTrue(any(call.argv[0] == str(tools.cppcheck) for call in runner.calls))
            self.assertEqual(reports, (output.resolve() / "analysis.sarif",))

    def test_source_checks_fail_when_cppcheck_produces_no_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            tools = self.tools(root)

            with self.assertRaisesRegex(FileNotFoundError, "Cppcheck did not produce"):
                source_checks.source_checks(
                    repository, ("x64",), root / "out",
                    runner=StubRunner(cppcheck=False), tools=tools,
                )

    def test_source_checks_reject_a_cppcheck_report_without_runs(self) -> None:
        class BrokenRunner(StubRunner):
            def materialise(self, argv: tuple[str, ...]) -> None:
                for argument in argv:
                    if argument.startswith("--output-file="):
                        self.write(
                            Path(argument.split("=", 1)[1]),
                            json.dumps({"version": "2.1.0", "runs": {}}),
                        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            tools = self.tools(root)

            with self.assertRaisesRegex(ValueError, "no runs"):
                source_checks.source_checks(
                    repository, ("x64",), root / "out",
                    runner=BrokenRunner(), tools=tools,
                )

    def test_the_operation_defaults_to_the_located_tools_and_the_native_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            tools = self.tools(root)
            execute = StubRunner()

            with (
                mock.patch.object(source_checks, "locate_tools", return_value=tools) as locate,
                mock.patch.object(source_checks.native, "run_tool", execute),
            ):
                reports = source_checks.source_checks(
                    repository, ("x64",), root / "out", jobs=2
                )

        locate.assert_called_once_with()
        self.assertTrue(execute.calls)
        self.assertEqual(reports, ((root / "out").resolve() / "analysis.sarif",))

    def test_source_checks_create_the_cppcheck_build_directory_before_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            tools = self.tools(root)
            observed: dict[str, bool] = {}

            class Observing(StubRunner):
                def __call__(self, argv, cwd=None, env=None) -> None:
                    for argument in argv:
                        if argument.startswith("--cppcheck-build-dir="):
                            observed["exists"] = Path(argument.split("=", 1)[1]).is_dir()
                    super().__call__(argv, cwd=cwd, env=env)

            source_checks.source_checks(
                repository, ("x64",), root / "out", runner=Observing(), tools=tools,
            )
            self.assertTrue(observed.get("exists", False))


class CompilerAnalysisTests(ToolsMixin, unittest.TestCase):
    def repository(self, root: Path) -> Path:
        files = {
            "src/modules/renpy/pickle.cpp": '#include "pickle.h"\n',
            "src/modules/renpy/pickle.h": "#pragma once\n",
            "build/projects/renpy.vcxproj": "<Project />\n",
            "build/projects/rpgmaker.vcxproj": "<Project />\n",
            "build/projects/leak-probe.vcxproj": "<Project />\n",
        }
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    def test_compiler_analysis_analyzes_every_project_and_gates_each_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo").resolve()
            tools = self.tools(root)
            output = root / "out"
            resolved = output.resolve()
            runner = StubRunner()

            reports = source_checks.compiler_analysis(
                repository, ("x64",), output, jobs=5, runner=runner, tools=tools,
            )

            self.assertEqual(reports, (resolved / "x64" / "analysis.sarif",))
            self.assertEqual(
                runner.calls[0].argv,
                native.restore_command(repository, "x64", tools.native),
            )

            # Every production project is compiled for the analyzer: once for MSVC /analyze and
            # once for clang-tidy, into isolated output variants.
            msbuild_calls = [
                call for call in runner.calls if call.argv[0] == str(tools.native.msbuild)
            ]
            projects = ("leak-probe", "renpy", "rpgmaker")
            self.assertEqual(
                [call.argv[1] for call in msbuild_calls],
                [
                    str(repository / "build" / "projects" / f"{name}.vcxproj")
                    for name in projects
                    for _backend in ("msvc", "tidy")
                ],
            )

            def expected_msvc(name: str, configuration: str):
                variant = resolved / "x64" / "raw" / "msvc" / name
                return (
                    str(tools.native.msbuild),
                    str(repository / "build" / "projects" / f"{name}.vcxproj"),
                    "/nologo",
                    "/m:5",
                    "/nr:false",
                    "/t:Rebuild",
                    "/p:BuildProjectReferences=false",
                    f"/p:Configuration={configuration}",
                    "/p:Platform=x64",
                    f"/p:OutDir={variant}{os.sep}",
                    f"/p:IntDir={variant / 'obj'}{os.sep}",
                    "/p:ObserverCompileAnalysis=true",
                    f"/p:VcpkgRoot={tools.native.vcpkg_root}",
                    f"/p:VcpkgInstalledDir={native.restore_root(repository, 'x64')}{os.sep}",
                    "/p:ObserverRunCodeAnalysis=true",
                    "/p:RunCodeAnalysis=true",
                    "/p:EnableMicrosoftCodeAnalysis=true",
                    "/p:ObserverEnableClangTidy=false",
                    f"/p:ObserverAnalysisReportName={name}",
                    f"/p:ObserverAnalysisReportPath={variant / f'{name}.sarif'}",
                )

            def expected_tidy(name: str, configuration: str):
                variant = resolved / "x64" / "raw" / "tidy" / name
                return (
                    str(tools.native.msbuild),
                    str(repository / "build" / "projects" / f"{name}.vcxproj"),
                    "/nologo",
                    "/m:5",
                    "/nr:false",
                    "/t:Rebuild",
                    "/p:BuildProjectReferences=false",
                    f"/p:Configuration={configuration}",
                    "/p:Platform=x64",
                    f"/p:OutDir={variant}{os.sep}",
                    f"/p:IntDir={variant / 'obj'}{os.sep}",
                    "/p:ObserverCompileAnalysis=true",
                    f"/p:VcpkgRoot={tools.native.vcpkg_root}",
                    f"/p:VcpkgInstalledDir={native.restore_root(repository, 'x64')}{os.sep}",
                    "/p:ObserverRunCodeAnalysis=true",
                    "/p:RunCodeAnalysis=true",
                    "/p:EnableMicrosoftCodeAnalysis=false",
                    "/p:ObserverEnableClangTidy=true",
                    f"/p:LLVMInstallDir={tools.llvm_dir}",
                    f"/p:ClangTidyLogFile={name}.ClangTidy.log",
                )

            self.assertEqual(msbuild_calls[0].argv, expected_msvc("leak-probe", "Release"))
            self.assertEqual(msbuild_calls[1].argv, expected_tidy("leak-probe", "Release"))
            self.assertEqual(msbuild_calls[2].argv, expected_msvc("renpy", "Debug"))
            self.assertEqual(msbuild_calls[3].argv, expected_tidy("renpy", "Debug"))
            self.assertTrue(all(call.cwd == repository for call in msbuild_calls))

            # Raw MSVC SARIF and clang-tidy logs are normalized then merged and gated per arch.
            normalize = next(call for call in runner.calls if call.argv[3] == "normalize-msvc")
            self.assertEqual(
                normalize.argv,
                (
                    sys.executable,
                    "-m",
                    "core.sarif",
                    "normalize-msvc",
                    str(resolved / "x64" / "raw" / "msvc" / "leak-probe" / "leak-probe.sarif"),
                    "msvc-analyze/x64/leak-probe/",
                    "--output-name",
                    "x64/msvc/leak-probe.sarif",
                ),
            )
            convert = next(call for call in runner.calls if call.argv[3] == "convert-tidy")
            self.assertEqual(
                convert.argv,
                (
                    sys.executable,
                    "-m",
                    "core.sarif",
                    "convert-tidy",
                    str(repository),
                    str(resolved / "x64" / "raw" / "tidy" / "leak-probe" / "obj"),
                    "clang-tidy/x64/leak-probe/",
                    "--output-name",
                    "x64/tidy/leak-probe.sarif",
                ),
            )
            merge = next(call for call in runner.calls if call.argv[3] == "merge")
            self.assertEqual(merge.argv[:4], (sys.executable, "-m", "core.sarif", "merge"))
            self.assertEqual(
                merge.argv[4:-2],
                tuple(
                    str(resolved / "x64" / backend / f"{name}.sarif")
                    for name in projects
                    for backend in ("msvc", "tidy")
                ),
            )
            self.assertEqual(merge.argv[-2:], ("--output-name", "x64/analysis.sarif"))
            self.assertEqual(merge.env["OBSERVER_OUT_DIR"], str(resolved))
            gate = next(call for call in runner.calls if call.argv[3] == "gate")
            self.assertEqual(
                gate.argv,
                (sys.executable, "-m", "core.sarif", "gate", str(resolved / "x64" / "analysis.sarif")),
            )

    def test_compiler_analysis_skips_the_leak_probe_for_non_x64(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo").resolve()
            tools = self.tools(root)
            output = root / "out"
            runner = StubRunner()

            reports = source_checks.compiler_analysis(
                repository, ("x86",), output, runner=runner, tools=tools,
            )

            self.assertEqual(reports, (output.resolve() / "x86" / "analysis.sarif",))
            compiled = [
                call.argv[1]
                for call in runner.calls
                if call.argv[0] == str(tools.native.msbuild)
            ]
            self.assertEqual(
                compiled,
                [
                    str(repository / "build" / "projects" / f"{name}.vcxproj")
                    for name in ("renpy", "rpgmaker")
                    for _backend in ("msvc", "tidy")
                ],
            )
            self.assertFalse(any("leak-probe" in argument for argument in compiled))

    def test_compiler_analysis_fail_when_msvc_produces_no_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            tools = self.tools(root)

            with self.assertRaisesRegex(FileNotFoundError, "MSVC analysis did not produce"):
                source_checks.compiler_analysis(
                    repository, ("x64",), root / "out",
                    runner=StubRunner(msvc=False), tools=tools,
                )

    def test_compiler_analysis_fail_when_clang_tidy_produces_no_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            tools = self.tools(root)

            with self.assertRaisesRegex(FileNotFoundError, "clang-tidy did not produce"):
                source_checks.compiler_analysis(
                    repository, ("x64",), root / "out",
                    runner=StubRunner(tidy=False), tools=tools,
                )

    def test_compiler_analysis_compiles_fuzz_projects_without_linking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo").resolve()
            fuzz = repository / "build" / "projects" / "fuzz-renpy.vcxproj"
            fuzz.write_text(
                "<Project>\n"
                "  <Import Project=\"..\\ObserverFuzz.props\" />\n"
                "  <ItemGroup>\n"
                "    <ClCompile Include=\"$(RepositoryRoot)src\\fuzz\\archive.cpp\" />\n"
                "  </ItemGroup>\n"
                "</Project>\n",
                encoding="utf-8",
            )
            tools = self.tools(root)
            runner = StubRunner()

            source_checks.compiler_analysis(
                repository, ("x64",), root / "out", runner=runner, tools=tools,
            )

            fuzz_calls = [
                call for call in runner.calls
                if call.argv[0] == str(tools.native.msbuild) and call.argv[1] == str(fuzz)
            ]
            self.assertEqual(len(fuzz_calls), 2)
            for call in fuzz_calls:
                self.assertIn("/t:ClCompile", call.argv)
                self.assertNotIn("/t:Rebuild", call.argv)
                # A compile-only target would otherwise reuse a stale incremental analysis.
                self.assertIn("/p:ForceRebuild=true", call.argv)

    def test_compiler_analysis_defaults_to_the_located_analysis_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            tools = self.tools(root)
            execute = StubRunner()

            with (
                mock.patch.object(
                    source_checks, "locate_analysis_tools", return_value=tools
                ) as locate,
                mock.patch.object(source_checks.native, "run_tool", execute),
            ):
                reports = source_checks.compiler_analysis(
                    repository, ("x64",), root / "out", jobs=2
                )

        locate.assert_called_once_with()
        self.assertTrue(execute.calls)
        self.assertEqual(reports, ((root / "out").resolve() / "x64" / "analysis.sarif",))


class LocateToolsTests(unittest.TestCase):
    def test_locate_tools_resolves_standalone_source_tools_without_llvm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pwsh = executable(root / "pwsh.exe")
            clang_format = executable(root / "clang-format.exe")
            cppcheck = executable(root / "cppcheck.exe")
            pssa = executable(root / "PSScriptAnalyzer.psd1")
            native_tools = native.NativeTools(
                msbuild=executable(root / "MSBuild.exe"),
                vcpkg=executable(root / "vcpkg" / "vcpkg.exe"),
            )
            found = {
                "pwsh": str(pwsh),
                "clang-format": str(clang_format),
                "cppcheck": str(cppcheck),
            }
            completed = SimpleNamespace(stdout=str(pssa) + "\n")
            with (
                mock.patch.object(source_checks.shutil, "which", side_effect=found.get),
                mock.patch.object(
                    source_checks.subprocess, "run", return_value=completed
                ) as run,
                mock.patch.object(
                    source_checks.native, "locate_tools", return_value=native_tools
                ) as locate,
                mock.patch.object(source_checks, "discover_msvc_toolchain") as toolchain,
            ):
                tools = source_checks.locate_tools()

        toolchain.assert_not_called()
        locate.assert_called_once_with()
        self.assertEqual(tools.pwsh, pwsh.resolve())
        self.assertEqual(tools.clang_format, clang_format.resolve())
        self.assertEqual(tools.cppcheck, cppcheck.resolve())
        self.assertEqual(tools.psscriptanalyzer, pssa.resolve())
        self.assertEqual(tools.native, native_tools)
        self.assertIsNone(tools.llvm_dir)
        self.assertEqual(run.call_args.args[0][0], str(pwsh.resolve()))

    def test_locate_analysis_tools_adds_the_llvm_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = source_checks.CheckTools(
                pwsh=root / "pwsh.exe",
                clang_format=root / "clang-format.exe",
                cppcheck=root / "cppcheck.exe",
                psscriptanalyzer=root / "PSScriptAnalyzer.psd1",
                native=native.NativeTools(
                    msbuild=root / "MSBuild.exe", vcpkg=root / "vcpkg" / "vcpkg.exe"
                ),
            )
            toolchain = SimpleNamespace(llvm_dir=root / "llvm")
            with (
                mock.patch.object(
                    source_checks, "discover_msvc_toolchain", return_value=toolchain
                ) as discover,
                mock.patch.object(source_checks, "locate_tools", return_value=base),
            ):
                tools = source_checks.locate_analysis_tools()

        discover.assert_called_once_with()
        self.assertEqual(tools.llvm_dir, root / "llvm")
        self.assertEqual(tools.pwsh, base.pwsh)

    def test_a_missing_standalone_tool_is_reported(self) -> None:
        with mock.patch.object(source_checks.shutil, "which", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "not found on PATH"):
                source_checks._on_path("clang-format")

    def test_an_empty_psscriptanalyzer_query_is_reported(self) -> None:
        completed = SimpleNamespace(stdout="\n")
        with mock.patch.object(source_checks.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(FileNotFoundError, "PSScriptAnalyzer was not found"):
                source_checks._psscriptanalyzer_path(Path("pwsh.exe"))

    def test_locate_tools_falls_back_to_the_visual_studio_clang_format(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vs = root / "VS"
            msbuild = executable(vs / "MSBuild" / "Current" / "Bin" / "amd64" / "MSBuild.exe")
            formatter = executable(vs / "VC" / "Tools" / "Llvm" / "x64" / "bin" / "clang-format.exe")
            pwsh = executable(root / "pwsh.exe")
            cppcheck = executable(root / "cppcheck.exe")
            pssa = executable(root / "PSScriptAnalyzer.psd1")
            native_tools = native.NativeTools(
                msbuild=msbuild, vcpkg=executable(root / "vcpkg" / "vcpkg.exe")
            )
            # clang-format is deliberately absent from PATH; the VS formatter is the fallback.
            found = {"pwsh": str(pwsh), "cppcheck": str(cppcheck)}
            completed = SimpleNamespace(stdout=str(pssa) + "\n")
            with (
                mock.patch.object(source_checks.shutil, "which", side_effect=found.get),
                mock.patch.object(source_checks.subprocess, "run", return_value=completed),
                mock.patch.object(
                    source_checks.native, "locate_tools", return_value=native_tools
                ),
                mock.patch.object(source_checks, "discover_msvc_toolchain") as toolchain,
            ):
                tools = source_checks.locate_tools()

        toolchain.assert_not_called()
        self.assertEqual(tools.clang_format, formatter.resolve())
        self.assertIsNone(tools.llvm_dir)

    def test_locate_tools_reports_a_formatter_missing_from_path_and_visual_studio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            msbuild = executable(root / "VS" / "MSBuild" / "Current" / "Bin" / "MSBuild.exe")
            pwsh = executable(root / "pwsh.exe")
            cppcheck = executable(root / "cppcheck.exe")
            pssa = executable(root / "PSScriptAnalyzer.psd1")
            native_tools = native.NativeTools(
                msbuild=msbuild, vcpkg=executable(root / "vcpkg" / "vcpkg.exe")
            )
            found = {"pwsh": str(pwsh), "cppcheck": str(cppcheck)}
            completed = SimpleNamespace(stdout=str(pssa) + "\n")
            with (
                mock.patch.object(source_checks.shutil, "which", side_effect=found.get),
                mock.patch.object(source_checks.subprocess, "run", return_value=completed),
                mock.patch.object(
                    source_checks.native, "locate_tools", return_value=native_tools
                ),
            ):
                with self.assertRaisesRegex(FileNotFoundError, "clang-format"):
                    source_checks.locate_tools()


class PssaConversionTests(unittest.TestCase):
    def test_findings_convert_to_sarif_with_severity_mapping_and_grouping(self) -> None:
        repository = Path("C:/repo")
        sources = (repository / "a.ps1", repository / "b.ps1")
        # The collector emits raw PSScriptAnalyzer records; Python maps the raw field names.
        findings = [
            {"RuleName": "PSUseSingularNouns", "Severity": "Warning", "Message": "w",
             "Line": 3, "Column": 5, "ScriptPath": str(repository / "a.ps1")},
            {"RuleName": "PSAvoidUsingWriteHost", "Severity": "Error", "Message": "e",
             "Line": 1, "Column": 1, "ScriptPath": str(repository / "a.ps1")},
            {"RuleName": "PSUseShouldProcessForStateChangingFunctions", "Severity": "Information",
             "Message": "i", "Line": 9, "Column": 2, "ScriptPath": str(repository / "c.ps1")},
        ]
        runs = source_checks._pssa_runs(repository, sources, findings)
        self.assertEqual(
            [run["automationDetails"]["id"] for run in runs],
            ["psscriptanalyzer/a.ps1/", "psscriptanalyzer/b.ps1/", "psscriptanalyzer/c.ps1/"],
        )
        self.assertEqual([item["level"] for item in runs[0]["results"]], ["warning", "error"])
        self.assertEqual(runs[1]["results"], [])
        self.assertEqual(runs[2]["results"][0]["level"], "note")
        self.assertEqual(
            runs[0]["results"][0]["locations"][0]["physicalLocation"]["region"],
            {"startLine": 3, "startColumn": 5},
        )
        self.assertEqual(runs[0]["tool"]["driver"]["name"], "PSScriptAnalyzer")

    def test_findings_must_be_a_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "findings.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be a list"):
                source_checks._read_findings(path)

    def test_a_finding_outside_the_repository_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the repository"):
            source_checks._uri(Path("C:/repo"), Path("D:/other/x.ps1"))


_BRANCH_COUNT_QUERY = """
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:OBSERVER_COLLECTOR, [ref]$tokens, [ref]$errors)
$branches = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -or
    $node -is [System.Management.Automation.Language.ForEachStatementAst] -or
    $node -is [System.Management.Automation.Language.ForStatementAst] -or
    $node -is [System.Management.Automation.Language.WhileStatementAst] -or
    $node -is [System.Management.Automation.Language.SwitchStatementAst] -or
    $node -is [System.Management.Automation.Language.TryStatementAst]
}, $true).Count
"BRANCHES=$branches"
"STATEMENTS=$($ast.EndBlock.Statements.Count)"
"""


class CollectorTests(unittest.TestCase):
    """Run the real branchless PSScriptAnalyzer collector through the installed tooling."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pwsh = shutil.which("pwsh")
        cls.collector = (
            Path(source_checks.__file__).resolve().parent / "checks" / "psscriptanalyzer.ps1"
        )
        cls.module: str | None = None
        if cls.pwsh is not None:
            probe = subprocess.run(
                [cls.pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                 source_checks._PSSA_QUERY],
                capture_output=True, text=True,
            )
            cls.module = probe.stdout.strip() or None

    def setUp(self) -> None:
        if self.pwsh is None or self.module is None:
            self.skipTest("pwsh with PSScriptAnalyzer is not installed")

    def _collect(self, sources: tuple[Path, ...]) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "findings.json"
            result = subprocess.run(
                [
                    self.pwsh, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-File", str(self.collector),
                    "-Module", str(self.module),
                    "-Settings", str(PSSA_SETTINGS),
                    "-Output", str(output),
                    *(str(source) for source in sources),
                ],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.is_file(), result.stderr)
            document = json.loads(output.read_text(encoding="utf-8-sig"))
        self.assertIsInstance(document, list)
        return document

    def test_an_empty_source_list_produces_an_empty_list(self) -> None:
        self.assertEqual(self._collect(()), [])

    def test_a_single_source_emits_raw_analyzer_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "single.ps1"
            source.write_text("function Get-Things { }\n", encoding="utf-8")

            findings = self._collect((source,))

        self.assertTrue(findings)
        for finding in findings:
            for key in ("RuleName", "Severity", "Message", "Line", "Column", "ScriptPath"):
                self.assertIn(key, finding)
            self.assertIsInstance(finding["Severity"], str)
            self.assertEqual(finding["ScriptPath"], str(source))
        self.assertIn("PSUseSingularNouns", [finding["RuleName"] for finding in findings])

    def test_multiple_sources_map_through_the_python_sarif_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            first = repository / "first.ps1"
            second = repository / "second.ps1"
            first.write_text("function Get-Things { }\n", encoding="utf-8")
            second.write_text("function Get-Boxes { }\n", encoding="utf-8")

            findings = self._collect((first, second))
            runs = source_checks._pssa_runs(repository, (first, second), findings)

        self.assertEqual(
            [run["automationDetails"]["id"] for run in runs],
            ["psscriptanalyzer/first.ps1/", "psscriptanalyzer/second.ps1/"],
        )
        self.assertTrue(all(run["results"] for run in runs))
        self.assertEqual(runs[0]["results"][0]["level"], "warning")
        self.assertEqual(
            runs[0]["results"][0]["locations"][0]["physicalLocation"]["region"]["startColumn"],
            10,
        )

    def test_a_missing_analyzer_module_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "findings.json"
            result = subprocess.run(
                [
                    self.pwsh, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-File", str(self.collector),
                    "-Module", str(Path(temporary) / "missing.psd1"),
                    "-Settings", str(PSSA_SETTINGS),
                    "-Output", str(output),
                ],
                capture_output=True, text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.is_file())

    def test_the_remaining_collector_has_no_conditional_branches(self) -> None:
        # The collector has zero conditional or loop statements.  Measured with Set-PSBreakpoint
        # line hits over the empty/single/multiple/failure fixture, the behavior tests execute all
        # 5/5 of its statements (out/sol-team/interop-evidence/ps-executed-coverage.txt); this test
        # keeps the zero-branch half of that guarantee in the suite.
        result = subprocess.run(
            [
                self.pwsh, "-NoLogo", "-NoProfile", "-NonInteractive",
                "-Command", _BRANCH_COUNT_QUERY,
            ],
            capture_output=True, text=True,
            env={**os.environ, "OBSERVER_COLLECTOR": str(self.collector)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BRANCHES=0", result.stdout)


class ScriptTests(unittest.TestCase):
    def test_the_psscriptanalyzer_check_script_is_parseable_powershell(self) -> None:
        script = (
            Path(source_checks.__file__).resolve().parent / "checks" / "psscriptanalyzer.ps1"
        )
        self.assertTrue(script.is_file(), script)
        parser = """
$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseInput(
    [Console]::In.ReadToEnd(), [ref]$tokens, [ref]$errors) | Out-Null
if ($errors.Count -ne 0) { $errors | Out-String | Write-Error; exit 1 }
"""
        result = subprocess.run(
            [
                shutil.which("pwsh"),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                parser,
            ],
            input=script.read_text(encoding="utf-8"),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import diagnostics  # noqa: E402
import native  # noqa: E402


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


@dataclass(frozen=True)
class RecordedCall:
    argv: tuple[str, ...]
    cwd: Path | None
    env: dict[str, str] | None

    @property
    def name(self) -> str:
        return Path(self.argv[0]).name.casefold()


class FakeRunner:
    """Record every invocation and answer it with a scripted result."""

    def __init__(self, handler=None) -> None:
        self.calls: list[RecordedCall] = []
        self.handler = handler

    def __call__(self, argv, cwd=None, env=None) -> diagnostics.ToolResult:
        call = RecordedCall(
            tuple(str(item) for item in argv),
            cwd,
            None if env is None else dict(env),
        )
        self.calls.append(call)
        return (
            diagnostics.ToolResult(0, "")
            if self.handler is None
            else self.handler(call)
        )

    def commands(self, name: str) -> list[RecordedCall]:
        return [call for call in self.calls if call.name == name]


def _report(call: RecordedCall) -> None:
    for argument in call.argv:
        if argument.startswith("JUnit::out="):
            path = Path(argument.split("=", 1)[1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<testsuites/>", encoding="utf-8")


# A distinct payload for the full-detail LCOV export so a test can prove the
# two ``llvm-cov export`` shapes are not accidentally the same command.
LCOV_EXPORT = "SF:src/renpy/renpy.cpp\nDA:1,1\nend_of_record\n"


def _handler(*, profraws: bool = True, tests_xml: bool = True,
             replay_exit: int = 0, timed_exit: int = 0, report: str = "{}",
             generated: bytes | None = None):
    def handle(call: RecordedCall) -> diagnostics.ToolResult:
        if call.name == "tests.exe":
            if call.env and "LLVM_PROFILE_FILE" in call.env:
                if profraws:
                    pattern = Path(call.env["LLVM_PROFILE_FILE"])
                    pattern.parent.mkdir(parents=True, exist_ok=True)
                    (pattern.parent / "coverage-1-1.profraw").write_bytes(b"raw")
            if tests_xml:
                _report(call)
            return diagnostics.ToolResult(0, "suite output")
        if call.name == "llvm-cov.exe":
            if "--format=lcov" in call.argv:
                return diagnostics.ToolResult(0, LCOV_EXPORT)
            return diagnostics.ToolResult(0, report)
        if call.name.startswith("fuzz-"):
            timed = any(item.startswith("-max_total_time=") for item in call.argv)
            if timed:
                if generated is not None:
                    (Path(call.argv[1]) / "generated").write_bytes(generated)
                return diagnostics.ToolResult(timed_exit, "timed")
            return diagnostics.ToolResult(replay_exit, "replay")
        return diagnostics.ToolResult(0, "")

    return handle


def _tools(root: Path) -> diagnostics.DiagnosticsTools:
    return diagnostics.DiagnosticsTools(
        msbuild=_touch(root / "tools" / "MSBuild.exe"),
        vcpkg=_touch(root / "tools" / "vcpkg" / "vcpkg.exe"),
        llvm_install=root / "llvm",
        llvm_cov=_touch(root / "tools" / "llvm-cov.exe"),
        llvm_profdata=_touch(root / "tools" / "llvm-profdata.exe"),
        llvm_runtimes={"x86": root / "runtime-x86", "x64": root / "runtime-x64"},
        asan_runtimes={
            "x86": _touch(root / "runtime" / "clang_rt.asan_dynamic-i386.dll"),
            "x64": _touch(root / "runtime" / "clang_rt.asan_dynamic-x86_64.dll"),
        },
        umdh=_touch(root / "tools" / "umdh.exe"),
    )


def _seed(repository: Path, target: str, **seeds: bytes) -> Path:
    directory = repository / "src" / "fuzz" / "corpus" / target
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in seeds.items():
        (directory / name).write_bytes(content)
    return directory


def _fuzz_calls(runner: FakeRunner, exe: str, *, timed: bool) -> list[RecordedCall]:
    return [
        call for call in runner.calls
        if call.name == exe
        and any(item.startswith("-max_total_time=") for item in call.argv) is timed
    ]


class WorkspaceTestCase(unittest.TestCase):
    def workspace(self) -> tuple[Path, Path, Path]:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        return root, root / "repo", root / "out"


class RunnerTests(unittest.TestCase):
    def test_the_real_runner_merges_the_environment_preserves_cwd_and_exit_code(self) -> None:
        result = diagnostics.run_tool(
            [sys.executable, "-c", "import os; print(os.environ['OBSERVER_PROBE'])"],
            env={"OBSERVER_PROBE": "1"},
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "1")

        located = diagnostics.run_tool(
            [sys.executable, "-c", "import os; print(os.getcwd())"],
            cwd=Path(tempfile.gettempdir()),
        )
        self.assertEqual(Path(located.stdout.strip()), Path(tempfile.gettempdir()).resolve())

        failed = diagnostics.run_tool([sys.executable, "-c", "raise SystemExit(9)"])
        self.assertEqual(failed.returncode, 9)

    def test_the_real_runner_always_passes_an_explicit_inherited_environment(self) -> None:
        # A bare ``env=None`` lets the child inherit the raw Windows block with both PATH and
        # Path, which makes the MSBuild ClangCl task throw MSB6001; the merged os.environ
        # mapping normalises the duplicate through its case-insensitive lookup.
        completed = subprocess.CompletedProcess([], 0, stdout="")
        with mock.patch.object(diagnostics.subprocess, "run", return_value=completed) as run:
            diagnostics.run_tool(["tool.exe"])
            diagnostics.run_tool(["tool.exe"], env={"OBSERVER_PROBE": "1"})

        inherited = run.call_args_list[0].kwargs["env"]
        self.assertIsNotNone(inherited)
        self.assertEqual(inherited, dict(os.environ))

        override = run.call_args_list[1].kwargs["env"]
        self.assertEqual(override["OBSERVER_PROBE"], "1")
        self.assertEqual(override["PATH"], os.environ["PATH"])


class CheckedRunnerTests(unittest.TestCase):
    def test_the_adapter_passes_success_through_and_raises_on_failure(self) -> None:
        runner = FakeRunner()
        execute = diagnostics._checked_runner(runner)

        result = execute(("tool", "arg"), Path("C:/cwd"), {"A": "1"})
        self.assertEqual(result, diagnostics.ToolResult(0, ""))
        self.assertEqual(runner.calls[0].argv, ("tool", "arg"))
        self.assertEqual(runner.calls[0].cwd, Path("C:/cwd"))
        self.assertEqual(runner.calls[0].env, {"A": "1"})

        failing = diagnostics._checked_runner(
            FakeRunner(lambda call: diagnostics.ToolResult(17, "boom"))
        )
        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "exit code 17"):
            failing(("vcpkg", "install"), None, None)


class JobCountTests(unittest.TestCase):
    def test_jobs_default_to_the_cpu_count_and_reject_nonpositive_or_boolean_values(self) -> None:
        self.assertEqual(diagnostics._job_count(None), os.cpu_count() or 1)
        self.assertEqual(diagnostics._job_count(4), 4)
        for value in (0, -1, True):
            with self.assertRaisesRegex(ValueError, "jobs"):
                diagnostics._job_count(value)


class SelectionTests(unittest.TestCase):
    def test_selections_reject_empty_unsupported_and_duplicate_values(self) -> None:
        diagnostics._require_selection(("x86", "x64"), diagnostics.COVERAGE_ARCHITECTURES, "coverage")
        with self.assertRaisesRegex(ValueError, "at least one"):
            diagnostics._require_selection((), diagnostics.COVERAGE_ARCHITECTURES, "coverage")
        with self.assertRaisesRegex(ValueError, "coverage does not support: arm64"):
            diagnostics._require_selection(("arm64",), diagnostics.COVERAGE_ARCHITECTURES, "coverage")
        with self.assertRaisesRegex(ValueError, "duplicate selection"):
            diagnostics._require_selection(("x64", "x64"), diagnostics.COVERAGE_ARCHITECTURES, "coverage")

    def test_sanitizers_and_hosts_are_validated(self) -> None:
        diagnostics._require_sanitizer("asan")
        with self.assertRaisesRegex(ValueError, "unsupported sanitizer"):
            diagnostics._require_sanitizer("msan")
        diagnostics._require_runnable(("x86",), "x64")
        diagnostics._require_runnable(("x86",), None)
        with self.assertRaisesRegex(ValueError, "cannot run"):
            diagnostics._require_runnable(("x64",), "x86")
        with self.assertRaisesRegex(ValueError, "unsupported host"):
            diagnostics._require_runnable(("x64",), "sparc")


class RestoreGuardTests(WorkspaceTestCase):
    """Finding 1: every native.restore call must fail closed on a nonzero vcpkg exit."""

    def _failing(self) -> FakeRunner:
        return FakeRunner(
            lambda call: diagnostics.ToolResult(17, "vcpkg boom")
            if call.name == "vcpkg.exe" else diagnostics.ToolResult(0, "")
        )

    def test_a_failed_coverage_restore_stops_before_any_build(self) -> None:
        root, repository, output = self.workspace()
        runner = self._failing()

        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "exit code 17"):
            diagnostics.test_coverage(
                repository, ("x86", "x64"), output, tools=_tools(root), runner=runner, host="x64",
            )
        self.assertEqual(runner.commands("msbuild.exe"), [])

    def test_a_failed_sanitizer_restore_stops_before_any_build(self) -> None:
        root, repository, output = self.workspace()
        runner = self._failing()

        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "exit code 17"):
            diagnostics.test_sanitizer(
                repository, "ubsan", ("x64",), output, tools=_tools(root), runner=runner, host="x64",
            )
        self.assertEqual(runner.commands("msbuild.exe"), [])

    def test_a_failed_leak_restore_stops_before_any_build(self) -> None:
        root, repository, output = self.workspace()
        runner = self._failing()

        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "exit code 17"):
            diagnostics.test_leaks(
                repository, output, tools=_tools(root), runner=runner, host="x64",
                auditor=lambda *args, **kwargs: None,
            )
        self.assertEqual(runner.commands("msbuild.exe"), [])


class CoverageTests(WorkspaceTestCase):
    def test_coverage_builds_reports_and_gates_the_real_shared_objects(self) -> None:
        root, repository, output = self.workspace()
        tools = _tools(root)
        runner = FakeRunner(_handler())

        reports = diagnostics.test_coverage(
            repository, ("x86", "x64"), output, tools=tools, runner=runner, host="x64",
        )

        self.assertEqual(
            reports,
            (output / "coverage" / "x86" / "coverage.json",
             output / "coverage" / "x86" / "coverage.lcov",
             output / "coverage" / "x64" / "coverage.json",
             output / "coverage" / "x64" / "coverage.lcov"),
        )
        self.assertEqual(
            [call.argv for call in runner.commands("vcpkg.exe")],
            [native.restore_command(repository, architecture, tools)
             for architecture in ("x86", "x64")],
        )
        self.assertEqual(
            [call.argv for call in runner.commands("msbuild.exe")],
            [
                diagnostics._observer_command(
                    repository, architecture, "Coverage", diagnostics._job_count(None), tools,
                    runtime=tools.llvm_runtimes[architecture],
                )
                for architecture in ("x86", "x64")
            ],
        )
        for architecture in ("x86", "x64"):
            suite = next(
                call for call in runner.commands("tests.exe")
                if call.cwd == native.bin_directory(repository, architecture, "Coverage")
            )
            self.assertEqual(
                suite.argv,
                native.test_command(
                    repository, architecture, "Coverage",
                    output / "coverage" / architecture / "unit" / "tests.xml", None,
                ),
            )
            self.assertEqual(
                suite.env,
                {"LLVM_PROFILE_FILE": str(
                    native.artifacts_root(repository) / "coverage" / architecture / "unit"
                    / "coverage-%m-%p.profraw")},
            )
            self.assertTrue(
                (output / "coverage" / architecture / "coverage.json").is_file()
            )
            self.assertTrue(
                (output / "coverage" / architecture / "coverage.lcov").is_file()
            )

        merge = runner.commands("llvm-profdata.exe")
        self.assertEqual(len(merge), 2)
        self.assertEqual(merge[0].argv[1], "merge")
        self.assertEqual(
            merge[0].argv[-2:],
            ("-o", str(output / "coverage" / "x86" / "coverage.profdata")),
        )
        self.assertEqual(
            merge[0].argv[3],
            str(native.artifacts_root(repository) / "coverage" / "x86" / "unit"
                / "coverage-1-1.profraw"),
        )

        export = runner.commands("llvm-cov.exe")
        self.assertEqual(len(export), 4)
        argv = export[0].argv
        self.assertEqual(argv[1], "export")
        self.assertEqual(argv[2], str(native.test_executable(repository, "x86", "Coverage")))
        self.assertEqual(argv[3], "--instr-profile")
        self.assertEqual(argv[4], str(output / "coverage" / "x86" / "coverage.profdata"))
        self.assertEqual(argv[5], "--ignore-filename-regex")
        self.assertEqual(argv[6], diagnostics.COVERAGE_IGNORED_SOURCES)
        objects = [argv[index + 1] for index, item in enumerate(argv) if item == "--object"]
        self.assertEqual(
            objects,
            [str(native.bin_directory(repository, "x86", "Coverage") / native.BINARIES[name])
             for name in diagnostics.PROJECTS],
        )
        self.assertEqual(argv[-1], "--summary-only")

        detail = export[1].argv
        self.assertEqual(detail[1:3], argv[1:3])
        self.assertEqual(detail[4:7], argv[4:7])
        self.assertEqual(
            [detail[index + 1] for index, item in enumerate(detail) if item == "--object"],
            objects,
        )
        self.assertEqual(detail[-1], "--format=lcov")
        self.assertNotIn("--summary-only", detail)

        gate = runner.commands("python.exe")
        self.assertEqual(len(gate), 2)
        self.assertEqual(
            gate[0].argv,
            (str(diagnostics._PYTHON), "-m", "core.cpp_coverage", "gate",
             str(output / "coverage" / "x86" / "coverage.json")),
        )
        self.assertEqual(gate[0].cwd, diagnostics._BUILD_ROOT)
        self.assertEqual(
            gate[0].env,
            {"OBSERVER_OUT_DIR": str(output / "coverage" / "x86"),
             "OBSERVER_BUILD_DIR": str(output / "coverage" / "x86")},
        )

    def test_coverage_emits_the_full_lcov_detail_alongside_the_summary(self) -> None:
        root, repository, output = self.workspace()
        tools = _tools(root)
        runner = FakeRunner(_handler())

        reports = diagnostics.test_coverage(
            repository, ("x86",), output, tools=tools, runner=runner, host="x64",
        )

        json_report = output / "coverage" / "x86" / "coverage.json"
        lcov_report = output / "coverage" / "x86" / "coverage.lcov"
        self.assertEqual(reports, (json_report, lcov_report))
        export = runner.commands("llvm-cov.exe")
        self.assertEqual(len(export), 2)
        summary, detail = (call.argv for call in export)
        # Both exports drive the same executable, profile and shared objects...
        self.assertEqual(summary[1:7], detail[1:7])

        def objects(argv):
            return [argv[index + 1] for index, item in enumerate(argv) if item == "--object"]

        self.assertEqual(objects(summary), objects(detail))
        # ...but only the summary is trimmed, while the detail keeps every line.
        self.assertEqual(summary[-1], "--summary-only")
        self.assertNotIn("--format=lcov", summary)
        self.assertEqual(detail[-1], "--format=lcov")
        self.assertNotIn("--summary-only", detail)
        self.assertEqual(json_report.read_text(encoding="utf-8"), "{}")
        self.assertEqual(lcov_report.read_text(encoding="utf-8"), LCOV_EXPORT)

    def test_a_failing_lcov_export_fails_closed_without_the_gate(self) -> None:
        root, repository, output = self.workspace()

        def handler(call: RecordedCall) -> diagnostics.ToolResult:
            if call.name == "llvm-cov.exe" and "--format=lcov" in call.argv:
                return diagnostics.ToolResult(9, "lcov boom")
            return _handler()(call)

        runner = FakeRunner(handler)
        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "exit code 9"):
            diagnostics.test_coverage(
                repository, ("x64",), output, tools=_tools(root), runner=runner, host="x64",
            )

        # The failed export is never written and the gate is never reached.
        self.assertFalse((output / "coverage" / "x64" / "coverage.lcov").is_file())
        self.assertEqual(runner.commands("python.exe"), [])

    def test_coverage_with_corpus_runs_the_ordinary_and_compatibility_suites(self) -> None:
        root, repository, output = self.workspace()
        tools = _tools(root)
        runner = FakeRunner(_handler())
        corpus = root / "corpus"
        corpus.mkdir()

        reports = diagnostics.test_coverage(
            repository, ("x64",), output, tools=tools, runner=runner, host="x64",
            corpus=corpus,
        )

        self.assertEqual(
            reports,
            (output / "coverage" / "x64" / "coverage.json",
             output / "coverage" / "x64" / "coverage.lcov"),
        )
        suites = runner.commands("tests.exe")
        self.assertEqual(len(suites), 2)
        unit = next(
            call for call in suites
            if call.argv == native.test_command(
                repository, "x64", "Coverage",
                output / "coverage" / "x64" / "unit" / "tests.xml", None,
            )
        )
        self.assertNotIn("OBSERVER_TEST_CORPUS", unit.env)
        corpus_call = next(
            call for call in suites
            if call.argv == native.test_command(
                repository, "x64", "Coverage",
                output / "coverage" / "x64" / "corpus" / "tests.xml", corpus,
            )
        )
        self.assertIn("[compatibility]", corpus_call.argv)
        self.assertEqual(corpus_call.env["OBSERVER_TEST_CORPUS"], str(corpus))
        self.assertEqual(
            corpus_call.env["LLVM_PROFILE_FILE"],
            str(native.artifacts_root(repository) / "coverage" / "x64" / "corpus"
                / "coverage-%m-%p.profraw"),
        )
        unit_raw = (native.artifacts_root(repository) / "coverage" / "x64" / "unit"
                    / "coverage-1-1.profraw")
        corpus_raw = (native.artifacts_root(repository) / "coverage" / "x64" / "corpus"
                      / "coverage-1-1.profraw")
        self.assertTrue(unit_raw.is_file() and corpus_raw.is_file())
        merge = runner.commands("llvm-profdata.exe")[0]
        self.assertEqual(merge.argv[3:5], (str(unit_raw), str(corpus_raw)))
        self.assertTrue(
            (output / "coverage" / "x64" / "unit" / "tests.xml").is_file()
        )
        self.assertTrue(
            (output / "coverage" / "x64" / "corpus" / "tests.xml").is_file()
        )

    def test_coverage_rejects_a_missing_or_non_directory_corpus(self) -> None:
        root, repository, output = self.workspace()
        runner = FakeRunner()

        with self.assertRaises(FileNotFoundError):
            diagnostics.test_coverage(
                repository, ("x64",), output, tools=_tools(root), runner=runner,
                corpus=repository / "missing",
            )
        with self.assertRaises(NotADirectoryError):
            diagnostics.test_coverage(
                repository, ("x64",), output, tools=_tools(root), runner=runner,
                corpus=_touch(root / "corpus-file"),
            )
        self.assertEqual(runner.calls, [])

    def test_coverage_clears_stale_raw_profiles_before_running(self) -> None:
        root, repository, output = self.workspace()
        stale = native.artifacts_root(repository) / "coverage" / "x64" / "unit" / "stale.profraw"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"old")

        diagnostics.test_coverage(
            repository, ("x64",), output, tools=_tools(root),
            runner=FakeRunner(_handler()), host="x64",
        )

        self.assertFalse(stale.exists())
        self.assertTrue((native.artifacts_root(repository) / "coverage" / "x64" / "unit"
                         / "coverage-1-1.profraw").is_file())

    def test_coverage_requires_new_raw_profiles(self) -> None:
        root, repository, output = self.workspace()

        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "no LLVM raw profiles"):
            diagnostics.test_coverage(
                repository, ("x64",), output, tools=_tools(root),
                runner=FakeRunner(_handler(profraws=False)), host="x64",
            )

    def test_coverage_requires_raw_profiles_from_every_suite(self) -> None:
        root, repository, output = self.workspace()
        corpus = root / "corpus"
        corpus.mkdir()

        def handler(call: RecordedCall) -> diagnostics.ToolResult:
            # Only the compatibility suite yields a profile; the ordinary one is empty.
            if call.name == "tests.exe":
                if (
                    call.env
                    and "LLVM_PROFILE_FILE" in call.env
                    and "[compatibility]" in call.argv
                ):
                    pattern = Path(call.env["LLVM_PROFILE_FILE"])
                    pattern.parent.mkdir(parents=True, exist_ok=True)
                    (pattern.parent / "coverage-1-1.profraw").write_bytes(b"raw")
                _report(call)
            return diagnostics.ToolResult(0, "output")

        runner = FakeRunner(handler)
        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "no LLVM raw profiles"):
            diagnostics.test_coverage(
                repository, ("x64",), output, tools=_tools(root), runner=runner,
                host="x64", corpus=corpus,
            )

        # The empty ordinary suite stops the run before the profiles are merged.
        self.assertEqual(runner.commands("llvm-profdata.exe"), [])

    def test_coverage_rejects_an_architecture_without_a_profile_runtime(self) -> None:
        root, repository, output = self.workspace()
        runner = FakeRunner()

        with self.assertRaisesRegex(ValueError, "arm64"):
            diagnostics.test_coverage(
                repository, ("arm64",), output, tools=_tools(root), runner=runner,
            )
        self.assertEqual(runner.calls, [])

    def test_coverage_refuses_a_host_that_cannot_run_the_architecture(self) -> None:
        root, repository, output = self.workspace()
        runner = FakeRunner()

        with self.assertRaisesRegex(ValueError, "cannot run"):
            diagnostics.test_coverage(
                repository, ("x64",), output, tools=_tools(root), runner=runner, host="x86",
            )
        self.assertEqual(runner.calls, [])

    def test_a_failing_coverage_build_stops_before_the_suite_runs(self) -> None:
        root, repository, output = self.workspace()
        runner = FakeRunner(
            lambda call: diagnostics.ToolResult(3, "boom")
            if call.name == "msbuild.exe" else diagnostics.ToolResult(0, "")
        )

        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "exit code 3"):
            diagnostics.test_coverage(
                repository, ("x64",), output, tools=_tools(root), runner=runner, host="x64",
            )
        self.assertEqual(runner.commands("tests.exe"), [])


class SanitizerTests(WorkspaceTestCase):
    def test_asan_builds_copies_the_runtime_and_gates_the_suite_log(self) -> None:
        root, repository, output = self.workspace()
        tools = _tools(root)
        runner = FakeRunner(_handler())

        logs = diagnostics.test_sanitizer(
            repository, "asan", ("x86", "x64"), output,
            tools=tools, runner=runner, host="x64",
        )

        self.assertEqual(
            logs,
            (output / "sanitizer" / "asan" / "x86" / "tests.log",
             output / "sanitizer" / "asan" / "x64" / "tests.log"),
        )
        restores = runner.commands("vcpkg.exe")
        self.assertEqual(len(restores), 2)
        for call in restores:
            self.assertIn("-asan", call.argv[2])
        self.assertEqual(
            {call.argv[6] for call in restores},
            {"observer-x86-windows-static-asan", "observer-x64-windows-static-asan"},
        )
        for call in runner.commands("msbuild.exe"):
            self.assertFalse(any(item.startswith("/p:LLVM") for item in call.argv))
        for architecture in ("x86", "x64"):
            runtime = (native.bin_directory(repository, architecture, "ASan")
                       / diagnostics.ASAN_RUNTIMES[architecture])
            self.assertTrue(runtime.is_file())
        suite = runner.commands("tests.exe")[0]
        self.assertEqual(suite.env, {"ASAN_OPTIONS": diagnostics.SANITIZER_ENVIRONMENT["asan"][1]})
        gate = runner.commands("python.exe")[0]
        self.assertEqual(
            gate.argv[1:],
            ("-m", "core.sanitizer", "gate", "asan",
             str(output / "sanitizer" / "asan" / "x86" / "tests.log")),
        )

    def test_ubsan_links_the_runtime_directory_and_gates_the_suite_log(self) -> None:
        root, repository, output = self.workspace()
        tools = _tools(root)
        runner = FakeRunner(_handler())

        logs = diagnostics.test_sanitizer(
            repository, "ubsan", ("x64",), output,
            tools=tools, runner=runner, host="x64",
        )

        self.assertEqual(logs, (output / "sanitizer" / "ubsan" / "x64" / "tests.log",))
        restore = runner.commands("vcpkg.exe")[0]
        self.assertFalse(restore.argv[2].endswith("-asan"))
        build = runner.commands("msbuild.exe")[0]
        self.assertIn(f"/p:LLVMInstallDir={tools.llvm_install}", build.argv)
        self.assertIn(f"/p:LLVMRuntimeDir={tools.llvm_runtimes['x64']}", build.argv)
        self.assertEqual(list(native.bin_directory(repository, "x64", "UBSan").glob("*.dll")), [])
        self.assertEqual(
            runner.commands("tests.exe")[0].env,
            {"UBSAN_OPTIONS": diagnostics.SANITIZER_ENVIRONMENT["ubsan"][1]},
        )

    def test_a_sanitizer_suite_without_results_is_a_failure(self) -> None:
        root, repository, output = self.workspace()

        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "tests.xml"):
            diagnostics.test_sanitizer(
                repository, "asan", ("x64",), output, tools=_tools(root),
                runner=FakeRunner(_handler(tests_xml=False)), host="x64",
            )

    def test_sanitizers_reject_unknown_names_and_unsupported_architectures(self) -> None:
        root, repository, output = self.workspace()
        runner = FakeRunner()

        with self.assertRaisesRegex(ValueError, "unsupported sanitizer"):
            diagnostics.test_sanitizer(
                repository, "msan", ("x64",), output, tools=_tools(root), runner=runner,
            )
        with self.assertRaisesRegex(ValueError, "ubsan does not support: x86"):
            diagnostics.test_sanitizer(
                repository, "ubsan", ("x86",), output, tools=_tools(root), runner=runner,
            )
        self.assertEqual(runner.calls, [])


class FuzzTests(WorkspaceTestCase):
    def test_fuzz_runs_the_timed_x64_budget_and_the_x86_replay_gate(self) -> None:
        root, repository, output = self.workspace()
        for target in diagnostics.FUZZ_TARGETS:
            _seed(repository, target, **{"valid.hex": b"0011ff", "raw.bin": b"\x00\x01"})
        tools = _tools(root)
        runner = FakeRunner(_handler())

        outputs = diagnostics.fuzz(
            repository, ("x86", "x64"), diagnostics.FUZZ_TARGETS, 30, output,
            tools=tools, runner=runner, host="x64",
        )

        self.assertEqual(
            outputs,
            tuple(output / "fuzz" / architecture / target
                  for architecture in ("x86", "x64") for target in diagnostics.FUZZ_TARGETS),
        )
        builds = runner.commands("msbuild.exe")
        self.assertEqual(len(builds), 8)
        for call in builds:
            expected = tools.llvm_runtimes["x86" if "/p:Platform=Win32" in call.argv else "x64"]
            self.assertIn(f"/p:LLVMRuntimeDir={expected}", call.argv)

        timed = _fuzz_calls(runner, "fuzz-pickle.exe", timed=True)
        replays = _fuzz_calls(runner, "fuzz-pickle.exe", timed=False)
        self.assertEqual(len(timed), 1)
        self.assertEqual(len(replays), 2)
        self.assertTrue(all("-max_len=262144" in call.argv for call in timed + replays))

        seed_dir = native.artifacts_root(repository) / "fuzz" / "x64" / "pickle" / "seeds"
        self.assertEqual((seed_dir / "valid").read_bytes(), bytes.fromhex("0011ff"))
        self.assertEqual((seed_dir / "raw.bin").read_bytes(), b"\x00\x01")
        self.assertEqual(timed[0].argv[1], str(
            native.artifacts_root(repository) / "fuzz" / "x64" / "pickle" / "run"))
        x64_replay = next(call for call in replays if "x64" in call.argv[0])
        self.assertIn(str(seed_dir / "valid"), x64_replay.argv)

        published = output / "fuzz" / "x64" / "pickle"
        self.assertEqual((published / "corpus" / "valid").read_bytes(), bytes.fromhex("0011ff"))
        self.assertEqual((published / "status.txt").read_text(encoding="utf-8"), "0\n")
        self.assertTrue((published / "artifacts").is_dir())

        gates = runner.commands("python.exe")
        self.assertEqual(len(gates), len(diagnostics.FUZZ_TARGETS))
        for gate in gates:
            self.assertEqual(gate.argv[1:5], ("-m", "core.sanitizer", "gate", "asan"))

    def test_a_same_named_prior_seed_never_changes_the_hermetic_replay(self) -> None:
        root, repository, output = self.workspace()
        _seed(repository, "pickle", **{"valid": b"seed-bytes"})
        prior = root / "prior"
        (prior / "pickle").mkdir(parents=True)
        (prior / "pickle" / "valid").write_bytes(b"prior-bytes")
        tools = _tools(root)
        runner = FakeRunner(_handler())

        diagnostics.fuzz(
            repository, ("x86", "x64"), ("pickle",), 5, output,
            tools=tools, runner=runner, host="x64", prior_corpus=prior,
        )

        work = native.artifacts_root(repository) / "fuzz" / "x64" / "pickle"
        self.assertEqual((work / "seeds" / "valid").read_bytes(), b"seed-bytes")
        self.assertEqual((work / "prior" / "valid").read_bytes(), b"prior-bytes")
        self.assertEqual((work / "run" / "valid").read_bytes(), b"prior-bytes")
        x64_replay = next(
            call for call in _fuzz_calls(runner, "fuzz-pickle.exe", timed=False)
            if "x64" in call.argv[0]
        )
        self.assertIn(str(work / "seeds" / "valid"), x64_replay.argv)
        self.assertNotIn(str(work / "prior" / "valid"), x64_replay.argv)
        x86_work = native.artifacts_root(repository) / "fuzz" / "x86" / "pickle"
        self.assertEqual((x86_work / "seeds" / "valid").read_bytes(), b"seed-bytes")
        self.assertEqual((x86_work / "prior" / "valid").read_bytes(), b"prior-bytes")
        x86_replay = next(
            call for call in _fuzz_calls(runner, "fuzz-pickle.exe", timed=False)
            if "x86" in call.argv[0]
        )
        self.assertIn(str(x86_work / "seeds" / "valid"), x86_replay.argv)
        self.assertIn(str(x86_work / "prior" / "valid"), x86_replay.argv)
        timed = _fuzz_calls(runner, "fuzz-pickle.exe", timed=True)[0]
        self.assertEqual(timed.argv[1], str(work / "run"))

    def test_fuzz_publishes_the_x64_corpus_and_reserves_it_for_reuse(self) -> None:
        root, repository, output = self.workspace()
        _seed(repository, "pickle", **{"valid.hex": b"00ff"})
        tools, runner = _tools(root), FakeRunner(_handler(generated=b"new-input"))

        diagnostics.fuzz(
            repository, ("x64",), ("pickle",), 5, output,
            tools=tools, runner=runner, host="x64",
        )
        published = output / "fuzz" / "x64" / "pickle" / "corpus"
        self.assertEqual((published / "generated").read_bytes(), b"new-input")

        second = root / "second"
        runner2 = FakeRunner(_handler())
        diagnostics.fuzz(
            repository, ("x64",), ("pickle",), 5, second,
            tools=tools, runner=runner2, host="x64", prior_corpus=output / "fuzz" / "x64",
        )
        self.assertTrue((published / "generated").is_file())
        work = native.artifacts_root(repository) / "fuzz" / "x64" / "pickle"
        self.assertEqual((work / "run" / "generated").read_bytes(), b"new-input")
        self.assertTrue(
            (second / "fuzz" / "x64" / "pickle" / "corpus" / "generated").is_file()
        )

    def test_absent_prior_corpus_entries_are_ignored(self) -> None:
        root, repository, output = self.workspace()
        _seed(repository, "pickle", **{"valid.hex": b"00ff"})
        prior = root / "prior"
        (prior / "renpy").mkdir(parents=True)
        (prior / "renpy" / "other").write_bytes(b"other")

        diagnostics.fuzz(
            repository, ("x64",), ("pickle",), 5, output,
            tools=_tools(root), runner=FakeRunner(_handler()), host="x64", prior_corpus=prior,
        )

        self.assertEqual(
            sorted(path.name for path in
                   (native.artifacts_root(repository) / "fuzz" / "x64" / "pickle" / "run").iterdir()),
            ["valid"],
        )
        self.assertFalse(
            (native.artifacts_root(repository) / "fuzz" / "x64" / "pickle" / "prior").exists()
        )

    def test_a_missing_prior_corpus_directory_is_rejected(self) -> None:
        root, repository, output = self.workspace()

        with self.assertRaises(NotADirectoryError):
            diagnostics.fuzz(
                repository, ("x64",), ("pickle",), 5, output, tools=_tools(root),
                runner=FakeRunner(), host="x64", prior_corpus=repository / "missing",
            )

    def test_fuzz_seed_loading_handles_missing_empty_and_nested_entries(self) -> None:
        root, repository, output = self.workspace()
        tools, runner = _tools(root), FakeRunner(_handler())

        with self.assertRaisesRegex(FileNotFoundError, "no checked-in fuzzer seeds"):
            diagnostics.fuzz(repository, ("x64",), ("pickle",), 5, output,
                             tools=tools, runner=runner, host="x64")

        (repository / "src" / "fuzz" / "corpus" / "pickle").mkdir(parents=True)
        with self.assertRaisesRegex(FileNotFoundError, "no checked-in fuzzer seeds"):
            diagnostics.fuzz(repository, ("x64",), ("pickle",), 5, output,
                             tools=tools, runner=runner, host="x64")

        directory = _seed(repository, "pickle", **{"valid.hex": b"00ff"})
        (directory / "nested").mkdir()
        diagnostics.fuzz(repository, ("x64",), ("pickle",), 5, output,
                         tools=tools, runner=runner, host="x64")
        self.assertEqual(
            [path.name for path in
             (native.artifacts_root(repository) / "fuzz" / "x64" / "pickle" / "seeds").iterdir()],
            ["valid"],
        )

    def test_a_timed_fuzzer_crash_is_reported_with_its_evidence(self) -> None:
        root, repository, output = self.workspace()
        _seed(repository, "pickle", **{"valid.hex": b"00ff"})

        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "exit code 7"):
            diagnostics.fuzz(
                repository, ("x64",), ("pickle",), 5, output, tools=_tools(root),
                runner=FakeRunner(_handler(timed_exit=7)), host="x64",
            )

        directory = output / "fuzz" / "x64" / "pickle"
        self.assertEqual((directory / "status.txt").read_text(encoding="utf-8"), "7\n")
        self.assertTrue((directory / "run.log").is_file())
        self.assertTrue((directory / "artifacts").is_dir())
        self.assertFalse((directory / "corpus").exists())

    def test_a_replay_crash_is_reported_with_its_log(self) -> None:
        root, repository, output = self.workspace()
        _seed(repository, "pickle", **{"valid.hex": b"00ff"})

        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "exit code 4"):
            diagnostics.fuzz(
                repository, ("x86",), ("pickle",), 5, output, tools=_tools(root),
                runner=FakeRunner(_handler(replay_exit=4)), host="x64",
            )

        self.assertTrue((output / "fuzz" / "x86" / "pickle" / "replay.log").is_file())

    def test_a_reused_x64_output_records_the_replay_failure_over_the_old_success(self) -> None:
        root, repository, output = self.workspace()
        _seed(repository, "pickle", **{"valid.hex": b"00ff"})
        tools = _tools(root)

        diagnostics.fuzz(
            repository, ("x64",), ("pickle",), 5, output,
            tools=tools, runner=FakeRunner(_handler(generated=b"new-input")), host="x64",
        )
        directory = output / "fuzz" / "x64" / "pickle"
        self.assertEqual((directory / "status.txt").read_text(encoding="utf-8"), "0\n")
        self.assertTrue((directory / "run.log").is_file())
        self.assertTrue((directory / "corpus" / "generated").is_file())

        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "exit code 4"):
            diagnostics.fuzz(
                repository, ("x64",), ("pickle",), 5, output,
                tools=tools, runner=FakeRunner(_handler(replay_exit=4)), host="x64",
            )

        # The stale success is replaced by the current failure evidence.
        self.assertEqual((directory / "status.txt").read_text(encoding="utf-8"), "4\n")
        self.assertFalse((directory / "run.log").exists())
        self.assertTrue((directory / "replay.log").is_file())
        # The accumulated corpus from the earlier success is preserved for reuse.
        self.assertTrue((directory / "corpus" / "generated").is_file())

    def test_fuzz_rejects_unknown_targets_and_nonpositive_budgets(self) -> None:
        root, repository, output = self.workspace()
        runner = FakeRunner()

        with self.assertRaisesRegex(ValueError, "fuzz does not support: bogus"):
            diagnostics.fuzz(repository, ("x64",), ("bogus",), 5, output,
                             tools=_tools(root), runner=runner)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            diagnostics.fuzz(repository, ("x64",), ("pickle",), 0, output,
                             tools=_tools(root), runner=runner)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            diagnostics.fuzz(repository, ("x64",), ("pickle",), True, output,
                             tools=_tools(root), runner=runner)
        self.assertEqual(runner.calls, [])


class FreshMeasureTests(WorkspaceTestCase):
    """Finding 4: stale per-operation measure output must be refreshed safely."""

    def test_a_missing_owned_directory_is_left_alone(self) -> None:
        root, _repository, _output = self.workspace()

        diagnostics._remove_owned(root / "missing", root)

        self.assertFalse((root / "missing").exists())

    def test_an_existing_owned_directory_is_removed(self) -> None:
        root, _repository, _output = self.workspace()
        victim = root / "snapshots"
        victim.mkdir(parents=True)
        (victim / "old.txt").write_text("old", encoding="utf-8")

        diagnostics._remove_owned(victim, root)

        self.assertFalse(victim.exists())

    def test_an_escape_outside_the_output_root_is_refused(self) -> None:
        root, _repository, _output = self.workspace()
        inside = root / "root"
        outside = root / "outside"
        inside.mkdir()
        outside.mkdir()

        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "outside the operation output"):
            diagnostics._remove_owned(outside, inside)
        self.assertTrue(outside.is_dir())

    def test_a_reparse_point_is_refused(self) -> None:
        root, _repository, _output = self.workspace()
        inside = root / "root"
        target = inside / "target"
        target.mkdir(parents=True)
        link = inside / "link"
        os.symlink(target, link, target_is_directory=True)

        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "reparse point"):
            diagnostics._remove_owned(link, inside)
        self.assertTrue(link.is_symlink())

    def test_fresh_measure_refuses_a_linked_measure_directory_before_creating_it(self) -> None:
        root, _repository, output = self.workspace()
        outside = root / "external"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"keep")
        link = output / "leak" / "mode" / "scenario" / "measure"
        link.parent.mkdir(parents=True)
        os.symlink(outside, link, target_is_directory=True)

        with self.assertRaisesRegex(
            diagnostics.DiagnosticsError, "outside the operation output"
        ):
            diagnostics._fresh_measure(link, output)

        # The linked measure directory and its external target are untouched.
        self.assertEqual((outside / "keep.txt").read_bytes(), b"keep")
        self.assertFalse((outside / "snapshots").exists())
        self.assertTrue(link.is_symlink())

    def test_remove_owned_validates_a_child_that_does_not_exist_yet(self) -> None:
        root, _repository, output = self.workspace()
        outside = root / "external"
        outside.mkdir()
        link = output / "leak" / "mode" / "scenario" / "measure"
        link.parent.mkdir(parents=True)
        os.symlink(outside, link, target_is_directory=True)

        with self.assertRaisesRegex(
            diagnostics.DiagnosticsError, "outside the operation output"
        ):
            diagnostics._remove_owned(link / "snapshots", output)

        self.assertFalse((outside / "snapshots").exists())
        self.assertTrue(link.is_symlink())


class LeakTests(WorkspaceTestCase):
    def test_leaks_builds_the_probe_and_gates_every_mode_scenario(self) -> None:
        root, repository, output = self.workspace()
        tools = _tools(root)
        runner = FakeRunner(_handler())
        audits: list[tuple] = []

        def auditor(repository_, architectures, audit_output, *, include_leak_probe):
            audits.append((repository_, architectures, audit_output, include_leak_probe))

        summaries = diagnostics.test_leaks(
            repository, output, warmup=2, iterations=3, windows=4, tolerance_bytes=8,
            tools=tools, runner=runner, host="x64", auditor=auditor,
        )

        expected = tuple(
            output / "leak" / mode / scenario / "measure" / "summary.json"
            for mode in diagnostics.LEAK_MODES for scenario in diagnostics.LEAK_SCENARIOS
        )
        self.assertEqual(summaries, expected)
        self.assertEqual(audits, [(repository, ("x64",), output / "audit", True)])
        leak_calls = [call for call in runner.calls if call.argv[1:3] == ("-m", "core.leak")]
        self.assertEqual(len(leak_calls), 1 + 3 * len(expected))
        self.assertEqual(leak_calls[0].argv[3], "setup")
        binary = native.bin_directory(repository, "x64", "Release")
        self.assertEqual(
            leak_calls[0].argv,
            (str(diagnostics._PYTHON), "-m", "core.leak", "setup",
             str(binary / "leak-probe.exe"),
             *[str(binary / native.BINARIES[name]) for name in diagnostics.PROJECTS]),
        )
        measure = next(call for call in leak_calls if call.argv[3] == "measure")
        self.assertEqual(measure.argv[5], str(tools.umdh))
        self.assertIn(measure.argv[6], diagnostics.LEAK_MODES)
        self.assertIn(measure.argv[7], diagnostics.LEAK_SCENARIOS)
        self.assertEqual(measure.argv[-4:], ("2", "3", "4", "8"))
        self.assertEqual(measure.argv[4], str(output / "leak" / "setup"))
        self.assertEqual(
            measure.env["OBSERVER_OUT_DIR"],
            str(output / "leak" / diagnostics.LEAK_MODES[0]
                / diagnostics.LEAK_SCENARIOS[0] / "measure"),
        )
        builds = runner.commands("msbuild.exe")
        self.assertEqual(len(builds), 2)
        self.assertEqual(
            builds[1].argv[1],
            str(repository / "build" / "projects" / "leak-probe.vcxproj"),
        )

    def test_the_default_auditor_delegates_to_packaging_ops(self) -> None:
        root, repository, output = self.workspace()
        recorded: dict[str, object] = {}
        module = types.ModuleType("packaging_ops")

        def audit_binaries(repository_, architectures, audit_output, *, include_leak_probe=False):
            recorded["call"] = (repository_, architectures, audit_output, include_leak_probe)

        module.audit_binaries = audit_binaries  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"packaging_ops": module}):
            diagnostics._default_auditor(
                repository, ("x64",), output / "audit", include_leak_probe=True
            )

        self.assertEqual(
            recorded["call"], (repository, ("x64",), output / "audit", True)
        )

    def test_the_default_auditor_creates_its_output_directory_before_delegating(self) -> None:
        root, repository, output = self.workspace()
        audit_output = output / "audit"
        module = types.ModuleType("packaging_ops")
        seen: dict[str, Path] = {}

        def audit_binaries(repository_, architectures, output_, *, include_leak_probe=False):
            # The real packaging_ops.audit_binaries.audit refuses a missing output root with a
            # PackagingError; the default auditor must materialise it before delegating.
            if not Path(output_).is_dir():
                raise diagnostics.DiagnosticsError(
                    f"output directory must be an existing directory: {output_}"
                )
            seen["output"] = Path(output_)

        module.audit_binaries = audit_binaries  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"packaging_ops": module}):
            diagnostics._default_auditor(
                repository, ("x64",), audit_output, include_leak_probe=True
            )

        self.assertTrue(audit_output.is_dir())
        self.assertEqual(seen["output"], audit_output)

    def test_the_default_auditor_refuses_a_linked_output_before_creating_it(self) -> None:
        root, repository, _output = self.workspace()
        external = root / "external"
        external.mkdir()
        (external / "keep.txt").write_bytes(b"keep")
        linked = root / "linked"
        os.symlink(external, linked, target_is_directory=True)
        audit_output = linked / "audit"
        module = types.ModuleType("packaging_ops")
        delegations: list[tuple] = []

        def audit_binaries(repository_, architectures, output_, *, include_leak_probe=False):
            delegations.append((repository_, architectures, output_, include_leak_probe))

        module.audit_binaries = audit_binaries  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"packaging_ops": module}):
            with self.assertRaisesRegex(diagnostics.DiagnosticsError, "reparse point"):
                diagnostics._default_auditor(
                    repository, ("x64",), audit_output, include_leak_probe=True
                )

        # The linked parent and its external target are untouched, the missing audit child was
        # never materialised outside the operation, and packaging never ran.
        self.assertEqual(delegations, [])
        self.assertFalse((external / "audit").exists())
        self.assertEqual((external / "keep.txt").read_bytes(), b"keep")
        self.assertTrue(linked.is_symlink())

    def test_the_default_audit_runs_before_the_leak_setup(self) -> None:
        root, repository, output = self.workspace()
        module = types.ModuleType("packaging_ops")
        order: list[str] = []

        def audit_binaries(repository_, architectures, audit_output, *, include_leak_probe=False):
            order.append("audit")

        module.audit_binaries = audit_binaries  # type: ignore[attr-defined]
        runner = FakeRunner(_handler())
        with mock.patch.dict(sys.modules, {"packaging_ops": module}):
            diagnostics.test_leaks(
                repository, output, warmup=1, iterations=1, windows=3,
                tools=_tools(root), runner=runner, host="x64",
            )

        self.assertEqual(order, ["audit"])
        setup = next(
            call for call in runner.calls
            if call.argv[1:4] == ("-m", "core.leak", "setup")
        )
        self.assertEqual(setup.argv[3], "setup")

    def test_a_failing_audit_stops_before_the_leak_setup(self) -> None:
        root, repository, output = self.workspace()
        runner = FakeRunner()

        def auditor(*args, **kwargs):
            raise diagnostics.DiagnosticsError("binary audit failed")

        with self.assertRaisesRegex(diagnostics.DiagnosticsError, "binary audit failed"):
            diagnostics.test_leaks(
                repository, output, tools=_tools(root), runner=runner,
                host="x64", auditor=auditor,
            )
        self.assertEqual(
            [call for call in runner.calls if "core.leak" in call.argv], []
        )
        self.assertEqual(len(runner.commands("msbuild.exe")), 2)

    def test_a_repeated_leak_run_refreshes_the_measurement_directories(self) -> None:
        root, repository, output = self.workspace()
        scope = output / "leak" / diagnostics.LEAK_MODES[0] / diagnostics.LEAK_SCENARIOS[0]
        for name in ("snapshots", "diffs"):
            stale = scope / "measure" / name / "old.txt"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"old")

        diagnostics.test_leaks(
            repository, output, warmup=1, iterations=1, windows=3,
            tools=_tools(root), runner=FakeRunner(_handler()), host="x64",
            auditor=lambda *args, **kwargs: None,
        )

        for name in ("snapshots", "diffs"):
            self.assertFalse((scope / "measure" / name / "old.txt").exists())

    def test_leak_measurements_are_validated_before_building(self) -> None:
        root, repository, output = self.workspace()
        runner = FakeRunner()

        for options in (
            {"warmup": 0}, {"iterations": 0}, {"windows": 0}, {"windows": 2},
            {"tolerance_bytes": -1}, {"warmup": True},
        ):
            with self.assertRaises(ValueError):
                diagnostics.test_leaks(
                    repository, output, tools=_tools(root), runner=runner,
                    host="x64", **options,
                )
        self.assertEqual(runner.calls, [])

    def test_leaks_refuse_a_host_that_cannot_run_x64(self) -> None:
        root, repository, output = self.workspace()
        runner = FakeRunner()

        with self.assertRaisesRegex(ValueError, "cannot run"):
            diagnostics.test_leaks(
                repository, output, tools=_tools(root), runner=runner, host="x86",
            )
        self.assertEqual(runner.calls, [])


class ToolsTests(unittest.TestCase):
    def test_the_tool_bag_exposes_the_vcpkg_root(self) -> None:
        root = Path("C:/tools/vcpkg")
        tools = diagnostics.DiagnosticsTools(
            msbuild=Path("C:/tools/MSBuild.exe"),
            vcpkg=root / "vcpkg.exe",
            llvm_install=Path("C:/llvm"),
            llvm_cov=Path("C:/tools/llvm-cov.exe"),
            llvm_profdata=Path("C:/tools/llvm-profdata.exe"),
            llvm_runtimes={},
            asan_runtimes={},
            umdh=Path("C:/tools/umdh.exe"),
        )

        self.assertEqual(tools.vcpkg_root, root)


if __name__ == "__main__":
    unittest.main()

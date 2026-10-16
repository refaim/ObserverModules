"""Focused regression contracts for the driver findings (review 2026-10-12).

Each test encodes the observable behaviour ``build/main.py`` or ``build/leaks.py`` must
have after the review fixes, so every test is expected to fail against the frozen
production.  The assertions are about native command shape, path composition and failure
behaviour reached through the public operations (``ci_arch``, ``ci_source``, ``build``,
``main``) and the ``leaks`` worker, with ``main.run`` and the tool lookups faked so no real
compiler, debugger or network is ever touched.  A test that fails because a helper the fix
introduces is still missing is a missing-signature failure; a test that fails on an
assertion is a wrong-behaviour failure.  Both are the intended RED for this phase.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import leaks  # noqa: E402
import licenses  # noqa: E402
import main  # noqa: E402
from leaks import LeakError  # noqa: E402


FULL_COVERAGE = {
    "data": [{"totals": {"lines": {"count": 10, "covered": 10}, "branches": {"count": 4, "covered": 4}}}]
}


# The full-LCOV export must return real LCOV records, distinct from the JSON summary.
FULL_LCOV = (
    "SF:src/marker.cpp\n"
    "DA:7,1\n"
    "DA:8,0\n"
    "BRDA:9,0,0,1\n"
    "BRF:1\n"
    "BRH:1\n"
    "end_of_record\n"
)


def _dumpbin(argv) -> str:
    flag = argv[1]
    if flag == "/headers":
        return "8664 machine (x64)" if "x64" in argv[2] else "14C machine (x86)"
    if flag == "/dependents":
        return "    KERNEL32.dll"
    return "   1    0 00001000 LoadSubModule\n   2    0 00001010 UnloadSubModule"


def _fake_git(repository, *arguments):
    """Deterministic stand-in for the single ``git`` boundary the Release attestation reads."""

    if arguments == ("ls-files", "-z"):
        return "vcpkg.json\x00"
    if arguments == ("rev-parse", "HEAD"):
        return "a" * 40
    if arguments == ("rev-parse", "HEAD^{tree}"):
        return "b" * 40
    raise AssertionError(f"unexpected git invocation: {arguments}")


class PipelineRun:
    """A checked ``main.run`` stand-in covering every tool the CI operations reach."""

    def __init__(self, *, vswhere: str = "", dumpbin=None, sarif=None, junit_parents: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._vswhere = vswhere
        self._dumpbin = dumpbin
        self._sarif = sarif
        self._junit_parents = junit_parents

    def __call__(self, argv, cwd=None, env=None, capture=False, **_options: object) -> subprocess.CompletedProcess:
        recorded = tuple(str(item) for item in argv)
        self.calls.append(recorded)
        name = os.path.basename(recorded[0]).lower()
        if self._junit_parents:
            for item in recorded:
                if item.startswith("JUnit::out="):
                    report = Path(item.split("=", 1)[1])
                    if not report.parent.is_dir():
                        raise subprocess.CalledProcessError(1, list(recorded), f"cannot create {report.parent}")
        if name == "binskim.exe":
            document = self._sarif if self._sarif is not None else {"version": "2.1.0", "runs": []}
            destination = Path(recorded[recorded.index("--output") + 1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(document), encoding="utf-8")
            return subprocess.CompletedProcess(list(recorded), 0, "")
        if capture:
            if "vswhere" in name:
                return subprocess.CompletedProcess(list(recorded), 0, self._vswhere)
            if name == "clang-format.exe":
                return subprocess.CompletedProcess(list(recorded), 0, "clang-format version 23.1.3\n")
            if name == "dumpbin.exe" and self._dumpbin is not None:
                return subprocess.CompletedProcess(list(recorded), 0, self._dumpbin(recorded))
            if "llvm-cov" in name:
                if any("--format=lcov" in item for item in recorded):
                    return subprocess.CompletedProcess(list(recorded), 0, FULL_LCOV)
                return subprocess.CompletedProcess(list(recorded), 0, json.dumps(FULL_COVERAGE))
            return subprocess.CompletedProcess(list(recorded), 0, "")
        if env and "LLVM_PROFILE_FILE" in env:
            path = Path(env["LLVM_PROFILE_FILE"].replace("%m", "1").replace("%p", "2"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"raw")
        return subprocess.CompletedProcess(list(recorded), 0, None)

    def tools(self) -> list[str]:
        return [os.path.basename(call[0]).lower() for call in self.calls]

    def by_tool(self, tool: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if os.path.basename(call[0]).lower() == tool]


def _properties(argv: tuple[str, ...]) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in argv:
        match = re.match(r"^[-/](?:p|property):(?P<name>[^=]+)=(?P<value>.*)$", item)
        if match is not None:
            found[match.group("name")] = match.group("value")
    return found


class DriverCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "vcpkg.json").write_text('{"name": "unpackrat"}\n', encoding="utf-8")

        # Hard guard: nothing here may start a real native tool even from a partial driver.
        forbidden = AssertionError("a real subprocess launch is forbidden in this test")
        self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=forbidden))
        self.enterContext(mock.patch.object(subprocess, "run", side_effect=forbidden))

        self.llvm = main.Llvm(
            directory=self.root / "llvm",
            runtime=self.root / "llvm" / "runtime",
            cov=self.root / "llvm-cov.exe",
            profdata=self.root / "llvm-profdata.exe",
        )
        self.llvm.runtime.mkdir(parents=True)
        for name in main._ASAN_RUNTIMES.values():
            (self.llvm.runtime / name).write_bytes(b"matched-llvm21")
        self.asan = self.root / "asan.dll"
        self.asan.write_bytes(b"x")
        self.msbuild = self.root / "MSBuild.exe"
        self.vcpkg = self.root / "vcpkg.exe"
        self.vs_install = self.root / "vs"
        # The driver selects the selected 14.51 servicing toolset from the chosen VS install.
        cl = (self.vs_install / "VC" / "Tools" / "MSVC" / "14.51.36231"
              / "bin" / "Hostx64" / "x64" / "cl.exe")
        cl.parent.mkdir(parents=True, exist_ok=True)
        cl.write_bytes(b"x")

    def tools(self, run: PipelineRun):
        stack = contextlib.ExitStack()
        # A Release build records an attestation that reads the checkout through the git boundary.
        stack.enter_context(mock.patch.object(licenses, "_git", _fake_git))
        stack.enter_context(
            mock.patch.multiple(
                main,
                create=True,
                run=run,
                _native_tools=mock.Mock(return_value=(self.msbuild, self.vcpkg)),
                _llvm=mock.Mock(return_value=self.llvm),
                _clang_tidy=mock.Mock(return_value=self.root / "clang-tidy.exe"),
                _find_dumpbin=mock.Mock(return_value=self.root / "dumpbin.exe"),
                _find_umdh=mock.Mock(return_value=self.root / "umdh.exe"),
                _asan_runtime=mock.Mock(return_value=self.asan),
                _find_binskimm=mock.Mock(return_value=self.root / "binskim.exe"),
                # Debug tests and the coverage unit scope launch the fatal cleanup probe; the tool
                # fake must never let that escape into a real subprocess.
                _cleanup_fatal_probe=mock.Mock(),
            )
        )
        return stack

    def make_module(self, module: str = "renpy", version: str = "1.2.3") -> None:
        module_dir = self.repo / "src" / "modules" / module
        module_dir.mkdir(parents=True)
        (module_dir / "VERSION").write_text(version + "\n", encoding="utf-8")
        (module_dir / "observer_user.ini").write_bytes(b"[Modules]\n")
        (module_dir / "ChangeLog.en.txt").write_bytes(b"Version 1.2.3\n-\n+ note\n")
        (module_dir / "ChangeLog.ru.txt").write_bytes("\u0412\u0435\u0440\u0441\u0438\u044f 1.2.3\n-\n+ note\n".encode("utf-8"))
        (self.repo / "LICENSE.txt").write_bytes(b"MIT license text\n")
        licenses = self.repo / "licenses"
        licenses.mkdir(exist_ok=True)
        for name in ("Observer.txt", "GPL-3.0.txt", "rpatool.txt", "serde-pickle.txt", "zlib-ng.txt", "rgssad.txt"):
            (licenses / name).write_bytes(name.encode() + b" notice")
        for architecture in main.ARCHITECTURES:
            stage = self.repo / "out" / "native" / "bin" / architecture / "Release"
            stage.mkdir(parents=True, exist_ok=True)
            for name in (*main.MODULE_BINARIES.values(), main.LEAK_PROBE):
                (stage / name).write_bytes(b"binary")
                (stage / name).with_suffix(".pdb").write_bytes(b"symbols")
        for target in main.FUZZ_TARGETS:
            seeds = self.repo / "src" / "fuzz" / "corpus" / target
            seeds.mkdir(parents=True, exist_ok=True)
            (seeds / "seed.hex").write_text("414243", encoding="ascii")

    # ----- finding 3: fresh-tree JUnit report directories ---------------------------------

    def test_junit_fixture_rejects_a_missing_parent_before_the_tool_can_succeed(self) -> None:
        run = PipelineRun(junit_parents=True)
        with self.assertRaisesRegex(subprocess.CalledProcessError, "non-zero exit status 1"):
            run(("unit.exe", f"JUnit::out={self.root / 'absent' / 'report.xml'}"))
        self.assertEqual(run(("unrecognized.exe",), capture=True).stdout, "")

    def test_coverage_creates_the_junit_report_directory(self) -> None:
        run = PipelineRun(vswhere=str(self.vs_install), junit_parents=True)
        with self.tools(run):
            main._coverage(self.repo, "x64", self.msbuild, self.vcpkg, self.llvm)
        reports = self.repo / "out" / "native" / "reports" / "tests" / "x64" / "Coverage" / "unit"
        self.assertTrue(reports.is_dir(), "coverage must create the JUnit report directory before the suite runs")

    def test_sanitizer_creates_the_junit_report_directory(self) -> None:
        run = PipelineRun(vswhere=str(self.vs_install), junit_parents=True)
        with self.tools(run):
            main._sanitizer(self.repo, "asan", "x64", self.msbuild, self.vcpkg, self.llvm)
        reports = self.repo / "out" / "native" / "reports" / "tests" / "x64" / "ASan" / "asan"
        self.assertTrue(reports.is_dir(), "the sanitizer must create the JUnit report directory before the suite runs")

    # ----- finding 4: native analyze / clang-tidy / BinSkim ------------------------------

    def test_release_build_enables_msvc_analyze_and_clang_tidy(self) -> None:
        run = PipelineRun(vswhere=str(self.root / "vs"))
        with self.tools(run):
            main.build("x64", "Release", repository=self.repo)
        msbuild = run.by_tool("msbuild.exe")
        self.assertEqual(len(msbuild), 1)
        properties = _properties(msbuild[0])
        self.assertEqual(properties.get("RunCodeAnalysis"), "true", "the Release build must run the native analyzer")
        self.assertEqual(
            properties.get("EnableClangTidyCodeAnalysis"), "true", "the Release build must run clang-tidy"
        )
        clang_tidy = properties.get("ClangTidyToolExeAdditionalOptions", "")
        self.assertIn("warnings-as-errors", clang_tidy.replace(" ", ""), clang_tidy)
        self.assertIn("*", clang_tidy, clang_tidy)

    def test_ci_arch_fails_when_binskim_reports_a_warning(self) -> None:
        self.make_module()
        warning = {
            "version": "2.1.0",
            "runs": [{"results": [{"ruleId": "BA2001", "level": "warning", "message": {"text": "load config"}}]}],
        }
        run = PipelineRun(vswhere=str(self.vs_install), dumpbin=_dumpbin, sarif=warning)
        with self.tools(run), self.assertRaises(main.BuildError):
            main.ci_arch(["x64"], repository=self.repo)

    def test_ci_arch_accepts_omitted_level_hint_only_for_ba2027(self) -> None:
        self.make_module()
        # A result with no explicit level follows the SARIF default (warning) and must fail...
        omitted = {
            "version": "2.1.0",
            "runs": [{"results": [{"ruleId": "BA2001", "message": {"text": "no level"}}]}],
        }
        run = PipelineRun(vswhere=str(self.vs_install), dumpbin=_dumpbin, sarif=omitted)
        with self.tools(run), self.assertRaises(main.BuildError):
            main.ci_arch(["x64"], repository=self.repo)
        # ...while only the BA2027 *warning* is the single documented exception.
        exempt = {
            "version": "2.1.0",
            "runs": [{"results": [{"ruleId": "BA2027", "level": "warning", "message": {"text": "exempt"}}]}],
        }
        run = PipelineRun(vswhere=str(self.vs_install), dumpbin=_dumpbin, sarif=exempt)
        with self.tools(run):
            main.ci_arch(["x64"], repository=self.repo)

    # ----- finding 5: PSScriptAnalyzer scope ---------------------------------------------

    def test_pssa_scans_root_and_build_scripts_not_only_src(self) -> None:
        self.repo.joinpath("src").mkdir()
        (self.repo / "src" / "app.ps1").write_bytes(b"x")
        (self.repo / "build").mkdir()
        (self.repo / "build" / "tool.ps1").write_bytes(b"x")
        (self.repo / "root.ps1").write_bytes(b"x")
        for ignored in ("out", ".git", ".venv"):
            directory = self.repo / ignored
            directory.mkdir()
            (directory / "skip.ps1").write_bytes(b"x")
        run = PipelineRun()
        with mock.patch.object(main, "run", run):
            main._pssa(self.repo, main.SourceTools(self.root / "cf.exe", self.root / "cc.exe", self.root / "pwsh.exe"))
        pwsh = run.by_tool("pwsh.exe")
        self.assertEqual(len(pwsh), 1)
        scanned = {Path(item) for item in pwsh[0][pwsh[0].index("-Settings") + 2:]}
        self.assertIn(self.repo / "root.ps1", scanned)
        self.assertIn(self.repo / "build" / "tool.ps1", scanned)
        self.assertIn(self.repo / "src" / "app.ps1", scanned)
        for ignored in ("out", ".git", ".venv"):
            self.assertNotIn(self.repo / ignored / "skip.ps1", scanned)

    # ----- finding 6: -Jobs reaches MSBuild ----------------------------------------------

    def test_jobs_reaches_the_build_and_test_msbuild_invocations(self) -> None:
        for command in ("build", "test"):
            with self.subTest(command=command):
                run = PipelineRun(vswhere=str(self.root / "vs"))
                with self.tools(run):
                    self.assertEqual(
                        main.main([command, "-Repository", str(self.repo), "-Arch", "x64", "-Config", "Debug", "-Jobs", "3"]),
                        0,
                    )
                msbuild = run.by_tool("msbuild.exe")
                self.assertTrue(msbuild, f"{command} must drive MSBuild")
                self.assertTrue(
                    all("/m:3" in call for call in msbuild),
                    f"{command} did not forward -Jobs 3 to MSBuild: {msbuild[0]}",
                )

    # ----- finding 7: no Python version validator ----------------------------------------

    def test_read_version_reads_an_opaque_version_without_python_validation(self) -> None:
        module = self.repo / "src" / "modules" / "renpy"
        module.mkdir(parents=True)
        (module / "VERSION").write_text("opaque-build-42\n", encoding="utf-8")
        self.assertEqual(main.read_version(self.repo, "renpy"), "opaque-build-42")
        self.assertFalse(
            hasattr(main, "parse_version"),
            "the redundant Python version parser must be removed; MSBuild owns the format",
        )

    # ----- finding 10: bounded leak marker wait and UMDH tool timeouts --------------------

    def test_leak_read_accepts_a_timeout_bound(self) -> None:
        process = _StallingProbe()
        with self.assertRaises(LeakError):
            leaks._read(process, "READY", "", timeout=0.05)

    def test_umdh_snapshot_and_diff_invocations_have_a_finite_timeout(self) -> None:
        environment = self._leak_environment()
        recorded: list[dict] = []

        def runner(argv, **options: object) -> subprocess.CompletedProcess:
            recorded.append({"argv": list(argv), "options": options})
            if os.path.basename(argv[0]).lower() == "gflags.exe":
                return subprocess.CompletedProcess(list(argv), 0, "Current Registry Settings for leak-probe.exe executable are: 00000000")
            Path(argv[-1].removeprefix("-f:")).write_text(
                "Total increase == 0\n" if "-d" in argv else "BackTrace\n", encoding="utf-8"
            )
            return subprocess.CompletedProcess(list(argv), 0, "")

        with environment, mock.patch("leaks.psutil.Popen", return_value=_CleanProbe()), mock.patch(
            "leaks.subprocess.run", side_effect=runner
        ):
            leaks.main(("measure", str(self.root), str(self.root / "umdh.exe"), "operations", "malformed", "1", "2", "3", "0"))

        umdh_calls = [call for call in recorded if os.path.basename(call["argv"][0]).lower() == "umdh.exe"]
        self.assertTrue(umdh_calls, "the leak worker must invoke UMDH")
        for call in umdh_calls:
            timeout = call["options"].get("timeout")
            self.assertIsInstance(timeout, (int, float), f"UMDH invocation has no finite timeout: {call['argv']}")
            self.assertGreater(timeout, 0)

    def test_umdh_timeout_surfaces_as_a_leak_error_and_kills_the_probe(self) -> None:
        probe = _CleanProbe()

        def runner(argv, **options: object) -> subprocess.CompletedProcess:
            if os.path.basename(argv[0]).lower() == "gflags.exe":
                return subprocess.CompletedProcess(list(argv), 0, "Current Registry Settings for leak-probe.exe executable are: 00000000")
            raise subprocess.TimeoutExpired(list(argv), 1)

        with self._leak_environment(), mock.patch(
            "leaks.psutil.Popen", return_value=probe
        ), mock.patch("leaks.subprocess.run", side_effect=runner), mock.patch("leaks.psutil.wait_procs"), self.assertRaises(LeakError):
            leaks.main(("measure", str(self.root), str(self.root / "umdh.exe"), "operations", "malformed", "1", "2", "3", "0"))
        self.assertTrue(probe.killed, "a stalled UMDH invocation must still reap the probe")

    def _leak_environment(self):
        for name in ("leak-probe.exe", "umdh.exe", "gflags.exe"):
            (self.root / name).touch()
        output = self.root / "evidence"
        output.mkdir(exist_ok=True)
        return mock.patch.dict(os.environ, {"UNPACKRAT_OUT_DIR": str(output)}, clear=False)

    # ----- finding 11: ordinary-console tool lookup --------------------------------------

    def test_source_and_llvm_tools_resolve_from_the_installed_vs_without_path(self) -> None:
        install = self.root / "vs"
        llvm_bin = install / "VC" / "Tools" / "Llvm" / "x64" / "bin"
        llvm_bin.mkdir(parents=True)
        (llvm_bin / "clang-cl.exe").write_bytes(b"x")
        (install / "VC" / "Tools" / "Llvm" / "x64" / "lib" / "clang" / "19" / "lib" / "windows").mkdir(parents=True)
        # The standalone formatter is installed on the standard PATH, never in a private cache.
        formatter = self.root / "path" / "clang-format.exe"
        formatter.parent.mkdir(parents=True)
        formatter.write_bytes(b"x")
        dumpbin = install / "VC" / "Tools" / "MSVC" / "14.51.36231" / "bin" / "Hostx64" / "x64" / "dumpbin.exe"
        dumpbin.parent.mkdir(parents=True, exist_ok=True)
        dumpbin.write_bytes(b"x")
        pf86 = self.root / "pf86"
        vswhere = pf86 / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        vswhere.parent.mkdir(parents=True)
        vswhere.write_bytes(b"x")

        def which(name: str):
            # An ordinary console: only the standalone formatter is on the standard PATH.
            if name == "clang-format.exe":
                return str(formatter)
            return str(self.root / f"{name}.exe") if name in ("cppcheck", "pwsh") else None

        run = PipelineRun(vswhere=str(install))
        with mock.patch.object(main, "run", run), mock.patch("shutil.which", side_effect=which), mock.patch.dict(
            os.environ,
            {
                "ProgramFiles(x86)": str(pf86),
                "LOCALAPPDATA": str(self.root / "localappdata"),
                "UNPACKRAT_CLANG_FORMAT": "",
                "UNPACKRAT_LLVM_INSTRUMENTATION": str(install / "paired-llvm21"),
            },
        ):
            tools = main._source_tools()
            directory = main._llvm_dir()
            found_dumpbin = main._find_dumpbin()

        # The format gate uses the separately pinned standalone formatter from the PATH, not the
        # VS bundle and not the private user cache.
        self.assertEqual(tools.clang_format, formatter.resolve())
        self.assertFalse((self.root / "localappdata").exists())
        self.assertEqual(directory, install / "paired-llvm21" / "x64")
        self.assertEqual(found_dumpbin, dumpbin)
        # The lookup must use the installed tools, never install anything.
        self.assertTrue(
            all(os.path.basename(call[0]).lower() in ("vswhere.exe", "clang-format.exe") for call in run.calls),
            run.tools(),
        )


class _StallingProbe:
    """A probe whose stdout never yields the marker; the read must be time-bounded."""

    def __init__(self) -> None:
        self.stdout = io.StringIO("noise line\n" * 4)
        self.stdin = io.StringIO()
        self.pid = 77


class _CleanProbe:
    def __init__(self) -> None:
        labels = ("baseline", "window-1", "window-2", "window-3")
        self.stdout = io.StringIO(
            "\n".join(
                [
                    "UNPACKRAT_LEAK_PROBE|READY|pid=77|mode=operations|configuration=Release|scenarios=malformed",
                    *(f"UNPACKRAT_LEAK_PROBE|SNAPSHOT|{label}|pid=77|" for label in labels),
                    "UNPACKRAT_LEAK_PROBE|DONE|pid=77|",
                ]
            )
            + "\n"
        )
        self.stdin = io.StringIO()
        self.pid = 77
        self.returncode: int | None = None
        self.killed = False

    def wait(self, timeout: int = 0) -> int:
        self.returncode = 0
        return 0

    def poll(self) -> int | None:
        return self.returncode

    def children(self, *, recursive: bool) -> list[object]:
        return []

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class AttestationInvalidationCase(unittest.TestCase):
    """A prior Release attestation never survives an operation on the shared restore.

    The shared-root invalidation guard lives in ``_restore`` itself, because ``ci_source``,
    ``_coverage``, ``_sanitizer``, ``_prepare_leak_probe`` and ``_analyze`` reach the restore
    directly; only a later successful Release build may publish a fresh stamp.  The real ``_restore``
    runs against a faked child process and tool lookup, so the tests exercise the actual guard
    rather than a mocked boundary.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "vcpkg.json").write_text('{"name": "unpackrat"}\n', encoding="utf-8")
        self.enterContext(mock.patch.object(licenses, "_git", _fake_git))
        self.msbuild = self.root / "MSBuild.exe"
        self.vcpkg = self.root / "vcpkg.exe"
        self.vs = self.root / "vs"

    def _patch(self, *, run=None):
        """Fake only the external child process and the tool lookup; run the real ``_restore``."""

        return mock.patch.multiple(
            main,
            run=run if run is not None else mock.Mock(),
            _native_tools=mock.Mock(return_value=(self.msbuild, self.vcpkg)),
            _vs_installation=mock.Mock(return_value=self.vs),
            # newmaster's ``_restore`` resolves the servicing toolset for the child
            # environment, so the faked Visual Studio tree needs a mocked lookup too.
            _msvc_toolset_version=mock.Mock(return_value="14.51.36231"),
            _msbuild_command=mock.Mock(return_value=("MSBuild.exe", "tests.vcxproj")),
            _cleanup_fatal_probe=mock.Mock(),
        )

    @staticmethod
    def _failing_msbuild(argv, *, cwd=None, env=None, capture=False):
        """Let the restore succeed but fail the following MSBuild step."""

        if os.path.basename(str(argv[0])).lower() == "msbuild.exe":
            raise main.BuildError("msbuild failed")
        return mock.Mock()

    def _attestation(self, architecture: str = "x64") -> Path:
        return licenses.attestation_path(self.repo, architecture)

    def test_a_failed_restore_drops_the_prior_attestation(self) -> None:
        licenses.write_attestation(self.repo, "x64")
        with self._patch(run=mock.Mock(side_effect=main.BuildError("restore failed"))):
            with self.assertRaises(main.BuildError):
                main.build("x64", "Release", repository=self.repo)
        self.assertFalse(self._attestation().is_file())
        with self.assertRaises(licenses.LicenseError):
            licenses.load_attestation(self.repo, "x64")

    def test_a_failed_release_build_drops_the_prior_attestation(self) -> None:
        licenses.write_attestation(self.repo, "x64")
        with self._patch(run=self._failing_msbuild):
            with self.assertRaises(main.BuildError):
                main.build("x64", "Release", repository=self.repo)
        self.assertFalse(self._attestation().is_file())

    def test_a_debug_restore_invalidates_the_shared_attestation(self) -> None:
        licenses.write_attestation(self.repo, "x64")
        with self._patch():
            main.build("x64", "Debug", repository=self.repo)
        self.assertFalse(self._attestation().is_file())

    def test_a_debug_build_never_publishes_an_attestation(self) -> None:
        with self._patch():
            main.build("x64", "Debug", repository=self.repo)
        self.assertFalse(self._attestation().is_file())

    def test_an_isolated_asan_restore_keeps_the_shared_attestation(self) -> None:
        licenses.write_attestation(self.repo, "x64")
        with self._patch():
            main.build("x64", "ASan", repository=self.repo)
        self.assertTrue(self._attestation().is_file())
        licenses.load_attestation(self.repo, "x64")

    def test_a_successful_release_build_replaces_the_prior_attestation(self) -> None:
        licenses.write_attestation(self.repo, "x64")
        before = self._attestation().read_bytes()
        # A changed, uncommitted build input must be re-attested by the successful rebuild rather
        # than silently inherit the previous stamp.
        (self.repo / "vcpkg.json").write_text('{"name": "unpackrat", "x": 1}\n', encoding="utf-8")
        with self._patch():
            main.build("x64", "Release", repository=self.repo)
        document = licenses.load_attestation(self.repo, "x64")
        self.assertNotEqual(before, self._attestation().read_bytes())
        self.assertEqual(
            document["buildInputFingerprint"], licenses.build_input_fingerprint(self.repo)
        )

    def test_a_direct_shared_restore_drops_the_prior_attestation(self) -> None:
        # ``ci_source`` and ``_analyze`` (like the coverage and sanitizer stages) enter through
        # ``_restore`` without ``build``, so the guard must sit in ``_restore`` itself.
        licenses.write_attestation(self.repo, "x64")
        with self._patch():
            main._restore(self.repo, "x64", "Debug", self.vcpkg)
        self.assertFalse(self._attestation().is_file())

    def test_a_failed_direct_shared_restore_still_drops_the_prior_attestation(self) -> None:
        licenses.write_attestation(self.repo, "x64")
        with self._patch(run=mock.Mock(side_effect=main.BuildError("restore failed"))):
            with self.assertRaises(main.BuildError):
                main._restore(self.repo, "x64", "Release", self.vcpkg)
        self.assertFalse(self._attestation().is_file())

    def test_a_direct_isolated_restore_keeps_the_shared_attestation(self) -> None:
        licenses.write_attestation(self.repo, "x64")
        with self._patch():
            main._restore(self.repo, "x64", "ASan", self.vcpkg)
        self.assertTrue(self._attestation().is_file())
        licenses.load_attestation(self.repo, "x64")

    def test_a_successful_leak_probe_republishes_the_attestation(self) -> None:
        # The probe restore mutates the x64 Release tree and drops the stamp, so the finished probe
        # build is the last successful Release work and must republish a fresh stamp.
        licenses.write_attestation(self.repo, "x64")
        (self.repo / "vcpkg.json").write_text('{"name": "unpackrat", "x": 1}\n', encoding="utf-8")
        with self._patch():
            main._prepare_leak_probe(self.repo, self.msbuild, self.vcpkg)
        document = licenses.load_attestation(self.repo, "x64")
        self.assertEqual(
            document["buildInputFingerprint"], licenses.build_input_fingerprint(self.repo)
        )

    def test_a_failed_leak_probe_restore_leaves_no_attestation(self) -> None:
        licenses.write_attestation(self.repo, "x64")
        with self._patch(run=mock.Mock(side_effect=main.BuildError("restore failed"))):
            with self.assertRaises(main.BuildError):
                main._prepare_leak_probe(self.repo, self.msbuild, self.vcpkg)
        self.assertFalse(self._attestation().is_file())

    def test_a_failed_leak_probe_build_leaves_no_attestation(self) -> None:
        licenses.write_attestation(self.repo, "x64")
        with self._patch(run=self._failing_msbuild):
            with self.assertRaises(main.BuildError):
                main._prepare_leak_probe(self.repo, self.msbuild, self.vcpkg)
        self.assertFalse(self._attestation().is_file())


if __name__ == "__main__":
    unittest.main()

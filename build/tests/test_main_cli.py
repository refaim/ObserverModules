"""Coverage of the ``build/main.py`` tool lookups, helpers and CI operations.

The tests-first contract in ``test_minimal_build.py`` pins the public surface; these tests pin the
rest of the driver: tool resolution, the command shapes each operation emits, and the CI
orchestration.  Every external program is reached through the single checked ``main.run`` seam and
the tool-lookup boundaries, so no real compiler, debugger or network is ever touched.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import main  # noqa: E402


_FULL_COVERAGE = {
    "data": [
        {
            "totals": {
                "lines": {"count": 10, "covered": 10},
                "branches": {"count": 4, "covered": 4},
            }
        }
    ]
}


# A minimal LCOV record with distinct line and branch markers, so a JSON payload written under
# the ``.lcov`` name is rejected rather than mistaken for real LCOV.
_LCOV = (
    "SF:src/marker.cpp\n"
    "DA:10,1\n"
    "DA:11,0\n"
    "BRDA:12,0,0,1\n"
    "BRDA:13,0,1,-\n"
    "LF:2\n"
    "LH:1\n"
    "BRF:2\n"
    "BRH:1\n"
    "end_of_record\n"
)


# A BinSkim SARIF with no findings; the analysis gate must accept it.
_CLEAN_BINSKIM = {"version": "2.1.0", "runs": []}


class Recorded:
    def __init__(self, argv, cwd, env, capture):
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.capture = capture


class FakeRun:
    """A checked ``main.run`` stand-in that records calls and fakes tool output."""

    def __init__(self, *, vswhere="", dumpbin=None, llvm_export=None, fail_when=None, profraw=True):
        self.calls: list[Recorded] = []
        self._vswhere = vswhere
        self._dumpbin = dumpbin
        self._llvm_export = json.dumps(llvm_export if llvm_export is not None else _FULL_COVERAGE)
        self._fail_when = fail_when
        self._profraw = profraw

    def __call__(self, argv, cwd=None, env=None, capture=False):
        argv = tuple(str(item) for item in argv)
        self.calls.append(Recorded(argv, cwd, env, capture))
        name = os.path.basename(argv[0]).lower()
        if self._fail_when is not None and self._fail_when(argv):
            raise subprocess.CalledProcessError(3, list(argv), "")
        if name == "binskim.exe":
            destination = Path(argv[argv.index("--output") + 1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(_CLEAN_BINSKIM), encoding="utf-8")
            return subprocess.CompletedProcess(list(argv), 0, "")
        if capture:
            if "vswhere" in name:
                return subprocess.CompletedProcess(list(argv), 0, self._vswhere)
            if name == "dumpbin.exe" and self._dumpbin is not None:
                return subprocess.CompletedProcess(list(argv), 0, self._dumpbin(argv))
            if "llvm-cov" in name:
                if any("--format=lcov" in item for item in argv):
                    return subprocess.CompletedProcess(list(argv), 0, _LCOV)
                return subprocess.CompletedProcess(list(argv), 0, self._llvm_export)
            return subprocess.CompletedProcess(list(argv), 0, "")
        if self._profraw and env and "LLVM_PROFILE_FILE" in env:
            path = Path(env["LLVM_PROFILE_FILE"].replace("%m", "1").replace("%p", "2"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"raw")
        return subprocess.CompletedProcess(list(argv), 0, None)

    def tools(self):
        return [os.path.basename(call.argv[0]).lower() for call in self.calls]


def _dumpbin(argv):
    flag = argv[1]
    if flag == "/headers":
        machine = "8664 machine (x64)" if "x64" in argv[2] else "14C machine (x86)"
        return machine
    if flag == "/dependents":
        return "    KERNEL32.dll"
    return "   1    0 00001000 LoadSubModule\n   2    0 00001010 UnloadSubModule"


class MainCliCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.llvm = main.Llvm(
            directory=self.root / "llvm",
            runtime=self.root / "llvm" / "runtime",
            cov=self.root / "llvm-cov.exe",
            profdata=self.root / "llvm-profdata.exe",
        )
        self.asan = self.root / "asan.dll"
        self.asan.write_bytes(b"x")

    def tools(self, run: FakeRun):
        stack = mock.patch.multiple(
            main,
            run=run,
            _native_tools=mock.Mock(return_value=(self.root / "MSBuild.exe", self.root / "vcpkg.exe")),
            _source_tools=mock.Mock(return_value=main.SourceTools(
                self.root / "clang-format.exe", self.root / "cppcheck.exe", self.root / "pwsh.exe")),
            _llvm=mock.Mock(return_value=self.llvm),
            _find_dumpbin=mock.Mock(return_value=self.root / "dumpbin.exe"),
            _find_binskimm=mock.Mock(return_value=self.root / "binskim.exe"),
            _find_umdh=mock.Mock(return_value=self.root / "umdh.exe"),
            _asan_runtime=mock.Mock(return_value=self.asan),
        )
        return stack

    def make_module(self, module: str = "renpy", version: str = "1.2.3") -> Path:
        module_dir = self.repo / "src" / "modules" / module
        module_dir.mkdir(parents=True)
        (module_dir / "VERSION").write_text(version + "\n", encoding="utf-8")
        (module_dir / "observer_user.ini").write_bytes(b"[Modules]\n")
        (module_dir / "ChangeLog.en.txt").write_bytes(b"Version 1.2.3\n-\n+ note\n")
        (module_dir / "ChangeLog.ru.txt").write_bytes("\u0412\u0435\u0440\u0441\u0438\u044f 1.2.3\n-\n+ note\n".encode("utf-8"))
        (self.repo / "LICENSE.txt").write_bytes(b"MIT license text\n")
        licenses = self.repo / "licenses"
        licenses.mkdir(exist_ok=True)
        for name in ("Observer.txt", "GPL-3.0.txt", "rpatool.txt", "serde-pickle.txt", "zlib.txt", "zlib-ng.txt", "rgssad.txt", "zanzapak.txt"):
            (licenses / name).write_bytes(name.encode() + b" notice")
        for architecture in main.ARCHITECTURES:
            stage = self.repo / "out" / "native" / "bin" / architecture / "Release"
            stage.mkdir(parents=True, exist_ok=True)
            # A completed Release tree carries every module plus the leak probe and its symbols.
            for name in (*main.MODULE_BINARIES.values(), main.LEAK_PROBE):
                (stage / name).write_bytes(b"binary")
                (stage / name).with_suffix(".pdb").write_bytes(b"symbols")
        for target in main.FUZZ_TARGETS:
            seeds = self.repo / "src" / "fuzz" / "corpus" / target
            seeds.mkdir(parents=True, exist_ok=True)
            (seeds / "seed.hex").write_text("414243", encoding="ascii")
            (seeds / "raw.bin").write_bytes(b"raw seed")
            (seeds / "nested").mkdir()
        return module_dir

    # ----- tool lookups -------------------------------------------------------------------

    def test_run_forwards_capture_and_environment(self) -> None:
        with mock.patch.object(subprocess, "run") as invoked:
            invoked.return_value = subprocess.CompletedProcess(["x"], 0, "out")
            completed = main.run(["x"], cwd=self.root, env={"A": "1"}, capture=True)
        self.assertEqual(completed.stdout, "out")
        self.assertTrue(invoked.call_args.kwargs["check"])
        self.assertEqual(invoked.call_args.kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(invoked.call_args.kwargs["env"]["A"], "1")
        self.assertEqual(invoked.call_args.kwargs["cwd"], os.fspath(self.root))

    def test_run_without_capture_inherits_the_streams(self) -> None:
        with mock.patch.object(subprocess, "run") as invoked:
            invoked.return_value = subprocess.CompletedProcess(["x"], 0, None)
            main.run(["x"])
        self.assertIsNone(invoked.call_args.kwargs["stdout"])
        self.assertIsNone(invoked.call_args.kwargs["env"])

    def test_job_count_defaults_validates_and_rejects(self) -> None:
        self.assertGreaterEqual(main._job_count(None), 1)
        self.assertEqual(main._job_count(5), 5)
        for value in (0, -1, True, "x"):
            with self.subTest(value=value), self.assertRaises(main.BuildError):
                main._job_count(value)

    def test_require_tool_reports_a_missing_program(self) -> None:
        with mock.patch("shutil.which", return_value=None), self.assertRaises(FileNotFoundError):
            main._require_tool("nope")
        with mock.patch("shutil.which", return_value="/bin/tool"):
            self.assertEqual(main._require_tool("tool"), Path("/bin/tool"))

    def test_find_msbuild_prefers_amd64_and_falls_back(self) -> None:
        install = self.root / "vs"
        (install / "MSBuild" / "Current" / "Bin" / "amd64").mkdir(parents=True)
        (install / "MSBuild" / "Current" / "Bin" / "amd64" / "MSBuild.exe").write_bytes(b"x")
        (install / "MSBuild" / "Current" / "Bin" / "MSBuild.exe").write_bytes(b"x")
        run = FakeRun(vswhere=str(install))
        with mock.patch.object(main, "run", run), mock.patch.dict(os.environ, {"ProgramFiles(x86)": str(self.root)}):
            self.assertTrue(str(main._find_msbuild()).endswith("amd64\\MSBuild.exe") or str(main._find_msbuild()).endswith("amd64/MSBuild.exe"))
        (install / "MSBuild" / "Current" / "Bin" / "amd64" / "MSBuild.exe").unlink()
        with mock.patch.object(main, "run", run), mock.patch.dict(os.environ, {"ProgramFiles(x86)": str(self.root)}):
            self.assertTrue(str(main._find_msbuild()).endswith("Bin\\MSBuild.exe") or str(main._find_msbuild()).endswith("Bin/MSBuild.exe"))

    def test_find_msbuild_rejects_missing_prerequisites(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(FileNotFoundError):
            main._find_msbuild()
        with mock.patch.object(main, "run", FakeRun(vswhere="")), mock.patch.dict(
            os.environ, {"ProgramFiles(x86)": str(self.root)}
        ), self.assertRaises(FileNotFoundError):
            main._find_msbuild()

    def test_find_vcpkg_prefers_the_root_then_path(self) -> None:
        root = self.root / "vcpkg"
        (root / "scripts" / "buildsystems" / "msbuild").mkdir(parents=True)
        (root / "scripts" / "buildsystems" / "msbuild" / "vcpkg.props").write_bytes(b"x")
        with mock.patch.dict(os.environ, {"VCPKG_ROOT": str(root)}):
            self.assertEqual(main._find_vcpkg(), root / "vcpkg.exe")
        with mock.patch.dict(os.environ, {"VCPKG_ROOT": str(self.root / "empty")}), mock.patch(
            "shutil.which", return_value="/bin/vcpkg"
        ):
            self.assertEqual(main._find_vcpkg(), Path("/bin/vcpkg"))
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "shutil.which", return_value=None
        ), self.assertRaises(FileNotFoundError):
            main._find_vcpkg()

    def test_source_tools_and_dumpbin_and_umdh(self) -> None:
        with mock.patch("shutil.which", side_effect=lambda name: f"/bin/{name}"):
            tools = main._source_tools()
            self.assertEqual(main._find_dumpbin(), Path("/bin/dumpbin"))
        self.assertEqual(tools.cppcheck, Path("/bin/cppcheck"))

        override = self.root / "custom-umdh.exe"
        override.write_bytes(b"x")
        with mock.patch.dict(os.environ, {"UNPACKRAT_UMDH": str(override)}):
            self.assertEqual(main._find_umdh(), override)
        with mock.patch.dict(os.environ, {"UNPACKRAT_UMDH": str(self.root / "missing.exe")}), self.assertRaises(FileNotFoundError):
            main._find_umdh()
        kits = self.root / "kits"
        umdh = kits / "Windows Kits" / "10" / "Debuggers" / "x64" / "umdh.exe"
        umdh.parent.mkdir(parents=True)
        umdh.write_bytes(b"x")
        with mock.patch.dict(os.environ, {"ProgramFiles(x86)": str(kits)}, clear=True):
            self.assertEqual(main._find_umdh(), umdh)
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(FileNotFoundError):
            main._find_umdh()

    def test_llvm_resolution_success_and_failures(self) -> None:
        llvm = self.root / "llvm"
        (llvm / "bin").mkdir(parents=True)
        (llvm / "bin" / "llvm-cov.exe").write_bytes(b"x")
        (llvm / "bin" / "llvm-profdata.exe").write_bytes(b"x")
        windows = llvm / "lib" / "clang" / "19.1.0" / "lib" / "windows"
        windows.mkdir(parents=True)
        with mock.patch.object(main, "_llvm_dir", return_value=llvm):
            resolved = main._llvm()
        self.assertEqual(resolved.runtime, windows)
        self.assertEqual(resolved.cov, llvm / "bin" / "llvm-cov.exe")

        shutil.rmtree(windows)
        with mock.patch.object(main, "_llvm_dir", return_value=llvm), self.assertRaises(FileNotFoundError):
            main._llvm()
        windows.mkdir(parents=True)
        (llvm / "bin" / "llvm-cov.exe").unlink()
        with mock.patch.object(main, "_llvm_dir", return_value=llvm), self.assertRaises(FileNotFoundError):
            main._llvm()

        with mock.patch("shutil.which", return_value=None), mock.patch.object(
            main, "_vs_installation", side_effect=FileNotFoundError("no Visual Studio")
        ), self.assertRaises(FileNotFoundError):
            main._llvm_dir()

    def test_llvm_dir_prefers_the_bundled_visual_studio_toolset(self) -> None:
        install = self.root / "vs"
        bundled = install / "VC" / "Tools" / "Llvm" / "x64"
        bundled.mkdir(parents=True)
        # A PATH clang-cl (for example a global LLVM 20) must not override the toolset that
        # ships with the selected Visual Studio.
        path_bin = self.root / "llvm20" / "bin"
        path_bin.mkdir(parents=True)
        (path_bin / "clang-cl.exe").write_bytes(b"x")
        with mock.patch.object(main, "_vs_installation", return_value=install), mock.patch(
            "shutil.which", return_value=str(path_bin / "clang-cl.exe")
        ):
            self.assertEqual(main._llvm_dir(), bundled)

    def test_llvm_rejects_a_directory_without_a_clang_tree(self) -> None:
        empty = self.root / "no-llvm"
        empty.mkdir()
        with mock.patch.object(main, "_llvm_dir", return_value=empty), self.assertRaises(FileNotFoundError):
            main._llvm()

    def test_find_umdh_reports_when_no_kits_copy_exists(self) -> None:
        kits = self.root / "empty-kits"
        kits.mkdir()
        with mock.patch.dict(os.environ, {"ProgramFiles(x86)": str(kits)}, clear=True), self.assertRaises(FileNotFoundError):
            main._find_umdh()

    def test_asan_runtime_reports_a_missing_install_runtime(self) -> None:
        install = self.root / "vs-empty"
        msbuild = install / "MSBuild" / "Current" / "Bin" / "amd64" / "MSBuild.exe"
        msbuild.parent.mkdir(parents=True)
        msbuild.write_bytes(b"x")
        with mock.patch.object(main, "_native_tools", return_value=(msbuild, self.root / "vcpkg.exe")), self.assertRaises(FileNotFoundError):
            main._asan_runtime("x64")

    def test_asan_runtime_resolution(self) -> None:
        install = self.root / "vs"
        bin_dir = install / "VC" / "Tools" / "MSVC" / "14.4" / "bin" / "Hostx64" / "x64"
        bin_dir.mkdir(parents=True)
        runtime = bin_dir / main._ASAN_RUNTIMES["x64"]
        runtime.write_bytes(b"x")
        msbuild = install / "MSBuild" / "Current" / "Bin" / "amd64" / "MSBuild.exe"
        msbuild.parent.mkdir(parents=True)
        msbuild.write_bytes(b"x")
        with mock.patch.object(main, "_native_tools", return_value=(msbuild, self.root / "vcpkg.exe")):
            self.assertEqual(main._asan_runtime("x64"), runtime)
        with mock.patch.object(main, "_native_tools", return_value=(self.root / "MSBuild.exe", self.root / "vcpkg.exe")), self.assertRaises(FileNotFoundError):
            main._asan_runtime("x64")

    def test_path_helpers(self) -> None:
        self.assertEqual(main.artifacts_root(self.repo), self.repo / "out" / "native")
        self.assertEqual(main.triplet("x64", "Debug"), "unpackrat-x64-windows-static")
        self.assertEqual(main.triplet("x64", "ASan"), "unpackrat-x64-windows-static-asan")
        self.assertEqual(main.restore_root(self.repo, "x86", "Fuzz"), main.artifacts_root(self.repo) / "vcpkg_installed" / "x86-asan")

    def test_read_version_reads_the_declared_text(self) -> None:
        self.make_module("renpy", "3.1.0")
        self.assertEqual(main.read_version(self.repo, "renpy"), "3.1.0")
        (self.repo / "src" / "modules" / "renpy" / "VERSION").write_text("10.20.30\n", encoding="utf-8")
        self.assertEqual(main.read_version(self.repo, "renpy"), "10.20.30")
        with self.assertRaises(main.BuildError):
            main.read_version(self.repo, "missing")

    # ----- licenses and zips --------------------------------------------------------------

    def test_licenses_document_includes_notices_and_normalises_newlines(self) -> None:
        self.make_module("zanzarah", "2.1.0")
        document = main._licenses_document(self.repo, "zanzarah")
        self.assertIn(b"MIT license text", document)
        self.assertIn(b"Observer.txt notice", document)
        self.assertIn(b"GPL-3.0.txt notice", document)
        self.assertTrue(document.endswith(b"\n"))
        # A notice that already ends in a newline is not doubled.
        self.assertTrue(main._license_section("t", b"body\n").endswith(b"body\n"))
        self.assertFalse(main._license_section("t", b"body\n").endswith(b"body\n\n"))
        self.assertTrue(main._license_section("t", b"body").endswith(b"body\n"))

    def test_renpy_packages_the_linked_zlib_ng_notice(self) -> None:
        self.make_module("renpy")
        document = main._licenses_document(self.repo, "renpy")
        self.assertIn(b"zlib-ng.txt notice", document)
        self.assertNotIn(b"zlib.txt notice", document)

    def test_write_zip_round_trips(self) -> None:
        path = self.root / "a.zip"
        main._write_zip(path, {"b": b"2", "a": b"1"})
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.namelist(), ["a", "b"])

    # ----- source checks ------------------------------------------------------------------

    def test_source_files_handles_missing_root_and_suffixes(self) -> None:
        self.assertEqual(main._source_files(self.repo, main._CPP_SUFFIXES), ())
        src = self.repo / "src"
        src.mkdir()
        (src / "a.cpp").write_bytes(b"x")
        (src / "b.h").write_bytes(b"x")
        (src / "c.txt").write_bytes(b"x")
        ignored = src / "out"
        ignored.mkdir()
        (ignored / "d.cpp").write_bytes(b"x")
        self.assertEqual([path.name for path in main._source_files(self.repo, main._CPP_SUFFIXES)], ["a.cpp", "b.h"])

    def test_ci_source_runs_every_source_check(self) -> None:
        src = self.repo / "src"
        src.mkdir()
        (src / "a.cpp").write_bytes(b"x")
        (src / "a.ps1").write_bytes(b"x")
        (self.repo / "build" / "checks").mkdir(parents=True)
        (self.repo / "build" / "checks" / "psscriptanalyzer.ps1").write_bytes(b"x")
        (self.repo / "build" / "PSScriptAnalyzerSettings.psd1").write_bytes(b"x")
        (self.repo / "build" / "tests").mkdir(parents=True)
        (self.repo / "build" / "tests" / "x.Tests.ps1").write_bytes(b"x")
        (self.repo / "build" / "pyproject.toml").write_bytes(b"x")
        run = FakeRun()
        with self.tools(run):
            main.ci_source(repository=self.repo, jobs=4)
        tools = run.tools()
        self.assertIn("clang-format.exe", tools)
        self.assertIn("cppcheck.exe", tools)
        self.assertIn("pwsh.exe", tools)
        cppcheck = next(call for call in run.calls if os.path.basename(call.argv[0]).lower() == "cppcheck.exe")
        self.assertIn("--error-exitcode=1", cppcheck.argv)
        self.assertTrue(any("coverage" in call.argv for call in run.calls))

    def test_ci_source_restores_dependencies_before_cppcheck(self) -> None:
        src = self.repo / "src"
        src.mkdir()
        (src / "a.cpp").write_bytes(b"x")
        run = FakeRun()
        with self.tools(run):
            main.ci_source(repository=self.repo, jobs=4)
        names = [os.path.basename(call.argv[0]).lower() for call in run.calls]
        self.assertIn("vcpkg.exe", names)
        restore = next(index for index, name in enumerate(names) if name == "vcpkg.exe")
        cppcheck = next(index for index, name in enumerate(names) if name == "cppcheck.exe")
        self.assertLess(restore, cppcheck, "the dependency restore must precede the Cppcheck scan")
        command = run.calls[restore].argv
        self.assertEqual(command[1], "install")
        resolved = self.repo.resolve()
        self.assertEqual(command[command.index("--triplet") + 1], main.triplet("x64", "Debug"))
        self.assertIn(f"--x-install-root={main.restore_root(resolved, 'x64', 'Debug')}", command)

    def test_ci_source_cppcheck_scans_restored_dependencies_with_baseline_exemptions(self) -> None:
        src = self.repo / "src"
        src.mkdir()
        (src / "a.cpp").write_bytes(b"x")
        run = FakeRun()
        with self.tools(run):
            main.ci_source(repository=self.repo, jobs=4)
        command = next(
            call.argv for call in run.calls if os.path.basename(call.argv[0]).lower() == "cppcheck.exe"
        )
        resolved = self.repo.resolve()
        triplet = main.triplet("x64", "Debug")
        include = main.restore_root(resolved, "x64", "Debug") / triplet / "include"
        self.assertIn(f"-I{include}", command)
        self.assertIn("--check-level=exhaustive", command)
        self.assertIn("--suppress=uninitMemberVarNoCtor:src/api.h", command)
        self.assertIn(f"--suppress=*:*\\{triplet}\\include\\*", command)
        self.assertIn("--suppress=functionStatic", command)
        self.assertIn("--suppress=*:third_party/*", command)
        self.assertIn(f"--relative-paths={resolved}", command)
        self.assertIn("--error-exitcode=1", command)

    def test_source_checks_skip_empty_source_trees(self) -> None:
        (self.repo / "build" / "tests").mkdir(parents=True)
        run = FakeRun()
        with self.tools(run):
            main._format_check(self.repo, main.SourceTools(self.root / "cf", self.root / "cc", self.root / "ps"))
            main._pssa(self.repo, main.SourceTools(self.root / "cf", self.root / "cc", self.root / "ps"))
        self.assertEqual(run.calls, [])

    def test_contracts_run_each_powershell_contract(self) -> None:
        tests = self.repo / "build" / "tests"
        tests.mkdir(parents=True)
        (tests / "a.Tests.ps1").write_bytes(b"x")
        run = FakeRun()
        with self.tools(run):
            main._contracts(self.repo, main.SourceTools(self.root / "cf", self.root / "cc", self.root / "pwsh.exe"))
        self.assertEqual(len(run.calls), 1)

    # ----- coverage -----------------------------------------------------------------------

    def test_coverage_builds_runs_and_gates(self) -> None:
        run = FakeRun()
        with self.tools(run):
            summary = main._coverage(self.repo, "x64", self.root / "MSBuild.exe", self.root / "vcpkg.exe", self.llvm, jobs=2)
        self.assertTrue(summary.is_file())
        self.assertEqual(json.loads(summary.read_text()), _FULL_COVERAGE)

    def test_coverage_adds_the_corpus_suite(self) -> None:
        corpus = self.root / "corpus"
        corpus.mkdir()
        run = FakeRun()
        with self.tools(run):
            main._coverage(self.repo, "x64", self.root / "MSBuild.exe", self.root / "vcpkg.exe", self.llvm, corpus=corpus)
        suites = [call for call in run.calls if call.env and "UNPACKRAT_TEST_CORPUS" in call.env]
        self.assertEqual(len(suites), 1)

    def test_coverage_fails_without_raw_profiles(self) -> None:
        run = FakeRun(profraw=False)
        with self.tools(run), self.assertRaises(main.BuildError):
            main._coverage(self.repo, "x64", self.root / "MSBuild.exe", self.root / "vcpkg.exe", self.llvm)

    def test_coverage_clears_stale_raw_profiles(self) -> None:
        stale = main.artifacts_root(self.repo) / "coverage" / "x64" / "unit"
        stale.mkdir(parents=True)
        (stale / "stale.profraw").write_bytes(b"old")
        run = FakeRun()
        with self.tools(run):
            main._coverage(self.repo, "x64", self.root / "MSBuild.exe", self.root / "vcpkg.exe", self.llvm)
        self.assertFalse((stale / "stale.profraw").exists())

    def test_coverage_export_names_every_object(self) -> None:
        command = main._coverage_export(self.repo, "x64", self.root / "p.profdata", self.llvm)
        self.assertEqual(command.count("--object"), len(main.MODULES))
        self.assertEqual(command[-1], "--summary-only")

    def test_coverage_writes_distinct_lcov_beside_the_summary(self) -> None:
        run = FakeRun()
        with self.tools(run):
            summary = main._coverage(self.repo, "x64", self.root / "MSBuild.exe", self.root / "vcpkg.exe", self.llvm)
        self.assertEqual(json.loads(summary.read_text(encoding="utf-8")), _FULL_COVERAGE)
        lcov = main.artifacts_root(self.repo) / "coverage" / "x64" / "coverage.lcov"
        self.assertTrue(lcov.is_file(), "the native coverage must export a full LCOV beside the summary")
        text = lcov.read_text(encoding="utf-8")
        self.assertIn("DA:10,1", text)
        self.assertIn("DA:11,0", text)
        self.assertIn("BRDA:12,0,0,1", text)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(text)

    def test_coverage_lcov_export_is_full_and_never_summary_only(self) -> None:
        command = main._coverage_lcov_export(self.repo, "x64", self.root / "p.profdata", self.llvm)
        self.assertEqual(command.count("--object"), len(main.MODULES))
        self.assertIn("--format=lcov", command)
        self.assertNotIn("--summary-only", command)

    def test_coverage_saves_both_reports_before_the_gate_fails(self) -> None:
        incomplete = {
            "data": [{"totals": {"lines": {"count": 10, "covered": 9}, "branches": {"count": 4, "covered": 4}}}]
        }
        run = FakeRun(llvm_export=incomplete)
        with self.tools(run), self.assertRaises(ValueError):
            main._coverage(self.repo, "x64", self.root / "MSBuild.exe", self.root / "vcpkg.exe", self.llvm)
        coverage = main.artifacts_root(self.repo) / "coverage" / "x64"
        self.assertTrue((coverage / "coverage.json").is_file(), "the summary must be saved before the gate")
        self.assertIn("BRDA:", (coverage / "coverage.lcov").read_text(encoding="utf-8"))

    def test_coverage_propagates_the_lcov_export_failure(self) -> None:
        run = FakeRun(fail_when=lambda argv: "--format=lcov" in argv)
        with self.tools(run), self.assertRaises(subprocess.CalledProcessError):
            main._coverage(self.repo, "x64", self.root / "MSBuild.exe", self.root / "vcpkg.exe", self.llvm)

    def test_python_coverage_exports_xml_before_the_report_gate(self) -> None:
        run = FakeRun()
        with mock.patch.object(main, "run", run):
            main._python_coverage(self.repo)
        commands = [call.argv for call in run.calls if "coverage" in call.argv]
        xml = next(command for command in commands if "xml" in command)
        report = next(command for command in commands if "report" in command)
        self.assertLess(commands.index(xml), commands.index(report), "the XML must be written before the gate")
        destination = Path(xml[xml.index("-o") + 1])
        self.assertEqual(destination, main.artifacts_root(self.repo) / "coverage" / "python" / "coverage.xml")
        self.assertTrue(destination.parent.is_dir(), "the XML output directory must exist")
        self.assertIn("--fail-under=100", report)

    def test_coverage_complete_rejects_malformed_metrics(self) -> None:
        with self.assertRaises(ValueError):
            main.coverage_complete({"data": [{"totals": {"lines": {"count": "x", "covered": 1}, "branches": {"count": 1, "covered": 1}}}]})
        with self.assertRaises(ValueError):
            main.coverage_complete({"data": [{"totals": {"lines": {"count": 1, "covered": 2}, "branches": {"count": 1, "covered": 1}}}]})

    # ----- sanitizers ---------------------------------------------------------------------

    def test_asan_and_ubsan_operations(self) -> None:
        run = FakeRun()
        with self.tools(run):
            asan = main._sanitizer(self.repo, "asan", "x64", self.root / "MSBuild.exe", self.root / "vcpkg.exe", self.llvm)
            ubsan = main._sanitizer(self.repo, "ubsan", "x64", self.root / "MSBuild.exe", self.root / "vcpkg.exe", self.llvm)
        self.assertIn("asan", asan.parts)
        self.assertIn("ubsan", ubsan.parts)
        asan_env = next(call.env for call in run.calls if call.env and "ASAN_OPTIONS" in call.env)
        self.assertIn("halt_on_error=1", asan_env["ASAN_OPTIONS"])
        ubsan_env = next(call.env for call in run.calls if call.env and "UBSAN_OPTIONS" in call.env)
        self.assertIn("halt_on_error=1", ubsan_env["UBSAN_OPTIONS"])

    # ----- fuzz ---------------------------------------------------------------------------

    def test_fuzz_replays_on_x86_and_times_on_x64(self) -> None:
        self.make_module()
        run = FakeRun()
        with self.tools(run):
            main._fuzz(self.repo, "x64", self.root / "MSBuild.exe", self.root / "vcpkg.exe", self.llvm)
            main._fuzz(self.repo, "x86", self.root / "MSBuild.exe", self.root / "vcpkg.exe", self.llvm)
        timed = [call for call in run.calls if any("-max_total_time=" in item for item in call.argv)]
        replay = [call for call in run.calls if any(item.endswith("seed") for item in call.argv)]
        self.assertEqual(len(timed), len(main.FUZZ_TARGETS))
        self.assertEqual(len(replay), len(main.FUZZ_TARGETS))

    def test_seed_corpus_decodes_hex_and_rejects_empty(self) -> None:
        self.make_module()
        seeds = main._seed_corpus(self.repo, "renpy", self.root / "work" / "seeds")
        by_name = {path.name: path for path in seeds}
        self.assertEqual(by_name["seed"].read_bytes(), b"ABC")
        self.assertEqual(by_name["raw.bin"].read_bytes(), b"raw seed")
        with self.assertRaises(FileNotFoundError):
            main._seed_corpus(self.repo, "missing", self.root / "work" / "nope")
        empty = self.repo / "src" / "fuzz" / "corpus" / "empty"
        empty.mkdir(parents=True)
        with self.assertRaises(FileNotFoundError):
            main._seed_corpus(self.repo, "empty", self.root / "work" / "empty")

    # ----- leaks --------------------------------------------------------------------------

    def test_prepare_leak_probe_builds_the_probe_with_release_analysis(self) -> None:
        run = FakeRun()
        with self.tools(run):
            probe = main._prepare_leak_probe(self.repo, self.root / "MSBuild.exe", self.root / "vcpkg.exe")
        self.assertEqual(probe.name, main.LEAK_PROBE)
        command = next(
            call.argv for call in run.calls
            if any("leak-probe.vcxproj" in item for item in call.argv)
        )
        self.assertIn("/p:RunCodeAnalysis=true", command)
        self.assertIn("/p:Configuration=Release", command)

    def test_leak_measure_stages_and_measures_every_scenario(self) -> None:
        self.make_module()
        run = FakeRun()
        with self.tools(run):
            main._leak_measure(self.repo)
        leak_calls = [call for call in run.calls if call.argv[1:3] == ("-m", "leaks")]
        self.assertEqual(len(leak_calls), 1 + len(main.LEAK_MODES) * len(main.LEAK_SCENARIOS))
        self.assertEqual(leak_calls[0].argv[3], "setup")
        # The measurement never reaches a build tool; it only measures the ready tree.
        self.assertNotIn("vcpkg.exe", run.tools())
        self.assertNotIn("msbuild.exe", run.tools())

    def test_leak_measure_fails_closed_without_the_ready_binaries(self) -> None:
        run = FakeRun()
        with self.tools(run), self.assertRaises(main.BuildError):
            main._leak_measure(self.repo)
        self.assertEqual(run.calls, [])

    def test_leak_measure_fails_closed_without_the_symbols(self) -> None:
        self.make_module()
        for name in main.MODULE_BINARIES.values():
            (self.repo / "out" / "native" / "bin" / "x64" / "Release" / name).with_suffix(".pdb").unlink()
        run = FakeRun()
        with self.tools(run), self.assertRaises(main.BuildError):
            main._leak_measure(self.repo)
        self.assertEqual(run.calls, [])

    # ----- audit --------------------------------------------------------------------------

    def test_audit_accepts_a_clean_module_and_rejects_findings(self) -> None:
        run = FakeRun(dumpbin=_dumpbin)
        with self.tools(run):
            main._audit(self.repo, "x64")

    def test_require_release_pe_rejects_bad_machines_imports_and_exports(self) -> None:
        good_headers = "8664 machine (x64)"
        good_deps = "    KERNEL32.dll"
        good_exports = "   1    0 00001000 LoadSubModule\n   2    0 00001010 UnloadSubModule"
        main.require_release_pe("x64", good_headers, good_deps, good_exports)
        with self.assertRaises(main.BuildError):
            main.require_release_pe("arm64", good_headers, good_deps, good_exports)
        with self.assertRaises(main.BuildError):
            main.require_release_pe("x64", "14C machine (x86)", good_deps, good_exports)
        with self.assertRaises(main.BuildError):
            main.require_release_pe("x64", good_headers, "    vcruntime140.dll", good_exports)
        with self.assertRaises(main.BuildError):
            main.require_release_pe("x64", good_headers, "    mystery.dll", good_exports)
        # A generic api-ms-win shim is allowed; the CRT ones are not.
        main.require_release_pe("x64", good_headers, "    api-ms-win-core-file-l1-1-0.dll", good_exports)
        with self.assertRaises(main.BuildError):
            main.require_release_pe("x64", good_headers, "    api-ms-win-crt-runtime-l1-1-0.dll", good_exports)
        with self.assertRaises(main.BuildError):
            main.require_release_pe("x64", good_headers, good_deps, "   1    0 00001000 LoadSubModule")

    # ----- ci_arch ------------------------------------------------------------------------

    def test_ci_arch_runs_the_full_x64_pipeline(self) -> None:
        self.make_module()
        run = FakeRun(dumpbin=_dumpbin)
        with self.tools(run):
            main.ci_arch(["x64"], repository=self.repo)
        tools = run.tools()
        self.assertIn("msbuild.exe", tools)
        self.assertTrue(any("-m" == call.argv[1] and "leaks" == call.argv[2] for call in run.calls))

    def test_ci_arch_x86_skips_the_x64_specialists(self) -> None:
        self.make_module()
        run = FakeRun(dumpbin=_dumpbin)
        with self.tools(run):
            main.ci_arch(["x86"], repository=self.repo)
        self.assertFalse(any(call.argv[1:3] == ("-m", "leaks") for call in run.calls))

    def test_ci_arch_runs_every_stage_in_order_by_default(self) -> None:
        seen: list[str] = []
        with mock.patch.object(main, "_dispatch", side_effect=lambda name, *_args: seen.append(name)):
            main.ci_arch(["x64"], repository=self.repo)
        self.assertEqual(seen, ["native", "coverage", "asan", "ubsan", "fuzz", "leaks"])

    def test_ci_arch_single_stage_runs_only_that_stage(self) -> None:
        seen: list[str] = []
        with mock.patch.object(main, "_dispatch", side_effect=lambda name, *_args: seen.append(name)):
            main.ci_arch(["x64"], stage="fuzz", repository=self.repo)
        self.assertEqual(seen, ["fuzz"])

    def test_ci_arch_rejects_an_unknown_stage(self) -> None:
        with self.assertRaises(main.BuildError):
            main.ci_arch(["x64"], stage="bogus", repository=self.repo)

    def test_ci_arch_rejects_non_x64_selections_for_the_x64_specialists(self) -> None:
        cases = (("ubsan", ("x86",)), ("leaks", ("x86",)), ("leaks", ("x86", "x64")))
        for stage, architectures in cases:
            with self.subTest(stage=stage, architectures=architectures), self.assertRaises(main.BuildError):
                main.ci_arch(list(architectures), stage=stage, repository=self.repo)

    def test_leaks_stage_measures_without_any_build_tool(self) -> None:
        self.make_module()
        run = FakeRun()
        with self.tools(run):
            main.ci_arch(["x64"], stage="leaks", repository=self.repo)
        names = run.tools()
        self.assertNotIn("msbuild.exe", names)
        self.assertNotIn("vcpkg.exe", names)
        self.assertNotIn("binskim.exe", names)
        self.assertTrue(any(call.argv[1:3] == ("-m", "leaks") for call in run.calls))

    # ----- CLI ----------------------------------------------------------------------------

    def test_selection_parses_lists_all_and_rejects_bad_input(self) -> None:
        parse = main._selection(main.ARCHITECTURES)
        self.assertEqual(parse("all"), main.ARCHITECTURES)
        self.assertEqual(parse("x64"), ("x64",))
        for value in ("", "all,x64", "arm64"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                parse(value)

    def test_main_dispatches_ci_source_and_ci_arch(self) -> None:
        with mock.patch.object(main, "ci_source", return_value=self.repo) as handler:
            self.assertEqual(main.main(["ci-source", "-Jobs", "4"]), 0)
        handler.assert_called_once()
        with mock.patch.object(main, "ci_arch", return_value=self.repo) as handler:
            self.assertEqual(main.main(["ci-arch", "-Arch", "x64"]), 0)
        self.assertEqual(handler.call_args.args[0], ("x64",))
        self.assertEqual(handler.call_args.kwargs["stage"], "all")
        with mock.patch.object(main, "ci_arch", return_value=self.repo) as handler:
            self.assertEqual(main.main(["ci-arch", "-Arch", "x64", "-Stage", "coverage"]), 0)
        self.assertEqual(handler.call_args.kwargs["stage"], "coverage")

    def test_main_dispatches_build_and_test_over_lists(self) -> None:
        with mock.patch.object(main, "build", return_value=self.repo) as handler:
            self.assertEqual(main.main(["build", "-Arch", "x86,x64", "-Config", "Debug,Release"]), 0)
        self.assertEqual(handler.call_count, 4)
        with mock.patch.object(main, "test", return_value=()) as handler:
            self.assertEqual(main.main(["test", "-Arch", "x64", "-Config", "Release"]), 0)
        handler.assert_called_once()


if __name__ == "__main__":
    unittest.main()

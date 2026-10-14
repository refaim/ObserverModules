"""Behavioral contract for architecture-specific LLVM instrumentation-toolchain resolution.

The instrumentation stages (coverage, ASan, UBSan, fuzz) need one matched LLVM 19.1.5 family:
the compiler, the coverage and profile tools, the headers and the per-architecture runtime
libraries.  A hosted image's globally installed LLVM on ``PATH`` has no 32-bit ``profile-i386``
runtime, so the driver must never borrow it.  Two selectors exist: the explicit, absolute
``UNPACKRAT_LLVM_INSTRUMENTATION`` root the CI prep job publishes, or, locally, the LLVM 19.1.5
bundled with the Visual Studio 2022 installation.  For both, the x64 runtime comes from the
``x64`` home beside the compiler and the x86 runtime from the sibling ``Llvm`` root, and the
driver validates every tool version, the headers and every required runtime library before it
forwards anything to MSBuild.

``ci_arch`` must hand each architecture its own resolved ``Llvm`` object so a single 64-bit
runtime is never reused for both architectures.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import main


VERSION = "19"
X64_RUNTIMES = (
    "clang_rt.profile-x86_64.lib",
    "clang_rt.fuzzer-x86_64.lib",
    "clang_rt.asan-x86_64.lib",
    "clang_rt.asan_cxx-x86_64.lib",
    "clang_rt.ubsan_standalone-x86_64.lib",
    "clang_rt.ubsan_standalone_cxx-x86_64.lib",
)
I386_RUNTIMES = (
    "clang_rt.profile-i386.lib",
    "clang_rt.fuzzer-i386.lib",
    "clang_rt.asan-i386.lib",
    "clang_rt.asan_cxx-i386.lib",
)


def _tool_runner(version: str = "19.1.5"):
    """A ``main.run`` stand-in that reports one natural ``--version`` response."""

    def runner(argv, **options: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 0, f"LLVM version {version}\n")

    return runner


def seed_home(home: Path, runtimes: tuple[str, ...]) -> None:
    """Create one LLVM ``x64`` home: the three tools, the headers and the runtime libraries."""

    (home / "bin").mkdir(parents=True, exist_ok=True)
    for tool in ("clang-cl", "llvm-cov", "llvm-profdata"):
        (home / "bin" / f"{tool}.exe").write_bytes(b"x")
    include = home / "lib" / "clang" / VERSION / "include"
    include.mkdir(parents=True, exist_ok=True)
    (include / "stddef.h").write_bytes(b"x")
    windows = home / "lib" / "clang" / VERSION / "lib" / "windows"
    windows.mkdir(parents=True, exist_ok=True)
    for library in runtimes:
        (windows / library).write_bytes(b"x")


def seed_sibling(install: Path, runtimes: tuple[str, ...] = I386_RUNTIMES) -> Path:
    """Create the sibling ``VC/Tools/Llvm`` root that carries the 32-bit runtime only."""

    windows = install / "VC" / "Tools" / "Llvm" / "lib" / "clang" / VERSION / "lib" / "windows"
    windows.mkdir(parents=True, exist_ok=True)
    for library in runtimes:
        (windows / library).write_bytes(b"x")
    return windows


class LlvmArchitectureResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.install = self.root / "vs"

    def test_bundled_visual_studio_wins_over_a_path_llvm_for_both_architectures(self) -> None:
        x64 = self.install / "VC" / "Tools" / "Llvm" / "x64"
        seed_home(x64, X64_RUNTIMES)
        sibling = seed_sibling(self.install)

        # A global LLVM 20 on PATH without any 32-bit compiler-rt must not be selected.
        path_bin = self.root / "llvm20" / "bin"
        path_bin.mkdir(parents=True)
        (path_bin / "clang-cl.exe").write_bytes(b"x")
        (self.root / "llvm20" / "lib" / "clang" / "20" / "lib" / "windows").mkdir(parents=True)

        with mock.patch.object(main, "_llvm_installation", return_value=self.install), mock.patch(
            "shutil.which", return_value=str(path_bin / "clang-cl.exe")
        ), mock.patch.object(main, "run", side_effect=_tool_runner()):
            x64_llvm = main._llvm("x64")
            x86_llvm = main._llvm("x86")

        self.assertEqual(x64_llvm.directory, x64)
        self.assertEqual(x64_llvm.runtime, x64 / "lib" / "clang" / VERSION / "lib" / "windows")
        self.assertEqual(x64_llvm.cov, x64 / "bin" / "llvm-cov.exe")
        self.assertEqual(x64_llvm.profdata, x64 / "bin" / "llvm-profdata.exe")

        # The compiler and coverage tools are shared; only the runtime differs by architecture.
        self.assertEqual(x86_llvm.directory, x64)
        self.assertEqual(x86_llvm.cov, x64 / "bin" / "llvm-cov.exe")
        self.assertEqual(x86_llvm.profdata, x64 / "bin" / "llvm-profdata.exe")
        self.assertEqual(x86_llvm.runtime, sibling)
        self.assertNotEqual(x86_llvm.runtime, x64_llvm.runtime)

        # The forwarded tool-set version stays consistent: it is the bundled version for both.
        self.assertEqual(x86_llvm.runtime.parent.parent.name, x64_llvm.runtime.parent.parent.name)

    def test_x86_runtime_requires_the_sibling_root(self) -> None:
        x64 = self.install / "VC" / "Tools" / "Llvm" / "x64"
        seed_home(x64, X64_RUNTIMES)

        with mock.patch.object(main, "_llvm_installation", return_value=self.install), mock.patch.object(
            main, "run", side_effect=_tool_runner()
        ):
            # The x64 root alone cannot serve the 32-bit runtime.
            self.assertFalse((self.install / "VC" / "Tools" / "Llvm" / "lib" / "clang").exists())
            with self.assertRaises(FileNotFoundError):
                main._llvm("x86")
            # The compiler directory still resolves for its own architecture.
            self.assertEqual(main._llvm("x64").directory, x64)

        runtime = seed_sibling(self.install)
        with mock.patch.object(main, "_llvm_installation", return_value=self.install), mock.patch.object(
            main, "run", side_effect=_tool_runner()
        ):
            self.assertEqual(main._llvm("x86").runtime, runtime)

    def test_the_load_bearing_tool_versions_headers_and_runtimes_are_all_required(self) -> None:
        x64 = self.install / "VC" / "Tools" / "Llvm" / "x64"
        seed_home(x64, X64_RUNTIMES)
        seed_sibling(self.install)

        def resolve():
            with mock.patch.object(main, "_llvm_installation", return_value=self.install), mock.patch.object(
                main, "run", side_effect=_tool_runner()
            ):
                return main._llvm("x64")

        self.assertEqual(resolve().runtime.name, "windows")
        # A tool that reports a different version must fail closed rather than mix versions.
        with mock.patch.object(main, "_llvm_installation", return_value=self.install), mock.patch.object(
            main, "run", side_effect=_tool_runner("19.1.6")
        ), self.assertRaises(main.BuildError):
            main._llvm("x64")
        # A missing header breaks the same contract.
        (x64 / "lib" / "clang" / VERSION / "include" / "stddef.h").unlink()
        with self.assertRaises(FileNotFoundError):
            resolve()
        (x64 / "lib" / "clang" / VERSION / "include" / "stddef.h").write_bytes(b"x")
        # A missing runtime library breaks it too.
        (x64 / "lib" / "clang" / VERSION / "lib" / "windows" / X64_RUNTIMES[0]).unlink()
        with self.assertRaises(FileNotFoundError):
            resolve()


class LlvmInstrumentationSelectorTests(unittest.TestCase):
    """The explicit env root and the local Visual Studio 2022 fallback."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _seed_root(self, root: Path) -> None:
        seed_home(root / "x64", X64_RUNTIMES)
        sibling = root / "lib" / "clang" / VERSION / "lib" / "windows"
        sibling.mkdir(parents=True, exist_ok=True)
        for library in I386_RUNTIMES:
            (sibling / library).write_bytes(b"x")

    def test_the_env_root_selects_its_x64_home_without_consulting_visual_studio(self) -> None:
        root = self.root / "instrumentation"
        self._seed_root(root)
        with mock.patch.dict(
            os.environ, {"UNPACKRAT_LLVM_INSTRUMENTATION": str(root)}, clear=True
        ), mock.patch.object(
            main, "_llvm_installation", side_effect=AssertionError("must not consult Visual Studio")
        ), mock.patch.object(main, "run", side_effect=_tool_runner()):
            self.assertEqual(main._llvm_dir(), root / "x64")
            self.assertEqual(
                main._llvm("x64").runtime, root / "x64" / "lib" / "clang" / VERSION / "lib" / "windows"
            )
            self.assertEqual(
                main._llvm("x86").runtime, root / "lib" / "clang" / VERSION / "lib" / "windows"
            )

    def test_a_relative_env_root_fails_closed(self) -> None:
        with mock.patch.dict(
            os.environ, {"UNPACKRAT_LLVM_INSTRUMENTATION": "relative/tools/llvm"}, clear=True
        ), self.assertRaises(FileNotFoundError):
            main._llvm_dir()

    def test_default_selects_the_visual_studio_2022_llvm_installation(self) -> None:
        install = self.root / "vs2022"
        with mock.patch.object(main, "_llvm_installation", return_value=install), mock.patch.dict(
            os.environ, {}, clear=True
        ):
            self.assertEqual(main._llvm_dir(), install / "VC" / "Tools" / "Llvm" / "x64")

    def test_default_fails_closed_when_no_installed_llvm_is_found(self) -> None:
        with mock.patch.object(
            main, "_llvm_installation", side_effect=FileNotFoundError("no LLVM")
        ), mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(FileNotFoundError):
            main._llvm_dir()


class LlvmInstallationLookupTests(unittest.TestCase):
    def test_vswhere_requires_the_visual_studio_2022_llvm_component(self) -> None:
        with mock.patch.dict(os.environ, {"ProgramFiles(x86)": r"C:\PF86"}, clear=True), \
             mock.patch.object(
                 main, "run",
                 return_value=subprocess.CompletedProcess([], 0, r"C:\VS2022"),
             ) as invoked:
            installation = main._llvm_installation()

        self.assertEqual(installation, Path(r"C:\VS2022"))
        argv = invoked.call_args.args[0]
        self.assertIn("-version", argv)
        self.assertIn("[17.0,18.0)", argv)
        self.assertIn("Microsoft.VisualStudio.Component.VC.Llvm.Clang", argv)
        self.assertEqual(argv[argv.index("-property") + 1], "installationPath")

    def test_missing_program_files_or_no_installation_fails_closed(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(FileNotFoundError):
            main._llvm_installation()
        with mock.patch.dict(os.environ, {"ProgramFiles(x86)": r"C:\PF86"}, clear=True), \
             mock.patch.object(main, "run", return_value=subprocess.CompletedProcess([], 0, "")), \
             self.assertRaises(FileNotFoundError):
            main._llvm_installation()


class CiArchLlvmRoutingTests(unittest.TestCase):
    """``ci_arch`` must resolve and forward a distinct ``Llvm`` per architecture."""

    def test_ci_arch_hands_each_architecture_its_own_llvm_runtime(self) -> None:
        resolved: dict[str, mock.Mock] = {}

        def fake_llvm(architecture: str = "x64") -> mock.Mock:
            resolved[architecture] = mock.Mock(name=f"llvm-{architecture}")
            return resolved[architecture]

        coverage = mock.Mock()
        sanitizer = mock.Mock()
        fuzz = mock.Mock()
        with mock.patch.multiple(
            main,
            test=mock.Mock(return_value=()),
            _native_tools=mock.Mock(return_value=(Path("MSBuild.exe"), Path("vcpkg.exe"))),
            _llvm=mock.Mock(side_effect=fake_llvm),
            _coverage=coverage,
            _sanitizer=sanitizer,
            _fuzz=fuzz,
            _prepare_leak_probe=mock.Mock(),
            _leak_measure=mock.Mock(),
            _audit=mock.Mock(),
            _binskim=mock.Mock(),
        ):
            main.ci_arch(["x86", "x64"], repository=Path("."))

        self.assertEqual(sorted(resolved), ["x64", "x86"])

        coverage_by_arch = {call.args[1]: call.args[4] for call in coverage.call_args_list}
        self.assertIs(coverage_by_arch["x86"], resolved["x86"])
        self.assertIs(coverage_by_arch["x64"], resolved["x64"])

        fuzz_by_arch = {call.args[1]: call.args[4] for call in fuzz.call_args_list}
        self.assertIs(fuzz_by_arch["x86"], resolved["x86"])
        self.assertIs(fuzz_by_arch["x64"], resolved["x64"])

        # UBSan runs for x64 only and must use the bundled x64 runtime.
        ubsan = [call for call in sanitizer.call_args_list if call.args[1] == "ubsan"]
        self.assertEqual(len(ubsan), 1)
        self.assertIs(ubsan[0].args[5], resolved["x64"])

    def test_msvc_asan_stage_needs_no_llvm_while_ubsan_still_requires_it(self) -> None:
        # ASan is the MSVC 14.44 dynamic ASan build, not the LLVM 19 clang sanitizer, so an
        # unavailable LLVM instrumentation toolchain must not block the asan stage. UBSan still
        # depends on the matched LLVM 19 runtime and must fail closed without it.
        def unavailable(architecture: str = "x64") -> mock.Mock:
            raise FileNotFoundError("no LLVM 19.1.5 instrumentation toolchain")

        sanitizer = mock.Mock()
        with mock.patch.multiple(
            main,
            _native_tools=mock.Mock(return_value=(Path("MSBuild.exe"), Path("vcpkg.exe"))),
            _llvm=mock.Mock(side_effect=unavailable),
            _sanitizer=sanitizer,
        ):
            main._dispatch("asan", Path("."), ("x86", "x64"), None, None, {})

        asan = sanitizer.call_args_list
        self.assertEqual([call.args[1] for call in asan], ["asan", "asan"])
        # No architecture is handed a resolved LLVM toolchain for the MSVC ASan build.
        self.assertTrue(all(call.args[5] is None for call in asan))

        with mock.patch.multiple(
            main,
            _native_tools=mock.Mock(return_value=(Path("MSBuild.exe"), Path("vcpkg.exe"))),
            _llvm=mock.Mock(side_effect=unavailable),
            _sanitizer=mock.Mock(),
        ), self.assertRaises(FileNotFoundError):
            main._dispatch("ubsan", Path("."), ("x64",), None, None, {})


if __name__ == "__main__":
    unittest.main()

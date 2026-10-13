"""Behavioral contract for architecture-specific LLVM toolchain resolution.

Hosted CI images ship a globally installed LLVM (for example clang 20 on ``PATH``)
whose ``clang_rt`` directory has no 32-bit ``profile-i386`` runtime, so an x86
coverage or fuzz build fails to link.  The driver must therefore resolve every LLVM
tool from the LLVM bundled with the selected Visual Studio, and must take the x86
runtime from the sibling ``VC/Tools/Llvm`` root that ships ``profile-i386``, while
the x64 runtime comes from the ``VC/Tools/Llvm/x64`` root alongside the compiler.

``ci_arch`` must hand each architecture its own resolved ``Llvm`` object so a single
64-bit runtime is never reused for both architectures.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import main


class LlvmArchitectureResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.install = self.root / "vs"
        self.version = "19.1.5"

    def _seed(self, relative: str, version: str) -> Path:
        """Create one LLVM root under the Visual Studio tree and return it."""

        directory = self.install / "VC" / "Tools" / "Llvm" / relative
        (directory / "bin").mkdir(parents=True, exist_ok=True)
        (directory / "bin" / "llvm-cov.exe").write_bytes(b"x")
        (directory / "bin" / "llvm-profdata.exe").write_bytes(b"x")
        (directory / "lib" / "clang" / version / "lib" / "windows").mkdir(parents=True, exist_ok=True)
        return directory

    def test_bundled_visual_studio_wins_over_a_path_llvm_for_both_architectures(self) -> None:
        x64 = self._seed("x64", self.version)
        sibling = self._seed("", self.version)

        # A global LLVM 20 on PATH without any 32-bit compiler-rt must not be selected.
        path_bin = self.root / "llvm20" / "bin"
        path_bin.mkdir(parents=True)
        (path_bin / "clang-cl.exe").write_bytes(b"x")
        (self.root / "llvm20" / "lib" / "clang" / "20" / "lib" / "windows").mkdir(parents=True)

        with mock.patch.object(main, "_vs_installation", return_value=self.install), mock.patch(
            "shutil.which", return_value=str(path_bin / "clang-cl.exe")
        ):
            x64_llvm = main._llvm("x64")
            x86_llvm = main._llvm("x86")

        self.assertEqual(x64_llvm.directory, x64)
        self.assertEqual(x64_llvm.runtime, x64 / "lib" / "clang" / self.version / "lib" / "windows")
        self.assertEqual(x64_llvm.cov, x64 / "bin" / "llvm-cov.exe")
        self.assertEqual(x64_llvm.profdata, x64 / "bin" / "llvm-profdata.exe")

        # The compiler and coverage tools are shared; only the runtime differs by architecture.
        self.assertEqual(x86_llvm.directory, x64)
        self.assertEqual(x86_llvm.cov, x64 / "bin" / "llvm-cov.exe")
        self.assertEqual(x86_llvm.profdata, x64 / "bin" / "llvm-profdata.exe")
        self.assertEqual(x86_llvm.runtime, sibling / "lib" / "clang" / self.version / "lib" / "windows")
        self.assertNotEqual(x86_llvm.runtime, x64_llvm.runtime)

        # The forwarded tool-set version stays consistent: it is the bundled version for both.
        self.assertEqual(x86_llvm.runtime.parent.parent.name, x64_llvm.runtime.parent.parent.name)

    def test_x86_runtime_requires_the_sibling_root(self) -> None:
        x64 = self._seed("x64", self.version)
        sibling = self.install / "VC" / "Tools" / "Llvm"

        with mock.patch.object(main, "_vs_installation", return_value=self.install):
            # The x64 root alone cannot serve the 32-bit runtime.
            self.assertFalse((sibling / "lib" / "clang").exists())
            with self.assertRaises(FileNotFoundError):
                main._llvm("x86")
            # The compiler directory still resolves for its own architecture.
            self.assertEqual(main._llvm("x64").directory, x64)

        runtime = sibling / "lib" / "clang" / self.version / "lib" / "windows"
        runtime.mkdir(parents=True)
        with mock.patch.object(main, "_vs_installation", return_value=self.install):
            self.assertEqual(main._llvm("x86").runtime, runtime)


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


if __name__ == "__main__":
    unittest.main()

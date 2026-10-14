"""The staged ASan runtime comes from the same legacy 14.44 toolset the build uses.

``clang_rt.asan_dynamic-*.dll`` must match the compiler toolset that produced the
instrumented binaries.  When both the legacy 14.44 and the newer 14.51 runtimes are
installed, the driver must copy the 14.44 runtime for both architectures and must
fail closed - never silently pick the newer 14.51 DLL - when the selected runtime is
missing.  The existing "pick the newest directory" behaviour is the regression.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


LEGACY_VERSION = "14.44.10"
NEWER_VERSION = "14.51.0"
RUNTIMES = {"x86": "clang_rt.asan_dynamic-i386.dll", "x64": "clang_rt.asan_dynamic-x86_64.dll"}


class AsanRuntimeSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.installation = self.root / "vs"
        self.msbuild = self.installation / "MSBuild" / "Current" / "Bin" / "MSBuild.exe"
        self.msbuild.parent.mkdir(parents=True)
        self.msbuild.write_bytes(b"m")
        self.vcpkg = self.root / "vcpkg.exe"

    def _runtime(self, version: str, architecture: str) -> Path:
        runtime = (
            self.installation / "VC" / "Tools" / "MSVC" / version / "bin" / "Hostx64"
            / architecture / RUNTIMES[architecture]
        )
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_bytes(b"asan")
        return runtime

    def _selected(self, architecture: str) -> Path:
        with mock.patch.object(main, "_vs_installation", return_value=self.installation), \
             mock.patch.object(main, "_native_tools", return_value=(self.msbuild, self.vcpkg)):
            return main._asan_runtime(architecture)

    def _toolset_directory(self, version: str) -> None:
        (self.installation / "VC" / "Tools" / "MSVC" / version).mkdir(parents=True, exist_ok=True)

    def test_uses_the_legacy_toolset_runtime_for_both_architectures(self) -> None:
        for version in (LEGACY_VERSION, NEWER_VERSION):
            for architecture in ("x86", "x64"):
                self._runtime(version, architecture)
        for architecture in ("x86", "x64"):
            with self.subTest(architecture=architecture):
                self.assertEqual(
                    self._selected(architecture),
                    self._runtime(LEGACY_VERSION, architecture),
                )
                # The newer 14.51 runtime is a distinct file and is never the one selected.
                self.assertNotEqual(
                    self._selected(architecture),
                    self._runtime(NEWER_VERSION, architecture),
                )

    def test_a_real_legacy_toolset_missing_its_runtime_fails_even_when_the_newer_dll_exists(self) -> None:
        # The legacy 14.44 toolset directory really exists and is the one the driver selects, but
        # it does not stage an ASan DLL; the presence of the newer 14.51 DLL must not mask that.
        for architecture in ("x86", "x64"):
            self._toolset_directory(LEGACY_VERSION)
            self._runtime(NEWER_VERSION, architecture)
        for architecture in ("x86", "x64"):
            with self.subTest(architecture=architecture), self.assertRaises(
                (FileNotFoundError, main.BuildError)
            ):
                self._selected(architecture)

    def test_missing_toolset_and_missing_runtime_are_distinct_failures(self) -> None:
        # No legacy toolset directory at all: the version lookup fails before the DLL is checked.
        with self.assertRaises((FileNotFoundError, main.BuildError)):
            self._selected("x64")
        # With the toolset present but its exact DLL absent, the failure is still closed.
        self._toolset_directory(LEGACY_VERSION)
        with self.assertRaises((FileNotFoundError, main.BuildError)):
            self._selected("x64")


if __name__ == "__main__":
    unittest.main()

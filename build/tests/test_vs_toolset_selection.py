"""Every MSVC and ClangCL build uses the legacy 14.44 toolset from a VS 18.x install.

The hosted VS2026 image ships both the newer 14.51 (v145) tools and the legacy 14.44
(v143) tools.  The ordinary build is not yet v145-clean, so the driver must select a
VS 18.x installation that carries the legacy component, forward the exact servicing
version for both the MSVC and the ClangCL configurations, and fail closed instead of
silently falling back to the newer toolset.

The tests drive the real ``_msbuild_command`` and ``_vs_installation`` against an
on-disk installation shaped like ``<root>/VC/Tools/MSVC/<version>`` so the numeric
servicing order (14.44.10 must win over 14.44.9) is exercised for real.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


LEGACY_COMPONENT = "Microsoft.VisualStudio.Component.VC.14.44.17.14.x86.x64"
LEGACY_VERSION = "14.44.10"
NEWER_VERSION = "14.51.0"


class VsLookupTests(unittest.TestCase):
    def test_vswhere_selects_the_stable_vs18_range_and_requires_the_legacy_component(self) -> None:
        with mock.patch.dict(os.environ, {"ProgramFiles(x86)": r"C:\PF86"}, clear=True), \
             mock.patch.object(
                 main, "run",
                 return_value=subprocess.CompletedProcess([], 0, r"C:\VS18"),
             ) as invoked:
            installation = main._vs_installation()

        self.assertEqual(installation, Path(r"C:\VS18"))
        argv = invoked.call_args.args[0]
        self.assertIn("-version", argv)
        self.assertIn("[18.0,19.0)", argv)
        self.assertIn(LEGACY_COMPONENT, argv)
        self.assertEqual(argv[argv.index("-property") + 1], "installationPath")
        self.assertIn("-requires", argv)


class ToolsetForwardingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.installation = self.root / "vs"
        self.msbuild = self.installation / "MSBuild" / "Current" / "Bin" / "MSBuild.exe"
        self.msbuild.parent.mkdir(parents=True)
        self.msbuild.write_bytes(b"m")
        self.vcpkg = self.root / "vcpkg.exe"
        self.repository = self.root / "repo"
        self.project = self.repository / "build" / "projects" / "tests.vcxproj"

    def _toolset(self, version: str) -> None:
        binary = self.installation / "VC" / "Tools" / "MSVC" / version / "bin" / "Hostx64" / "x64" / "cl.exe"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"c")

    def _entry(self, name: str, *, directory: bool) -> None:
        """Create a sibling of the real toolset: a plain file or an empty directory."""

        path = self.installation / "VC" / "Tools" / "MSVC" / name
        if directory:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")

    def _command(self, *, clang: bool = False) -> tuple[str, ...]:
        llvm = None
        if clang:
            runtime = self.root / "llvm" / "lib" / "clang" / "23.1.3" / "lib" / "windows"
            runtime.mkdir(parents=True, exist_ok=True)
            llvm = main.Llvm(
                self.root / "llvm", runtime, self.root / "cov.exe", self.root / "prof.exe",
            )
        with mock.patch.object(main, "_vs_installation", return_value=self.installation), \
             mock.patch.object(main, "_clang_tidy", return_value=self.root / "tidy.exe"):
            return main._msbuild_command(
                self.repository, self.project, "x64", "Release", None,
                self.msbuild, self.vcpkg, llvm=llvm,
            )

    def test_forwards_the_legacy_servicing_version_for_msvc_and_clangcl(self) -> None:
        self._toolset(LEGACY_VERSION)
        self._toolset(NEWER_VERSION)
        for label, clang in (("msvc", False), ("clangcl", True)):
            with self.subTest(configured=label):
                self.assertIn(
                    f"/p:VCToolsVersion={LEGACY_VERSION}", self._command(clang=clang)
                )

    def test_numeric_servicing_order_prefers_14_44_10_over_14_44_9(self) -> None:
        self._toolset("14.44.9")
        self._toolset(LEGACY_VERSION)
        self.assertIn(f"/p:VCToolsVersion={LEGACY_VERSION}", self._command())

    def test_missing_legacy_toolset_fails_closed_instead_of_using_the_newer_one(self) -> None:
        self._toolset(NEWER_VERSION)
        with self.assertRaises((FileNotFoundError, main.BuildError)):
            self._command()

    def test_a_file_with_a_numeric_version_name_never_displaces_the_real_toolset(self) -> None:
        # The legacy glob ``14.44.*`` also matches a stray file such as the backup artifact
        # ``14.44.99999``. A file is not a toolset directory, so the real 14.44.35207 must still
        # win even though the file's numeric suffix sorts higher.
        self._toolset("14.44.35207")
        self._entry("14.44.99999", directory=False)
        self.assertIn("/p:VCToolsVersion=14.44.35207", self._command())

    def test_a_backup_suffixed_directory_never_breaks_or_wins_the_selection(self) -> None:
        # ``14.44.35207.backup`` is a real directory the glob matches, but its name is not a fully
        # numeric servicing number; it must be ignored rather than crashing the numeric sort or
        # displacing the valid 14.44.35207 toolset.
        self._toolset("14.44.35207")
        self._entry("14.44.35207.backup", directory=True)
        self.assertIn("/p:VCToolsVersion=14.44.35207", self._command())

    def test_only_a_backup_suffixed_directory_is_no_valid_toolset(self) -> None:
        self._entry("14.44.35207.backup", directory=True)
        with self.assertRaises(FileNotFoundError):
            self._command()


if __name__ == "__main__":
    unittest.main()

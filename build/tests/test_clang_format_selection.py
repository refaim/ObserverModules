"""The source format gate uses one pinned standalone clang-format 23.1.3.

The formatter resolves from the standard ``PATH`` (the owner installs it there) or is named by
an explicit ``UNPACKRAT_CLANG_FORMAT`` override; its version is validated and it is never
borrowed from the Visual Studio LLVM toolset or the private ``%LOCALAPPDATA%`` cache.  The
official LLVM 23 x64 archive ships no 32-bit runtime, so the standalone tools stay
format/analyze-only while the VS bundled compiler, coverage and profile tools remain together
for the build.
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


# The exact release provenance line the owner's installed clang-format prints.
VCS_SUFFIX = "(https://github.com/llvm/llvm-project 0d261d1ca552c95a8f007e061c787ac7132fbcbc)"


class ClangFormatSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        # A CI runner may export an inherited override; empty means "no explicit override",
        # so the scoped default resolution the cases below assert stays authoritative.
        ambient = mock.patch.dict(os.environ, {"UNPACKRAT_CLANG_FORMAT": ""}, clear=False)
        ambient.start()
        self.addCleanup(ambient.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.exe = self.root / "llvm" / "bin" / "clang-format.exe"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b"test executable")

    def _version(self, text: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, text)

    def test_default_resolves_the_pinned_tool_from_the_standard_path(self) -> None:
        with mock.patch("shutil.which", return_value=str(self.exe)) as which, \
             mock.patch.object(
                 main, "run",
                 return_value=self._version("clang-format version 23.1.3\n"),
             ) as invoked:
            self.assertEqual(main._clang_format(), self.exe.resolve())
        which.assert_called_once_with("clang-format.exe")
        invoked.assert_called_once_with([str(self.exe.resolve()), "--version"], capture=True)

    def test_default_never_reads_or_creates_a_user_cache(self) -> None:
        cache = self.root / "localappdata"
        with mock.patch("shutil.which", return_value=str(self.exe)), \
             mock.patch.object(main, "run", return_value=self._version("clang-format version 23.1.3")), \
             mock.patch.dict(os.environ, {"LOCALAPPDATA": str(cache)}, clear=True):
            self.assertEqual(main._clang_format(), self.exe.resolve())
        self.assertFalse(cache.exists())

    def test_override_selects_exact_executable_without_path_fallback(self) -> None:
        override = self.root / "custom" / "clang-format.exe"
        override.parent.mkdir()
        override.write_bytes(b"test executable")
        with mock.patch("shutil.which") as which, mock.patch.object(
            main, "run", return_value=self._version("clang-format version 23.1.3")
        ), mock.patch.dict(os.environ, {"UNPACKRAT_CLANG_FORMAT": str(override)}, clear=True):
            self.assertEqual(main._clang_format(), override.resolve())
        which.assert_not_called()

    def test_relative_override_keeps_same_executable_when_msbuild_changes_cwd(self) -> None:
        override = self.root / "custom" / "clang-format.exe"
        override.parent.mkdir()
        override.write_bytes(b"test executable")
        original = Path.cwd()
        try:
            os.chdir(self.root)
            with mock.patch.dict(
                os.environ, {"UNPACKRAT_CLANG_FORMAT": "custom/clang-format.exe"}, clear=True
            ), mock.patch.object(main, "run", return_value=self._version("clang-format version 23.1.3")):
                self.assertEqual(main._clang_format(), override.resolve())
        finally:
            os.chdir(original)

    def test_missing_default_tool_or_missing_override_fails_closed(self) -> None:
        with mock.patch("shutil.which", return_value=None), self.assertRaises(FileNotFoundError):
            main._clang_format()
        with mock.patch.dict(
            os.environ, {"UNPACKRAT_CLANG_FORMAT": str(self.root / "missing.exe")}, clear=True
        ), self.assertRaises(FileNotFoundError):
            main._clang_format()

    def test_wrong_or_missing_version_fails_closed(self) -> None:
        for output in (
            "clang-format version 19.1.5",
            "clang-format version 23.1.30",
            "clang-format version 23.1.80",
            "clang-format unknown",
        ):
            with self.subTest(output=output), \
                 mock.patch("shutil.which", return_value=str(self.exe)), \
                 mock.patch.object(main, "run", return_value=self._version(output)), \
                 self.assertRaises(main.BuildError):
                main._clang_format()

    def test_accepts_the_official_release_vcs_suffix(self) -> None:
        output = f"clang-format version 23.1.3 {VCS_SUFFIX}\n"
        with mock.patch("shutil.which", return_value=str(self.exe)), \
             mock.patch.object(main, "run", return_value=self._version(output)):
            self.assertEqual(main._clang_format(), self.exe.resolve())

    def test_source_stage_uses_the_pinned_standalone_formatter(self) -> None:
        # The VS bundled LLVM stays out of the format gate, so a bundled tool returned by
        # ``_llvm_tool`` must never satisfy the source stage.
        bundled = self.root / "vs" / "clang-format.exe"
        with mock.patch.object(main, "_clang_format", return_value=self.exe) as selected, \
             mock.patch.object(main, "_llvm_tool", return_value=bundled), \
             mock.patch.object(main, "_require_tool", side_effect=lambda name: self.root / f"{name}.exe"):
            tools = main._source_tools()
        selected.assert_called_once_with()
        self.assertEqual(tools.clang_format, self.exe)


if __name__ == "__main__":
    unittest.main()

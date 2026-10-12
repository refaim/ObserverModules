from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.source_tools import discover_source_tools  # noqa: E402


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


@dataclass(frozen=True)
class FakeToolchain:
    llvm_dir: Path
    pwsh: Path
    vcpkg_root: Path
    environment: tuple[tuple[str, str], ...]


class SourceToolDiscoveryTests(unittest.TestCase):
    def test_discovers_exact_paths_versions_and_preserves_runtime_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clang_format = executable(root / "llvm/bin/clang-format.exe")
            pwsh = executable(root / "PowerShell/pwsh.exe")
            cppcheck = executable(root / "Cppcheck/cppcheck.exe")
            pssa = executable(root / "Modules/PSScriptAnalyzer/PSScriptAnalyzer.psd1")
            vcpkg_root = root / "vcpkg"
            vcpkg_root.mkdir()
            toolchain = FakeToolchain(
                root / "llvm",
                pwsh,
                vcpkg_root,
                (("LIB", "sdk"),),
            )
            responses = (
                "PowerShell 7.5.2",
                "clang-format version 21.1.0",
                "Cppcheck 2.18.0",
                json.dumps({"Path": str(pssa), "Version": "1.24.0"}),
            )

            with (
                mock.patch("core.source_tools.shutil.which", return_value=str(cppcheck)) as which,
                mock.patch("core.source_tools._output", side_effect=responses) as output,
            ):
                tools = discover_source_tools(toolchain)

        self.assertEqual(tools.pwsh, pwsh.resolve())
        self.assertEqual(tools.clang_format, clang_format.resolve())
        self.assertEqual(tools.cppcheck, cppcheck.resolve())
        self.assertEqual(tools.psscriptanalyzer, pssa.resolve())
        self.assertEqual(tools.vcpkg_root, vcpkg_root.resolve())
        self.assertEqual(tools.environment, (("LIB", "sdk"),))
        self.assertEqual(
            dict(tools.identity),
            {
                "clang_format": str(clang_format.resolve()),
                "clang_format_version": "clang-format version 21.1.0",
                "cppcheck": str(cppcheck.resolve()),
                "cppcheck_version": "Cppcheck 2.18.0",
                "psscriptanalyzer": str(pssa.resolve()),
                "psscriptanalyzer_version": "1.24.0",
                "pwsh": str(pwsh.resolve()),
                "pwsh_version": "PowerShell 7.5.2",
                "vcpkg_root": str(vcpkg_root.resolve()),
            },
        )
        which.assert_called_once_with("cppcheck.exe")
        self.assertEqual(output.call_count, 4)
        self.assertEqual(output.call_args_list[0].args[0], [str(pwsh.resolve()), "--version"])
        self.assertEqual(
            output.call_args_list[1].args[0],
            [str(clang_format.resolve()), "--version"],
        )
        self.assertEqual(output.call_args_list[2].args[0], [str(cppcheck.resolve()), "--version"])
        self.assertEqual(output.call_args_list[3].args[0][0], str(pwsh.resolve()))
        self.assertIn("Get-Module -ListAvailable PSScriptAnalyzer", output.call_args_list[3].args[0][-1])

    def test_missing_cppcheck_is_reported_without_running_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            toolchain = FakeToolchain(
                root / "llvm",
                executable(root / "pwsh.exe"),
                root / "vcpkg",
                (),
            )
            executable(root / "llvm/bin/clang-format.exe")
            (root / "vcpkg").mkdir()
            with (
                mock.patch("core.source_tools.shutil.which", return_value=None),
                mock.patch("core.source_tools._output") as output,
                self.assertRaisesRegex(FileNotFoundError, "cppcheck.exe"),
            ):
                discover_source_tools(toolchain)

        output.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

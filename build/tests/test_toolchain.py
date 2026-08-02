from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.toolchain import _command_environment, discover_msvc_toolchain  # noqa: E402


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


class MsvcToolchainTests(unittest.TestCase):
    def test_command_environment_is_stable_when_canonical_values_are_inherited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            include = root / "include"
            lib = root / "lib"
            libpath = root / "references"
            for directory in (include, lib, libpath):
                directory.mkdir()

            canonical = {
                "INCLUDE": str(include),
                "LIB": str(lib),
                "LIBPATH": str(libpath),
                "VCToolsVersion": "14.44.35207",
                "VSCMD_VER": "17.14.15",
                "WindowsSDKVersion": "10.0.26100.0\\",
            }
            captured = "\r\n".join(
                (
                    *(f"{key}={value}" for key, value in canonical.items()),
                    "Path=toolchain;host",
                    "__VSCMD_PREINIT_PATH=host",
                    "GITHUB_SHA=unchanged",
                    "UNRELATED=unchanged",
                )
            )
            inherited = {"GITHUB_SHA": "unchanged", "UNRELATED": "unchanged"}

            with mock.patch.dict(os.environ, inherited, clear=True):
                absent = _command_environment(captured)
            with mock.patch.dict(os.environ, inherited | canonical, clear=True):
                already_equal = _command_environment(captured)

            self.assertEqual(dict(absent), canonical)
            self.assertEqual(already_equal, absent)

    def test_discovers_x64_tools_and_canonical_command_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program_files = root / "Program Files (x86)"
            system_root = root / "Windows"
            installation = root / "Visual Studio"
            vcpkg_root = root / "vcpkg" / "2026.07.29"
            lib = root / "SDK" / "Lib"
            lib.mkdir(parents=True)
            vcpkg_root.mkdir(parents=True)

            vswhere = executable(
                program_files
                / "Microsoft Visual Studio"
                / "Installer"
                / "vswhere.exe"
            )
            cmd = executable(system_root / "System32" / "cmd.exe")
            msbuild = executable(
                installation / "MSBuild" / "Current" / "Bin" / "amd64" / "MSBuild.exe"
            )
            vsdevcmd = executable(installation / "Common7" / "Tools" / "VsDevCmd.bat")
            llvm_dir = installation / "VC" / "Tools" / "Llvm" / "x64"
            clang_tidy = executable(llvm_dir / "bin" / "clang-tidy.exe")
            pwsh = executable(root / "PowerShell" / "pwsh.exe")

            original = {
                "ProgramFiles(x86)": str(program_files),
                "SystemRoot": str(system_root),
                "VCPKG_ROOT": str(vcpkg_root),
                "UNCHANGED": "host",
                "Path": "host",
            }
            command_environment = "\r\n".join(
                (
                    "=C:=C:\\working",
                    "Path=first;host",
                    "PATH=host;second",
                    "__VSCMD_PREINIT_PATH=host",
                    f"Lib={lib};{root / 'missing'}",
                    "VisualStudioVersion=17.0",
                    "VSCMD_VER=17.14.15",
                    "VCToolsVersion=14.44.35207",
                    "WindowsSDKVersion=10.0.26100.0\\",
                    "UNCHANGED=host",
                    "",
                )
            )
            responses = (
                subprocess.CompletedProcess([], 0, stdout=f"{installation}\r\n", stderr=""),
                subprocess.CompletedProcess([], 0, stdout=command_environment, stderr=""),
                subprocess.CompletedProcess([], 0, stdout="17.14.51.32402\r\n", stderr=""),
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout="LLVM tools\r\n  LLVM version 19.1.5\r\nOptimized build.\r\n",
                    stderr="",
                ),
            )

            with (
                mock.patch.dict(os.environ, original, clear=True),
                mock.patch("core.toolchain.shutil.which", return_value=str(pwsh)) as which,
                mock.patch("core.toolchain.subprocess.run", side_effect=responses) as run,
            ):
                host_before = dict(os.environ)
                toolchain = discover_msvc_toolchain()
                host_after = dict(os.environ)

            self.assertEqual(toolchain.installation, installation.resolve())
            self.assertEqual(toolchain.msbuild, msbuild.resolve())
            self.assertEqual(toolchain.vsdevcmd, vsdevcmd.resolve())
            self.assertEqual(toolchain.llvm_dir, llvm_dir.resolve())
            self.assertEqual(toolchain.clang_tidy, clang_tidy.resolve())
            self.assertEqual(toolchain.vcpkg_root, vcpkg_root.resolve())
            self.assertEqual(toolchain.pwsh, pwsh.resolve())
            self.assertEqual(
                dict(toolchain.environment),
                {
                    "LIB": str(lib),
                    "VCToolsVersion": "14.44.35207",
                    "VisualStudioVersion": "17.0",
                    "VSCMD_VER": "17.14.15",
                    "WindowsSDKVersion": "10.0.26100.0\\",
                },
            )
            self.assertEqual(
                dict(toolchain.identity),
                {
                    "clang_tidy": str(clang_tidy.resolve()),
                    "clang_tidy_version": "19.1.5",
                    "installation": str(installation.resolve()),
                    "msbuild": str(msbuild.resolve()),
                    "msbuild_version": "17.14.51.32402",
                    "pwsh": str(pwsh.resolve()),
                    "vc_tools_version": "14.44.35207",
                    "vcpkg_root": str(vcpkg_root.resolve()),
                    "vsdevcmd": str(vsdevcmd.resolve()),
                    "vsdevcmd_version": "17.14.15",
                    "windows_sdk_version": "10.0.26100.0\\",
                },
            )
            self.assertEqual(host_after, host_before)
            self.assertEqual(which.call_args_list, [mock.call("pwsh")])
            self.assertEqual(
                run.call_args_list[0].args[0],
                [
                    str(vswhere.resolve()),
                    "-latest",
                    "-products",
                    "*",
                    "-requires",
                    "Microsoft.Component.MSBuild",
                    "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                    "Microsoft.VisualStudio.Component.VC.Llvm.Clang",
                    "-property",
                    "installationPath",
                ],
            )
            payload = (
                f'call "{vsdevcmd.resolve()}" -no_logo -arch=amd64 '
                "-host_arch=amd64 >nul && set"
            )
            self.assertEqual(
                run.call_args_list[1].args[0],
                f'"{cmd.resolve()}" /d /s /c "{payload}"',
            )
            self.assertEqual(
                run.call_args_list[0].kwargs,
                {"check": True, "capture_output": True, "text": True},
            )
            self.assertEqual(
                run.call_args_list[1].kwargs,
                {
                    "check": True,
                    "capture_output": True,
                    "executable": str(cmd.resolve()),
                    "text": True,
                },
            )
            self.assertEqual(run.call_args_list[2].args[0], [str(msbuild.resolve()), "-version", "-nologo"])
            self.assertEqual(run.call_args_list[3].args[0], [str(clang_tidy.resolve()), "--version"])
            for call in (run.call_args_list[2], run.call_args_list[3]):
                self.assertEqual(
                    call.kwargs,
                    {"check": True, "capture_output": True, "text": True},
                )

    def test_falls_back_to_bin_msbuild_and_vcpkg_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program_files = root / "Program Files (x86)"
            system_root = root / "Windows"
            installation = root / "Visual Studio"
            executable(
                program_files
                / "Microsoft Visual Studio"
                / "Installer"
                / "vswhere.exe"
            )
            executable(system_root / "System32" / "cmd.exe")
            msbuild = executable(
                installation / "MSBuild" / "Current" / "Bin" / "MSBuild.exe"
            )
            executable(installation / "Common7" / "Tools" / "VsDevCmd.bat")
            executable(installation / "VC" / "Tools" / "Llvm" / "x64" / "bin" / "clang-tidy.exe")
            pwsh = executable(root / "PowerShell" / "pwsh.exe")
            vcpkg = executable(root / "vcpkg" / "2026.07.29" / "vcpkg.exe")
            responses = (
                subprocess.CompletedProcess([], 0, stdout=str(installation), stderr=""),
                subprocess.CompletedProcess([], 0, stdout="PATH=tools\r\n", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="17.14.51.32402\r\n", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="LLVM version 19.1.5\r\n", stderr=""),
            )

            def which(name: str) -> str:
                return str({"pwsh": pwsh, "vcpkg": vcpkg}[name])

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "ProgramFiles(x86)": str(program_files),
                        "SystemRoot": str(system_root),
                    },
                    clear=True,
                ),
                mock.patch("core.toolchain.shutil.which", side_effect=which) as find,
                mock.patch("core.toolchain.subprocess.run", side_effect=responses),
            ):
                toolchain = discover_msvc_toolchain()

            self.assertEqual(toolchain.msbuild, msbuild.resolve())
            self.assertEqual(toolchain.vcpkg_root, vcpkg.resolve().parent)
            self.assertEqual(find.call_args_list, [mock.call("vcpkg"), mock.call("pwsh")])


if __name__ == "__main__":
    unittest.main(verbosity=2)

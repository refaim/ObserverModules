"""Discover the installed x64 MSVC analysis toolchain."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess


@dataclass(frozen=True, slots=True)
class MsvcToolchain:
    installation: Path
    msbuild: Path
    vsdevcmd: Path
    llvm_dir: Path
    clang_tidy: Path
    vcpkg_root: Path
    pwsh: Path
    environment: tuple[tuple[str, str], ...]
    identity: tuple[tuple[str, str], ...]


_DEVELOPER_VARIABLES = frozenset(
    {
        "devenvdir",
        "extensionsdkdir",
        "external_include",
        "include",
        "lib",
        "libpath",
        "netfxsdkdir",
        "ucrtversion",
        "universalcrtsdkdir",
        "vcideinstalldir",
        "vcinstalldir",
        "visualstudioversion",
        "vs170comntools",
        "vsinstalldir",
        "windowslibpath",
    }
)
_DEVELOPER_PREFIXES = ("framework", "vctools", "vscmd_", "windowssdk")


def _developer_variable(name: str) -> bool:
    return name in _DEVELOPER_VARIABLES or name.startswith(_DEVELOPER_PREFIXES)


def _existing(path: Path | str | None, directory: bool = False) -> Path:
    resolved = Path(path).resolve() if path else None
    if resolved is None or not (resolved.is_dir() if directory else resolved.is_file()):
        raise FileNotFoundError(f"required path not found: {path}")
    return resolved


def _command_environment(text: str) -> tuple[tuple[str, str], ...]:
    values: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        if not line or line.startswith("="):
            continue
        key, value = line.split("=", 1)
        folded = key.casefold()
        if not _developer_variable(folded):
            continue
        canonical = key.upper() if folded in {"path", "lib"} else key
        if folded == "lib":
            value = os.pathsep.join(
                part for part in value.split(os.pathsep) if Path(part).is_dir()
            )
        values[folded] = (canonical, value)

    return tuple(sorted(values.values(), key=lambda pair: pair[0].casefold()))


def _output(argv: list[str] | str, **options: str) -> str:
    output = subprocess.run(
        argv, check=True, capture_output=True, text=True, **options
    ).stdout.strip()
    if not output:
        raise RuntimeError(f"tool returned empty output: {argv[0]}")
    return output
def discover_msvc_toolchain() -> MsvcToolchain:
    """Locate Visual Studio tools and capture an isolated amd64 developer environment."""

    components = (
        "Microsoft.Component.MSBuild",
        "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
        "Microsoft.VisualStudio.Component.VC.Llvm.Clang",
    )
    vswhere = _existing(
        Path(os.environ["ProgramFiles(x86)"])
        / "Microsoft Visual Studio"
        / "Installer"
        / "vswhere.exe"
    )
    installation = _existing(
        _output(
            [
                str(vswhere),
                "-latest",
                "-products",
                "*",
                "-requires",
                *components,
                "-property",
                "installationPath",
            ]
        ),
        directory=True,
    )
    bin_dir = installation / "MSBuild" / "Current" / "Bin"
    amd64_msbuild = bin_dir / "amd64" / "MSBuild.exe"
    msbuild = _existing(amd64_msbuild if amd64_msbuild.is_file() else bin_dir / "MSBuild.exe")
    vsdevcmd = _existing(installation / "Common7" / "Tools" / "VsDevCmd.bat")
    llvm_dir = _existing(installation / "VC" / "Tools" / "Llvm" / "x64", directory=True)
    clang_tidy = _existing(llvm_dir / "bin" / "clang-tidy.exe")
    cmd = _existing(Path(os.environ.get("SystemRoot", "")) / "System32" / "cmd.exe")
    payload = f'call "{vsdevcmd}" -no_logo -arch=amd64 -host_arch=amd64 >nul && set'
    environment = _command_environment(
        _output(f'"{cmd}" /d /s /c "{payload}"', executable=str(cmd))
    )
    msbuild_version = _output([str(msbuild), "-version", "-nologo"])
    clang_output = _output([str(clang_tidy), "--version"])
    clang_tidy_version = next(
        line.partition("LLVM version")[2].strip()
        for line in clang_output.splitlines()
        if "LLVM version" in line
    )
    vcpkg_root = _existing(
        os.environ.get("VCPKG_ROOT")
        or _existing(shutil.which("vcpkg")).parent,
        directory=True,
    )
    pwsh = _existing(shutil.which("pwsh"))
    captured = {key.casefold(): value for key, value in environment}
    identity = (
        ("clang_tidy", str(clang_tidy)),
        ("clang_tidy_version", clang_tidy_version),
        ("installation", str(installation)),
        ("msbuild", str(msbuild)),
        ("msbuild_version", msbuild_version),
        ("pwsh", str(pwsh)),
        ("vc_tools_version", captured.get("vctoolsversion", "")),
        ("vcpkg_root", str(vcpkg_root)),
        ("vsdevcmd", str(vsdevcmd)),
        ("vsdevcmd_version", captured.get("vscmd_ver", "")),
        ("windows_sdk_version", captured.get("windowssdkversion", "")),
    )
    return MsvcToolchain(installation, msbuild, vsdevcmd, llvm_dir, clang_tidy,
                         vcpkg_root, pwsh, environment, identity)

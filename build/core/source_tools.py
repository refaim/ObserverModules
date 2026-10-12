"""Discover and fingerprint the repository source-check tools."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil

from core.toolchain import MsvcToolchain, _output


@dataclass(frozen=True, slots=True)
class SourceTools:
    pwsh: Path
    clang_format: Path
    cppcheck: Path
    psscriptanalyzer: Path
    vcpkg_root: Path
    environment: tuple[tuple[str, str], ...]
    identity: tuple[tuple[str, str], ...]


def discover_source_tools(toolchain: MsvcToolchain) -> SourceTools:
    """Locate source analyzers and capture their exact cache identity."""

    pwsh = toolchain.pwsh.resolve(strict=True)
    clang_format = (toolchain.llvm_dir / "bin/clang-format.exe").resolve(strict=True)
    candidate = shutil.which("cppcheck.exe")
    if candidate is None:
        raise FileNotFoundError("cppcheck.exe was not found on PATH")
    cppcheck = Path(candidate).resolve(strict=True)
    vcpkg_root = toolchain.vcpkg_root.resolve(strict=True)
    pwsh_version = _output([str(pwsh), "--version"])
    clang_format_version = _output([str(clang_format), "--version"])
    cppcheck_version = _output([str(cppcheck), "--version"])

    pssa_query = """
$module = Get-Module -ListAvailable PSScriptAnalyzer |
    Sort-Object Version -Descending |
    Select-Object -First 1
if (-not $module) { throw 'PSScriptAnalyzer was not found' }
[ordered]@{ Path = $module.Path; Version = $module.Version.ToString() } |
    ConvertTo-Json -Compress
"""
    pssa_document = json.loads(
        _output([str(pwsh), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", pssa_query])
    )
    psscriptanalyzer = Path(pssa_document["Path"]).resolve(strict=True)
    identity = {
        "clang_format": str(clang_format),
        "clang_format_version": clang_format_version,
        "cppcheck": str(cppcheck),
        "cppcheck_version": cppcheck_version,
        "psscriptanalyzer": str(psscriptanalyzer),
        "psscriptanalyzer_version": str(pssa_document["Version"]),
        "pwsh": str(pwsh),
        "pwsh_version": pwsh_version,
        "vcpkg_root": str(vcpkg_root),
    }
    return SourceTools(
        pwsh,
        clang_format,
        cppcheck,
        psscriptanalyzer,
        vcpkg_root,
        toolchain.environment,
        tuple(identity.items()),
    )

"""Resolve immutable identities for optional local quality tools without installing them."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil

from core.toolchain import MsvcToolchain


@dataclass(frozen=True, slots=True)
class ResolvedTool:
    path: Path
    identity: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ResolvedDirectory:
    path: Path
    files: tuple[ResolvedTool, ...]

    @property
    def identity(self) -> tuple[tuple[str, str], ...]:
        return (("path", str(self.path)),) + tuple(
            (f"{tool.path.name}.{key}", value)
            for tool in self.files
            for key, value in tool.identity
        )


@dataclass(frozen=True, slots=True)
class QualityTools:
    clang_cl: ResolvedTool
    clang_scan_deps: ResolvedTool
    llvm_cov: ResolvedTool
    llvm_profdata: ResolvedTool
    dumpbin: ResolvedTool
    binskim: ResolvedTool
    umdh: ResolvedTool


@dataclass(frozen=True, slots=True)
class SanitizerRuntimes:
    asan_x86: ResolvedTool
    asan_x64: ResolvedTool
    ubsan: ResolvedDirectory


_LLVM_TOOLS = frozenset(("clang-cl", "clang-scan-deps", "llvm-cov", "llvm-profdata"))
UBSAN_LIBRARIES = (
    "clang_rt.ubsan_standalone-x86_64.lib",
    "clang_rt.ubsan_standalone_cxx-x86_64.lib",
)
_ASAN_RUNTIMES = {
    "x86": "clang_rt.asan_dynamic-i386.dll",
    "x64": "clang_rt.asan_dynamic-x86_64.dll",
}


def resolve_tool(path: Path | str | None, name: str) -> ResolvedTool:
    try:
        resolved = Path(path).resolve(strict=True) if path else None
    except (OSError, RuntimeError) as error:
        raise FileNotFoundError(f"missing {name}: {path}") from error
    if resolved is None or not resolved.is_file():
        raise FileNotFoundError(f"missing {name}: {path}")
    with resolved.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return ResolvedTool(resolved, (("path", str(resolved)), ("sha256", digest)))


def _identity_value(toolchain: MsvcToolchain, key: str, name: str) -> str:
    value = dict(toolchain.identity).get(key)
    if not value:
        raise FileNotFoundError(f"missing {name} identity")
    return value


def resolve_llvm(toolchain: MsvcToolchain, name: str) -> ResolvedTool:
    if name not in _LLVM_TOOLS:
        raise ValueError(f"unsupported LLVM tool: {name}")
    return resolve_tool(toolchain.llvm_dir / f"bin/{name}.exe", name)


def resolve_dumpbin(toolchain: MsvcToolchain) -> ResolvedTool:
    version = _identity_value(toolchain, "vc_tools_version", "MSVC vc_tools_version")
    path = toolchain.installation / f"VC/Tools/MSVC/{version}/bin/Hostx64/x64/dumpbin.exe"
    return resolve_tool(path, "dumpbin")


def resolve_binskim() -> ResolvedTool:
    return resolve_tool(shutil.which("binskim"), "BinSkim")


def resolve_umdh() -> ResolvedTool:
    override = os.environ.get("OBSERVER_UMDH")
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"missing UMDH override: {path}")
        return resolve_tool(path, "UMDH")
    program_files = os.environ.get("ProgramFiles(x86)")
    candidates = (() if not program_files else tuple(
        Path(program_files) / f"Windows Kits/{version}/Debuggers/x64/umdh.exe"
        for version in ("11", "10")
    ))
    for candidate in candidates:
        if candidate.is_file():
            return resolve_tool(candidate, "UMDH")
    searched = ", ".join(map(str, candidates)) or "ProgramFiles(x86) is unset"
    raise FileNotFoundError(f"missing UMDH: {searched}")


def resolve_asan_runtimes(
    toolchain: MsvcToolchain, architectures: tuple[str, ...]
) -> tuple[tuple[str, ResolvedTool], ...]:
    """Resolve only the requested MSVC ASan runtime DLLs."""

    unsupported = next(
        (architecture for architecture in architectures if architecture not in _ASAN_RUNTIMES),
        None,
    )
    if unsupported is not None:
        raise ValueError(f"unsupported ASan architecture: {unsupported}")
    msvc = _identity_value(toolchain, "vc_tools_version", "MSVC vc_tools_version")
    asan = toolchain.installation / f"VC/Tools/MSVC/{msvc}/bin/Hostx64"
    return tuple(
        (
            architecture,
            resolve_tool(
                asan / architecture / _ASAN_RUNTIMES[architecture],
                f"MSVC ASan {architecture} runtime",
            ),
        )
        for architecture in architectures
    )


def resolve_ubsan_runtime(toolchain: MsvcToolchain) -> ResolvedDirectory:
    """Resolve only the LLVM x64 UBSan archive pair."""

    llvm = _identity_value(toolchain, "clang_tidy_version", "LLVM clang_tidy_version")
    root = toolchain.llvm_dir / "lib/clang"
    candidates = tuple(root / version / "lib/windows" for version in dict.fromkeys((llvm, llvm.split(".")[0])))
    directory = next((candidate.resolve() for candidate in candidates if candidate.is_dir()), None)
    if directory is None:
        raise FileNotFoundError(f"missing UBSan runtime directory: {', '.join(map(str, candidates))}")
    files = tuple(resolve_tool(directory / name, f"UBSan runtime {name}") for name in UBSAN_LIBRARIES)
    return ResolvedDirectory(directory, files)


def resolve_sanitizer_runtimes(toolchain: MsvcToolchain) -> SanitizerRuntimes:
    """Resolve every sanitizer runtime required by doctor and aggregate verification."""

    asan = dict(resolve_asan_runtimes(toolchain, ("x86", "x64")))
    return SanitizerRuntimes(
        asan["x86"], asan["x64"], resolve_ubsan_runtime(toolchain)
    )


def discover_quality_tools(toolchain: MsvcToolchain) -> QualityTools:
    """Resolve the exact quality-tool files expected by coverage, sanitizer, audit, and leak graphs."""

    return QualityTools(
        clang_cl=resolve_llvm(toolchain, "clang-cl"),
        clang_scan_deps=resolve_llvm(toolchain, "clang-scan-deps"),
        llvm_cov=resolve_llvm(toolchain, "llvm-cov"),
        llvm_profdata=resolve_llvm(toolchain, "llvm-profdata"),
        dumpbin=resolve_dumpbin(toolchain),
        binskim=resolve_binskim(),
        umdh=resolve_umdh(),
    )

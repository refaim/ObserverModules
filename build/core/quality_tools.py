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
# clang resolves its runtime directory relative to its own binary, so every 32-bit compiler-rt
# archive only ever exists in the 32-bit toolset that ships beside the x64 one.
_LLVM_TOOLSETS = {"x86": "VC/Tools/Llvm", "x64": "VC/Tools/Llvm/x64"}
PROFILE_LIBRARIES = {
    "x86": "clang_rt.profile-i386.lib",
    "x64": "clang_rt.profile-x86_64.lib",
}
# unpackrat-fuzz.props links exactly these three archives per architecture.
FUZZER_LIBRARIES = {
    "x86": (
        "clang_rt.fuzzer-i386.lib",
        "clang_rt.asan-i386.lib",
        "clang_rt.asan_cxx-i386.lib",
    ),
    "x64": (
        "clang_rt.fuzzer-x86_64.lib",
        "clang_rt.asan-x86_64.lib",
        "clang_rt.asan_cxx-x86_64.lib",
    ),
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
    override = os.environ.get("UNPACKRAT_UMDH")
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


def _runtime_directory(toolchain: MsvcToolchain, toolset: Path, name: str) -> Path:
    llvm = _identity_value(toolchain, "clang_tidy_version", "LLVM clang_tidy_version")
    root = toolset / "lib/clang"
    candidates = tuple(root / version / "lib/windows" for version in dict.fromkeys((llvm, llvm.split(".")[0])))
    directory = next((candidate.resolve() for candidate in candidates if candidate.is_dir()), None)
    if directory is None:
        raise FileNotFoundError(f"missing {name} runtime directory: {', '.join(map(str, candidates))}")
    return directory


def resolve_ubsan_runtime(toolchain: MsvcToolchain) -> ResolvedDirectory:
    """Resolve only the LLVM x64 UBSan archive pair."""

    directory = _runtime_directory(toolchain, toolchain.llvm_dir, "UBSan")
    files = tuple(resolve_tool(directory / name, f"UBSan runtime {name}") for name in UBSAN_LIBRARIES)
    return ResolvedDirectory(directory, files)


def resolve_profile_runtime(toolchain: MsvcToolchain, architecture: str) -> ResolvedDirectory:
    """Resolve the LLVM profile archive from the toolset that owns the requested architecture."""

    if architecture not in PROFILE_LIBRARIES:
        raise ValueError(f"coverage has no profile runtime for {architecture}")
    directory = _runtime_directory(
        toolchain,
        toolchain.installation / _LLVM_TOOLSETS[architecture],
        f"{architecture} profile",
    )
    name = PROFILE_LIBRARIES[architecture]
    return ResolvedDirectory(
        directory, (resolve_tool(directory / name, f"profile runtime {name}"),)
    )


def resolve_profile_runtimes(
    toolchain: MsvcToolchain,
) -> tuple[tuple[str, ResolvedDirectory], ...]:
    """Resolve every profile runtime the Coverage configuration claims to support."""

    return tuple(
        (architecture, resolve_profile_runtime(toolchain, architecture))
        for architecture in PROFILE_LIBRARIES
    )


def resolve_fuzzer_runtime(toolchain: MsvcToolchain, architecture: str) -> ResolvedDirectory:
    """Resolve the libFuzzer and ASan archives from the toolset that owns the architecture."""

    if architecture not in FUZZER_LIBRARIES:
        raise ValueError(f"fuzzing has no libFuzzer runtime for {architecture}")
    directory = _runtime_directory(
        toolchain,
        toolchain.installation / _LLVM_TOOLSETS[architecture],
        f"{architecture} libFuzzer",
    )
    return ResolvedDirectory(directory, tuple(
        resolve_tool(directory / name, f"libFuzzer runtime {name}")
        for name in FUZZER_LIBRARIES[architecture]
    ))


def resolve_fuzzer_runtimes(
    toolchain: MsvcToolchain,
) -> tuple[tuple[str, ResolvedDirectory], ...]:
    """Resolve every libFuzzer runtime the Fuzz configuration claims to support."""

    return tuple(
        (architecture, resolve_fuzzer_runtime(toolchain, architecture))
        for architecture in FUZZER_LIBRARIES
    )


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

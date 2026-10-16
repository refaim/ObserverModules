"""Minimal console build driver for unpackrat.

One direct route: vcpkg restores the pinned manifest, MSBuild owns project evaluation,
dependencies and incremental state under ``out/native``, and every other operation is a
short function that runs one real tool and lets that tool's exit code be the verdict.
The public surface is ``build``, ``test``, ``package``, ``coverage_complete``, ``run`` and
``main``; the CLI exposes ``build``, ``test``, ``ci-source``, ``ci-arch`` and ``package``.
``ci-arch`` takes a ``-Stage`` selection so CI can split the per-architecture work into
independent jobs while the local default still runs every stage in sequence.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import zipfile


REPOSITORY = Path(__file__).resolve().parents[1]
ARCHITECTURES = ("x86", "x64")
PLATFORMS = {"x86": "Win32", "x64": "x64"}
CONFIGURATIONS = ("Debug", "Release")
MODULES = ("renpy", "rpgmaker", "zanzarah", "godot")
MODULE_BINARIES = {
    "renpy": "renpy.so",
    "rpgmaker": "rpgmaker.so",
    "zanzarah": "zanzarah.so",
    "godot": "godot.so",
}
OUT_NATIVE = "out/native"
OUT_PACKAGES = "out/packages"
TEST_PROJECT = "build/projects/tests.vcxproj"
LEAK_PROBE_PROJECT = "build/projects/leak-probe.vcxproj"
VCPKG_TRIPLETS = "build/vcpkg/triplets"
# A module ships its own MIT grant, the Observer API header's LGPLv3 notice with the full GPLv3
# text the LGPL incorporates, and then only that module's own third-party notices.
LICENSES = {
    "renpy": ("Observer.txt", "GPL-3.0.txt", "rpatool.txt", "serde-pickle.txt", "zlib-ng.txt",
              "YY-Thunks.txt"),
    "rpgmaker": ("Observer.txt", "GPL-3.0.txt", "rgssad.txt", "YY-Thunks.txt"),
    "zanzarah": ("Observer.txt", "GPL-3.0.txt", "YY-Thunks.txt"),
    "godot": (
        "Observer.txt", "GPL-3.0.txt", "godot.txt", "libspng.txt", "libogg.txt",
        "brotli.txt", "zstd.txt", "fastlz.txt", "zlib-ng.txt", "bcdec.txt", "dr_libs.txt",
        "qoa.txt", "zydis.txt", "zycore.txt", "mbedcrypto.txt", "YY-Thunks.txt",
    ),
}
FUZZ_TARGETS = ("pickle", "renpy", "rpgmaker", "zanzarah")
FUZZ_MAX_LENGTH = {"pickle": 262144, "renpy": 1048576, "rpgmaker": 1048576, "zanzarah": 1048576}
FUZZ_SECONDS = 60
LEAK_MODES = ("operations", "lifecycle")
LEAK_SCENARIOS = (
    "small-success", "malformed", "cancellation", "read-failure", "write-failure",
    "large-metadata", "sparse-metadata",
)
LEAK_PROBE = "leak-probe.exe"
# The fatal directory-cleanup probe owns the process boundary for one hidden Catch2 case (see
# ``src/tests/unit/image_codecs.cpp``).  The child must terminate through the fatal exit code and
# leave exactly one fixture directory behind, still holding the nested file it could not remove.
# The owned root carries a Unicode prefix so the child's own temporary paths, the profile flush and
# the Python-side cleanup round-trip are all exercised with non-ASCII path bytes.
CLEANUP_PROBE_ROOT_PREFIX = "unpackrat-cleanup-Ж漢-"
CLEANUP_PROBE_CASE = (
    "directory cleanup: an allocation failure while removing a populated directory stays fatal"
)
# Distinct from Catch2's own 0..4 exit codes and from the _Exit(86) used elsewhere, so the exact
# code proves the child reached its own terminate handler rather than a generic failure.
CLEANUP_PROBE_EXIT = 0x5A
# Every fixture directory the child leaves behind carries this marker, from
# ``test::support::unused_path("cleanup-fatal")``.
CLEANUP_PROBE_MARKER = "unpackrat-lazy-cleanup-fatal-"
CLEANUP_PROBE_TIMEOUT = 30
# The shipped binaries and dependencies use one numeric servicing version of VS2026's v145.
VS_TOOLSET_COMPONENT = "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"
TOOLSET_VERSION = "14.51"
# The family glob also matches stray files and ``.backup`` siblings; only a fully
# numeric servicing name that is a real directory is a toolset, so nothing displaces the choice.
_TOOLSET_NAME = re.compile(r"\d+(?:\.\d+)*")
# The standalone analyzer and formatter are provisioned separately from the VS LLVM toolset;
# the official LLVM 23 x64 archive has no 32-bit runtime, so these stay analyze/format-only.
LLVM_VERSION = "23.1.3"
# The official runner prints decoded child bytes; preserve native CRLF on Windows instead of
# translating its LF a second time and introducing blank diagnostic lines.
_CLANG_TIDY_RUNNER_BOOTSTRAP = (
    "import runpy,sys;sys.stdout.reconfigure(newline='');sys.stderr.reconfigure(newline='');"
    "sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')"
)
# Instrumentation uses an explicit, paired LLVM21 installation. Local tool installation is
# operator-owned; CI supplies the same verified x86/x64 pair through a same-run artifact.
LLVM_INSTRUMENTATION_ENV = "UNPACKRAT_LLVM_INSTRUMENTATION"
LLVM_INSTRUMENTATION_VERSION = "21.1.8"
LLVM_MAJOR_VERSION = "21"
_LLVM_INSTRUMENTATION_TOOLS = ("clang-cl", "lld-link", "llvm-cov", "llvm-profdata")
_LLVM_VERSION_PATTERN = re.compile(
    rf"(?<![\d.]){re.escape(LLVM_INSTRUMENTATION_VERSION)}(?![\d.])"
)
_LLVM_RUNTIMES = {
    "x86": (
        "clang_rt.profile-i386.lib", "clang_rt.fuzzer-i386.lib",
        "clang_rt.asan_dynamic-i386.lib", "clang_rt.asan_static_runtime_thunk-i386.lib",
        "clang_rt.asan_dynamic-i386.dll",
    ),
    "x64": (
        "clang_rt.profile-x86_64.lib", "clang_rt.fuzzer-x86_64.lib",
        "clang_rt.asan_dynamic-x86_64.lib", "clang_rt.asan_static_runtime_thunk-x86_64.lib",
        "clang_rt.asan_dynamic-x86_64.dll",
        "clang_rt.ubsan_standalone-x86_64.lib", "clang_rt.ubsan_standalone_cxx-x86_64.lib",
    ),
}
# ``all`` keeps the local pipeline whole; the rest let CI run one independent slice per job.
# ``native-debug``, ``native-release``, ``msvc-analysis`` and ``clang-tidy`` split the old
# monolith apart so each slice can be a separate CI job.
CI_STAGES = (
    "all", "native", "native-debug", "native-release", "msvc-analysis", "clang-tidy",
    "coverage", "asan", "ubsan", "fuzz", "leaks",
)
# ``all`` is the legacy local pipeline.  It is spelled out as its own constant rather than
# ``CI_STAGES[1:]`` so expanding the public stage list never silently grows ``all``.
_LEGACY_ALL_STAGES = ("native", "coverage", "asan", "ubsan", "fuzz", "leaks")
# The sanitizer and leak-probe steps are x64 specialists and refuse any other selection.
_X64_ONLY_STAGES = frozenset({"ubsan", "leaks"})
CPPCHECK_PLATFORMS = {"x86": ("win32W", "_M_IX86=600"), "x64": ("win64", "_M_X64=100")}
COVERAGE_IGNORED = (
    r"([\\/]src[\\/](tests|fuzz)[\\/])|([\\/]vcpkg_installed[\\/])|"
    r"([\\/]Microsoft Visual Studio[\\/])|([\\/]Windows Kits[\\/])|([\\/]third_party[\\/])"
)
_DEPENDENCY_SUFFIXES = {"ASan": "-asan", "Fuzz": "-asan", "UBSan": "-ubsan"}
_ASAN_OPTIONS = "halt_on_error=1:alloc_dealloc_mismatch=1"
_UBSAN_OPTIONS = "halt_on_error=1:print_stacktrace=1"
_ASAN_RUNTIMES = {"x86": "clang_rt.asan_dynamic-i386.dll", "x64": "clang_rt.asan_dynamic-x86_64.dll"}
_LICENSE_SEPARATOR = "=" * 72
_CPP_SUFFIXES = (".cpp", ".h")
_PS_SUFFIXES = (".ps1", ".psm1")
_IGNORED_DIRECTORIES = frozenset({".git", ".venv", "__pycache__", "out"})
# Top-level vendored trees hold pinned upstream bytes; they stay out of the source/format gates.
_IGNORED_TOP_LEVEL_DIRECTORIES = frozenset({"third_party"})
_MACHINES = {"x86": r"14C machine \(x86\)", "x64": r"8664 machine \(x64\)"}
_ALLOWED_DLLS = frozenset(
    {
        "advapi32.dll", "bcrypt.dll", "kernel32.dll", "ntdll.dll", "ole32.dll",
        "oleaut32.dll", "shell32.dll", "shlwapi.dll", "user32.dll",
    }
)
_FORBIDDEN_DLL = re.compile(
    r"^(?:vcruntime|msvcp|ucrtbase|api-ms-win-crt-|ext-ms-win-crt-|zlib|zstd|xxhash|clang_rt\.).*\.dll$",
    re.IGNORECASE,
)
# The Release variant runs the real native gates: the MSVC static analyzer and clang-tidy.
_ANALYSIS_PROPERTIES = (
    "/p:RunCodeAnalysis=true",
    "/p:unpackratRunCodeAnalysis=true",
    "/p:EnableClangTidyCodeAnalysis=true",
    "/p:unpackratEnableClangTidy=true",
    "/p:ClangTidyToolExeAdditionalOptions=-warnings-as-errors=*",
)
# The standalone ``msvc-analysis`` stage compiles only and runs the MSVC analyzer, so it turns
# clang-tidy off and asks the props layer for the PREfast compile-only pass. Disable Spectre
# library selection; the props layer retains /Qspectre on the compiler items themselves.
_MSVC_ANALYSIS_PROPERTIES = (
    "/p:unpackratCompileAnalysis=true",
    "/p:SpectreMitigation=false",
    "/p:unpackratRunCodeAnalysis=true",
    "/p:RunCodeAnalysis=true",
    "/p:unpackratEnableClangTidy=false",
    "/p:EnableClangTidyCodeAnalysis=false",
    "/p:UseMultiToolTask=true",
    "/p:EnforceProcessCountAcrossBuilds=true",
    "/p:EnableClServerMode=false",
)
# MSBuild's GetCompileCommands must carry the same clang-cl options as its ClangTidy target:
# the project props use this switch for -Qunused-arguments and the restored vcpkg -imsvc path.
_CLANG_TIDY_ANALYSIS_PROPERTIES = (
    "/p:unpackratStandaloneClangTidy=true",
    "/p:unpackratRunCodeAnalysis=false",
    "/p:RunCodeAnalysis=false",
    "/p:EnableMicrosoftCodeAnalysis=false",
    "/p:unpackratEnableClangTidy=true",
    "/p:EnableClangTidyCodeAnalysis=true",
)


class BuildError(RuntimeError):
    """A build operation could not satisfy its contract."""


@dataclass(frozen=True, slots=True)
class SourceTools:
    clang_format: Path
    cppcheck: Path
    pwsh: Path


@dataclass(frozen=True, slots=True)
class Llvm:
    directory: Path
    runtime: Path
    cov: Path
    profdata: Path


def run(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one checked child and return its ``CompletedProcess``.

    The single checked subprocess entry point: a nonzero exit raises
    ``CalledProcessError``.  ``capture`` merges the child's output into ``stdout``.
    """

    try:
        return subprocess.run(
            [os.fspath(item) for item in argv],
            cwd=None if cwd is None else os.fspath(cwd),
            env=None if env is None else {**os.environ, **env},
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        if error.stdout:
            error.add_note(error.stdout)
        raise


def _job_count(jobs: int | None) -> int:
    if jobs is None:
        return os.cpu_count() or 1
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise BuildError("jobs must be a positive integer")
    return jobs


def _require_tool(name: str) -> Path:
    found = shutil.which(name)
    if found is None:
        raise FileNotFoundError(f"{name} was not found on PATH")
    return Path(found)


def _clang_tidy() -> Path:
    """Select the separately pinned analyzer; never borrow the VS compiler toolchain."""

    return _standalone_tool(
        "UNPACKRAT_CLANG_TIDY", "clang-tidy.exe",
        rf"(?m)^\s*LLVM version {re.escape(LLVM_VERSION)}(?:\s+\([^)\n]*\))?\s*$",
    )


def _clang_format() -> Path:
    """Select the separately pinned formatter; never borrow the VS toolchain."""

    return _standalone_tool(
        "UNPACKRAT_CLANG_FORMAT", "clang-format.exe",
        rf"(?m)^\s*clang-format version {re.escape(LLVM_VERSION)}(?:\s+\([^)\n]*\))?\s*$",
    )


def _standalone_tool(override: str, executable: str, version_pattern: str) -> Path:
    """Resolve one pinned standalone LLVM tool from an override or the standard ``PATH``.

    The same selector serves the analyzer and the formatter: an explicit override names the
    exact executable (resolved so a relative override survives an MSBuild working-directory
    change), else the tool resolves from the standard ``PATH`` through :func:`_require_tool`.
    A missing file, a wrong version or an unset override fails closed; the private
    ``%LOCALAPPDATA%`` tool cache is never consulted and the tool is never installed.
    """

    override_path = os.environ.get(override)
    if override_path:
        candidate = Path(override_path)
    else:
        candidate = _require_tool(executable)
    # MSBuild runs from the repository even when the caller supplied a relative override.
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"Pinned {executable} {LLVM_VERSION} was not found: {candidate}")
    version = run([str(candidate), "--version"], capture=True).stdout or ""
    if re.search(version_pattern, version) is None:
        raise BuildError(
            f"Expected {executable} {LLVM_VERSION} at {candidate}; got {version.strip()!r}")
    return candidate


def _vs_installation() -> Path:
    """Return the installed Visual Studio root via vswhere, once per lookup."""

    base = os.environ.get("ProgramFiles(x86)")
    if not base:
        raise FileNotFoundError("ProgramFiles(x86) is not set; cannot locate vswhere.exe")
    vswhere = Path(base) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    completed = run(
        [
            str(vswhere), "-products", "*",
            "-version", "[18.0,19.0)",
            "-requires", "Microsoft.Component.MSBuild",
            "-requires", VS_TOOLSET_COMPONENT,
            "-property", "installationPath",
        ],
        capture=True,
    )
    installation = next(
        (line.strip() for line in (completed.stdout or "").splitlines() if line.strip()), None
    )
    if installation is None:
        raise FileNotFoundError("vswhere did not report a Visual Studio installation")
    return Path(installation)


def _msvc_toolset_version(installation: Path) -> str:
    """Return the numeric-maximum installed 14.51 servicing toolset; never another family."""

    root = Path(installation) / "VC" / "Tools" / "MSVC"
    versions = sorted(
        (
            child.name
            for child in root.glob(f"{TOOLSET_VERSION}.*")
            if child.is_dir() and _TOOLSET_NAME.fullmatch(child.name)
        ),
        key=lambda name: tuple(int(part) for part in name.split(".")),
    )
    if not versions:
        raise FileNotFoundError(
            f"no {TOOLSET_VERSION} MSVC toolset below {root}; the VS2026 v145 tools are required")
    return versions[-1]


def _find_msbuild() -> Path:
    directory = _vs_installation() / "MSBuild" / "Current" / "Bin"
    amd64 = directory / "amd64" / "MSBuild.exe"
    return amd64 if amd64.is_file() else directory / "MSBuild.exe"


def _find_vcpkg() -> Path:
    root = os.environ.get("VCPKG_ROOT")
    if root:
        integration = Path(root) / "scripts" / "buildsystems" / "msbuild" / "vcpkg.props"
        if integration.is_file():
            return Path(root) / "vcpkg.exe"
    found = shutil.which("vcpkg")
    if found is None:
        raise FileNotFoundError("vcpkg was not found; set VCPKG_ROOT")
    return Path(found)


def _native_tools() -> tuple[Path, Path]:
    return _find_msbuild(), _find_vcpkg()


def _llvm_home() -> Path:
    """Return the x64 compiler home from the operator's explicit paired LLVM21 root."""

    override = os.environ.get(LLVM_INSTRUMENTATION_ENV)
    if not override:
        raise FileNotFoundError(
            f"set {LLVM_INSTRUMENTATION_ENV} to an installed LLVM {LLVM_INSTRUMENTATION_VERSION} "
            "paired x86/x64 root; automatic tool installation is disabled")
    root = Path(override)
    if not root.is_absolute():
        raise FileNotFoundError(
            f"{LLVM_INSTRUMENTATION_ENV} must be an absolute path: {override}")
    return root / "x64"


def _llvm_dir() -> Path:
    return _llvm_home()


def _llvm_tool(name: str) -> Path:
    found = shutil.which(name)
    if found is not None:
        return Path(found)
    return _llvm_home() / "bin" / f"{name}.exe"


def _source_tools() -> SourceTools:
    # The format gate uses the separately pinned standalone formatter, never the VS-bundled one.
    return SourceTools(_clang_format(), _require_tool("cppcheck"), _require_tool("pwsh"))


def _verify_llvm_tool(executable: Path, name: str) -> None:
    """Require one instrumentation tool to exist and report the pinned LLVM version."""

    if not executable.is_file():
        raise FileNotFoundError(f"missing LLVM tool {name}: {executable}")
    version = run([str(executable), "--version"], capture=True).stdout or ""
    if _LLVM_VERSION_PATTERN.search(version) is None:
        raise BuildError(
            f"Expected LLVM {LLVM_INSTRUMENTATION_VERSION} for {name} at {executable}; "
            f"got {version.strip()!r}")


def _llvm(architecture: str = "x64") -> Llvm:
    directory = _llvm_dir()
    # The compiler, the coverage and profile tools must all report the same pinned version, so
    # no stage ever mixes a compiler with a coverage or profile tool from another build.
    for name in _LLVM_INSTRUMENTATION_TOOLS:
        _verify_llvm_tool(directory / "bin" / f"{name}.exe", name)
    root = directory / "lib" / "clang" / LLVM_MAJOR_VERSION
    if not (root / "lib" / "windows").is_dir():
        raise FileNotFoundError(f"missing LLVM {LLVM_MAJOR_VERSION} runtime below {root}")
    if not (root / "include" / "stddef.h").is_file():
        raise FileNotFoundError(f"missing LLVM {LLVM_MAJOR_VERSION} headers below {root}")
    # Both official distributions keep their architecture-specific runtime under their own root.
    runtime_root = directory if architecture == "x64" else directory.parent / "x86"
    runtime = runtime_root / "lib" / "clang" / LLVM_MAJOR_VERSION / "lib" / "windows"
    if not runtime.is_dir():
        raise FileNotFoundError(f"missing LLVM {architecture} runtime below {runtime_root}")
    for library in _LLVM_RUNTIMES[architecture]:
        if not (runtime / library).is_file():
            raise FileNotFoundError(f"missing LLVM {architecture} runtime library {library}")
    return Llvm(
        directory,
        runtime,
        directory / "bin" / "llvm-cov.exe",
        directory / "bin" / "llvm-profdata.exe",
    )


def _find_dumpbin() -> Path:
    found = shutil.which("dumpbin")
    if found is not None:
        return Path(found)
    matches = sorted(
        (_vs_installation() / "VC" / "Tools" / "MSVC").glob("*/bin/Host*/x64/dumpbin.exe")
    )
    if not matches:
        raise FileNotFoundError("dumpbin was not found; install the MSVC C++ toolset")
    return matches[-1]


def _find_binskimm() -> Path:
    return _require_tool("binskim")


def _find_umdh() -> Path:
    override = os.environ.get("UNPACKRAT_UMDH")
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"missing UMDH override: {path}")
        return path
    base = os.environ.get("ProgramFiles(x86)")
    if base:
        for version in ("11", "10"):
            candidate = Path(base) / "Windows Kits" / version / "Debuggers" / "x64" / "umdh.exe"
            if candidate.is_file():
                return candidate
    raise FileNotFoundError("missing UMDH: set UNPACKRAT_UMDH or install the Windows Debugging Tools")


def _asan_runtime(architecture: str) -> Path:
    msbuild = _native_tools()[0]
    installation = next(
        (path.parent for path in Path(msbuild).parents if path.name.casefold() == "msbuild"), None
    )
    if installation is None:
        raise FileNotFoundError(f"cannot locate the Visual Studio installation from {msbuild}")
    # The staged ASan runtime must come from the very toolset that produced the binaries, so
    # it uses the same selected 14.51 servicing version as the compiler.
    version = _msvc_toolset_version(installation)
    runtime = (installation / "VC" / "Tools" / "MSVC" / version / "bin" / "Hostx64"
               / architecture / _ASAN_RUNTIMES[architecture])
    if not runtime.is_file():
        raise FileNotFoundError(
            f"the {version} MSVC toolset does not ship the ASan runtime "
            f"{_ASAN_RUNTIMES[architecture]} for {architecture}: {runtime}")
    return runtime


def artifacts_root(repository: Path | str) -> Path:
    return Path(repository) / "out" / "native"


def bin_directory(repository: Path | str, architecture: str, configuration: str) -> Path:
    return artifacts_root(repository) / "bin" / architecture / configuration


def restore_root(repository: Path | str, architecture: str, configuration: str) -> Path:
    suffix = _DEPENDENCY_SUFFIXES.get(configuration, "")
    return artifacts_root(repository) / "vcpkg_installed" / f"{architecture}{suffix}"


def triplet(architecture: str, configuration: str) -> str:
    suffix = _DEPENDENCY_SUFFIXES.get(configuration, "")
    return f"unpackrat-{architecture}-windows-static{suffix}"


def _report_path(repository: Path, architecture: str, configuration: str, scope: str) -> Path:
    return artifacts_root(repository) / "reports" / "tests" / architecture / configuration / scope / "tests.xml"


def _restore(repository: Path, architecture: str, configuration: str, vcpkg: Path) -> None:
    suffix = _DEPENDENCY_SUFFIXES.get(configuration, "")
    scratch = artifacts_root(repository) / "vcpkg_scratch" / f"{architecture}{suffix}"
    # Point vcpkg at the same Visual Studio the driver builds with, through the child
    # environment only: two installed Visual Studios must never make vcpkg pick the older one.
    installation = _vs_installation()
    kept_variables = os.environ.get("VCPKG_KEEP_ENV_VARS", "").split(";")
    if not any(name.casefold() == "path" for name in kept_variables):
        kept_variables.append("PATH")
    env = {
        "VCPKG_VISUAL_STUDIO_PATH": os.fspath(installation),
        "VCPKG_PLATFORM_TOOLSET_VERSION": _msvc_toolset_version(installation),
        "VCPKG_FORCE_SYSTEM_BINARIES": "1",
        "VCPKG_KEEP_ENV_VARS": ";".join(filter(None, kept_variables)),
    }
    run(
        [
            str(vcpkg), "install",
            f"--x-install-root={restore_root(repository, architecture, configuration)}",
            f"--x-buildtrees-root={scratch / 'buildtrees'}",
            f"--x-packages-root={scratch / 'packages'}",
            "--triplet", triplet(architecture, configuration),
            f"--x-manifest-root={repository}",
            "--overlay-triplets", str(repository / "build" / "vcpkg" / "triplets"),
        ],
        cwd=repository,
        env=env,
    )


def _msbuild_command(
    repository: Path, project: Path, architecture: str, configuration: str,
    jobs: int | None, msbuild: Path, vcpkg: Path, *,
    llvm: Llvm | None = None, analysis: bool = True,
) -> tuple[str, ...]:
    """One ordinary MSBuild request for ``project``.

    ``analysis`` stays on for the default Release build, which is the analyzed configuration;
    the standalone analysis stages and the split native stages pass ``analysis=False`` to get a
    plain command with no analyzer flags and no clang-tidy lookup.
    """

    command = [
        str(msbuild), str(project), "/nologo", f"/m:{_job_count(jobs)}", "/nr:false", "/t:Build",
        f"/p:Configuration={configuration}",
        f"/p:Platform={PLATFORMS[architecture]}",
        f"/p:ArtifactsRoot={artifacts_root(repository)}{os.sep}",
        f"/p:VcpkgRoot={vcpkg.parent}",
        "/p:VcpkgManifestInstall=false",
        # Forward the exact selected servicing version for every configuration - the MSVC and
        # the ClangCL toolset alike - so every configuration uses the same compiler libraries.
        f"/p:VCToolsVersion={_msvc_toolset_version(_vs_installation())}",
    ]
    if llvm is not None:
        # ``LLVMToolsVersion`` must name the layout we point MSBuild at; otherwise
        # Microsoft.Cpp.ClangCl.Common.props derives its include version from the pinned
        # default and MSBuild fails with MSB8073.  The runtime is
        # ``<root>/lib/clang/<version>/lib/windows``, so the version is its grandparent name.
        tools_version = llvm.runtime.parent.parent.name
        command += [
            f"/p:LLVMInstallDir={llvm.directory}",
            f"/p:LLVMRuntimeDir={llvm.runtime}",
            f"/p:LLVMToolsVersion={tools_version}",
        ]
    if configuration == "Release" and analysis:
        clang_tidy = _clang_tidy()
        command += list(_ANALYSIS_PROPERTIES)
        command += [f"/p:ClangTidyToolPath={clang_tidy.parent}{os.sep}",
                    f"/p:ClangTidyToolExe={clang_tidy.name}"]
    return tuple(command)


def build(
    architecture: str, configuration: str, *,
    repository: Path | str = REPOSITORY, jobs: int | None = None, analysis: bool = True,
) -> Path:
    """Restore the pinned manifest and build ``tests.vcxproj`` for one variant."""

    repository = Path(repository).resolve()
    msbuild, vcpkg = _native_tools()
    _restore(repository, architecture, configuration, vcpkg)
    run(
        _msbuild_command(
            repository, repository / TEST_PROJECT, architecture, configuration, jobs, msbuild,
            vcpkg, analysis=analysis),
        cwd=repository,
    )
    return bin_directory(repository, architecture, configuration)


def test_command(
    repository: Path, architecture: str, configuration: str, report: Path, corpus: Path | None
) -> tuple[str, ...]:
    executable = bin_directory(repository, architecture, configuration) / "tests.exe"
    command = [str(executable)]
    if corpus is not None:
        command.append("[compatibility]")
    command += [
        "--reporter", "compact",
        "--reporter", f"JUnit::out={report}",
        "--durations", "yes",
        "--order", "lex",
    ]
    return tuple(command)


def test(
    architecture: str, configuration: str, *,
    repository: Path | str = REPOSITORY, corpus: Path | str | None = None,
    jobs: int | None = None, analysis: bool = True,
) -> tuple[Path, ...]:
    """Build and run the Catch2 suite; an optional corpus adds one compatibility pass."""

    repository = Path(repository).resolve()
    build(architecture, configuration, repository=repository, jobs=jobs, analysis=analysis)
    stage = bin_directory(repository, architecture, configuration)
    suites: list[tuple[str, Path | None]] = [("unit", None)]
    if corpus is not None:
        suites.append(("corpus", Path(corpus).resolve(strict=True)))
    reports: list[Path] = []
    for scope, corpus_path in suites:
        report = _report_path(repository, architecture, configuration, scope)
        report.parent.mkdir(parents=True, exist_ok=True)
        env = None if corpus_path is None else {"UNPACKRAT_TEST_CORPUS": str(corpus_path)}
        run(test_command(repository, architecture, configuration, report, corpus_path), cwd=stage, env=env)
        reports.append(report)
    if configuration == "Debug":
        _cleanup_fatal_probe(stage)
    return tuple(reports)


def _cleanup_fatal_probe(stage: Path | str, *, env: dict[str, str] | None = None) -> None:
    """Run the hidden fatal-cleanup case once and require its exact observable leftovers.

    The child inherits the parent process environment plus any ``env`` overrides, with ``TEMP`` and
    ``TMP`` forced onto an owned temporary root; the root is removed again on the way out, and a
    cleanup failure there propagates rather than being swallowed.  The child must terminate through
    the fatal exit code and leave exactly one marked fixture directory still holding its nested
    file, which is what a refused allocation during ``remove_all`` produces.
    """

    stage = Path(stage)
    with tempfile.TemporaryDirectory(prefix=CLEANUP_PROBE_ROOT_PREFIX) as root:
        owned = Path(root)
        child_env = {**os.environ, **(env or {})}
        child_env["TEMP"] = child_env["TMP"] = str(owned)
        child = subprocess.run(
            [str(stage / "tests.exe"), CLEANUP_PROBE_CASE],
            cwd=stage,
            env=child_env,
            capture_output=True,
            timeout=CLEANUP_PROBE_TIMEOUT,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if child.returncode != CLEANUP_PROBE_EXIT:
            raise BuildError(
                f"the cleanup fatal probe expected exit {CLEANUP_PROBE_EXIT}, got {child.returncode}"
            )
        leftovers: list[Path] = []
        for entry in owned.iterdir():
            if entry.name.startswith(CLEANUP_PROBE_MARKER):
                leftovers.append(entry)
        if len(leftovers) != 1:
            raise BuildError(
                "the cleanup fatal probe expected exactly one leftover directory, "
                f"found {len(leftovers)}"
            )
        if (leftovers[0] / "nested.bin").read_bytes() != b"nested":
            raise BuildError("the cleanup fatal probe found a leftover directory without its nested file")


def _licenses_document(repository: Path, module: str) -> bytes:
    parts = [_license_section("unpackrat", (repository / "LICENSE.txt").read_bytes())]
    for name in LICENSES[module]:
        parts.append(_license_section(name, (repository / "licenses" / name).read_bytes()))
    preamble = (
        f"This file accompanies the unpackrat plugin {module}.so and describes the\n"
        "licenses that apply to it. unpackrat is published under the MIT license; the\n"
        "sections below reproduce its own grant, the Observer API header's LGPLv3 notice\n"
        "with the full GPLv3 text, and the third-party notices relevant to this module.\n"
    )
    return preamble.encode("utf-8") + b"\n" + b"\n".join(parts)


def _license_section(title: str, text: bytes) -> bytes:
    heading = f"{_LICENSE_SEPARATOR}\n{title}\n{_LICENSE_SEPARATOR}\n\n".encode("utf-8")
    body = text if text.endswith(b"\n") else text + b"\n"
    return heading + body


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            archive.writestr(name, members[name])


def read_version(repository: Path | str, module: str) -> str:
    path = Path(repository) / "src" / "modules" / module / "VERSION"
    try:
        version = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise BuildError(f"module version file is unreadable: {path}") from error
    return version


def package(
    *, repository: Path | str = REPOSITORY, output: Path | str = OUT_PACKAGES,
    modules: Sequence[str] | None = None,
) -> tuple[Path, ...]:
    """Write ``<module>-<version>-bin-<arch>.zip`` and ``<module>-<version>-pdb.zip`` archives."""

    repository = Path(repository).resolve()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    selected = MODULES if modules is None else tuple(modules)
    created: list[Path] = []
    for module in selected:
        version = read_version(repository, module)
        module_dir = repository / "src" / "modules" / module
        common = {
            "observer_user.ini": (module_dir / "observer_user.ini").read_bytes(),
            "ChangeLog.en.txt": (module_dir / "ChangeLog.en.txt").read_bytes(),
            "ChangeLog.ru.txt": (module_dir / "ChangeLog.ru.txt").read_bytes(),
            "LICENSES.txt": _licenses_document(repository, module),
        }
        for architecture in ARCHITECTURES:
            binary = bin_directory(repository, architecture, "Release") / MODULE_BINARIES[module]
            members = {MODULE_BINARIES[module]: binary.read_bytes(), **common}
            path = output / f"{module}-{version}-bin-{architecture}.zip"
            _write_zip(path, members)
            created.append(path)
        symbols = {
            f"{architecture}/{module}.pdb":
                (bin_directory(repository, architecture, "Release") / f"{module}.pdb").read_bytes()
            for architecture in ARCHITECTURES
        }
        path = output / f"{module}-{version}-pdb.zip"
        _write_zip(path, symbols)
        created.append(path)
    return tuple(created)


def _metric(totals: object, name: str) -> tuple[int, int]:
    try:
        metric = totals[name]  # type: ignore[index]
        count, covered = metric["count"], metric["covered"]
    except (KeyError, TypeError) as error:
        raise ValueError("malformed LLVM coverage totals") from error
    if (
        isinstance(count, bool) or not isinstance(count, int) or count < 0
        or isinstance(covered, bool) or not isinstance(covered, int) or covered < 0
        or covered > count
    ):
        raise ValueError("malformed LLVM coverage totals")
    return count, covered


def coverage_complete(document: object) -> bool:
    """Return ``True`` only for nonempty, exact 100% first-party line and branch coverage."""

    try:
        totals = document["data"][0]["totals"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("malformed LLVM coverage report") from error
    for name in ("lines", "branches"):
        count, covered = _metric(totals, name)
        if count == 0:
            raise ValueError(f"no first-party {name} in LLVM coverage report")
        if covered != count:
            raise ValueError(f"first-party {name} coverage is {covered}/{count}, required 100%")
    return True


def _source_files(repository: Path, suffixes: tuple[str, ...]) -> tuple[Path, ...]:
    found: list[Path] = []
    for directory, directories, names in repository.walk():
        ignored = _IGNORED_DIRECTORIES
        if directory == repository:
            ignored = _IGNORED_DIRECTORIES | _IGNORED_TOP_LEVEL_DIRECTORIES
        directories[:] = sorted(set(directories) - ignored)
        for name in names:
            if Path(name).suffix in suffixes:
                found.append(directory / name)
    return tuple(sorted(found, key=str))


def _format_check(repository: Path, tools: SourceTools) -> None:
    sources = _source_files(repository, _CPP_SUFFIXES)
    if sources:
        run([str(tools.clang_format), "--dry-run", "--Werror", *(str(path) for path in sources)], cwd=repository)


def _cppcheck(
    repository: Path, architecture: str, configuration: str, tools: SourceTools
) -> None:
    platform, define = CPPCHECK_PLATFORMS[architecture]
    # The dependencies restored for this variant must be on the include path so the third-party
    # headers resolve. Restored and bundled third_party headers are exempt; our adapters remain checked.
    triplet_name = triplet(architecture, configuration)
    include_dir = restore_root(repository, architecture, configuration) / triplet_name / "include"
    report = artifacts_root(repository) / "analysis" / "cppcheck.sarif"
    report.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            str(tools.cppcheck), str(repository / "src"), "--std=c++23",
            f"--platform={platform}", "-DWIN32=1", "-D_WIN32=1", "-DUNICODE=1", "-D_UNICODE=1",
            f"-D{define}", f"-I{repository / 'src'}", f"-I{include_dir}",
            "--enable=warning,style,performance,portability", "--check-level=exhaustive",
            "--inconclusive", "--inline-suppr",
            "--suppress=missingIncludeSystem", "--suppress=uninitMemberVarNoCtor:src/api.h",
            f"--suppress=*:*\\{triplet_name}\\include\\*", "--suppress=functionStatic",
            "--suppress=*:third_party/*",
            f"--relative-paths={repository}", "--error-exitcode=1",
            "--output-format=sarif", f"--output-file={report}",
        ],
        cwd=repository,
    )


def _pssa(repository: Path, tools: SourceTools) -> None:
    sources = _source_files(repository, _PS_SUFFIXES)
    if sources:
        run(
            [
                str(tools.pwsh), "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
                str(repository / "build" / "checks" / "psscriptanalyzer.ps1"),
                "-Settings", str(repository / "build" / "PSScriptAnalyzerSettings.psd1"),
                *(str(path) for path in sources),
            ],
            cwd=repository,
        )


def _contracts(repository: Path, tools: SourceTools) -> None:
    for script in sorted((repository / "build" / "tests").glob("*.Tests.ps1")):
        run([str(tools.pwsh), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)], cwd=repository)


def _python_coverage(repository: Path) -> None:
    build_root = repository / "build"
    run(
        [sys.executable, "-m", "coverage", "run", "--rcfile", str(build_root / "pyproject.toml"),
         "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
        cwd=build_root,
    )
    # The Cobertura XML is written before the report gate: ``coverage xml`` inherits the
    # configured ``fail_under = 100``, so it still emits the report and then fails the run.
    report = artifacts_root(repository) / "coverage" / "python" / "coverage.xml"
    report.parent.mkdir(parents=True, exist_ok=True)
    run(
        [sys.executable, "-m", "coverage", "xml", "--rcfile", str(build_root / "pyproject.toml"),
         "-o", str(report)],
        cwd=build_root,
    )
    run(
        [sys.executable, "-m", "coverage", "report", "--rcfile", str(build_root / "pyproject.toml"),
         "--fail-under=100"],
        cwd=build_root,
    )


def ci_source(*, repository: Path | str = REPOSITORY, jobs: int | None = None) -> Path:
    """Restore the x64 dependencies, then run the format, Cppcheck, PSScriptAnalyzer,
    contract and Python coverage checks."""

    repository = Path(repository).resolve()
    _job_count(jobs)
    msbuild, vcpkg = _native_tools()
    tools = _source_tools()
    _restore(repository, "x64", "Debug", vcpkg)
    _format_check(repository, tools)
    _cppcheck(repository, "x64", "Debug", tools)
    _pssa(repository, tools)
    _contracts(repository, tools)
    _python_coverage(repository)
    return repository


def _coverage_export_arguments(
    repository: Path, architecture: str, profile: Path, llvm: Llvm
) -> list[str]:
    stage = bin_directory(repository, architecture, "Coverage")
    command = [
        str(llvm.cov), "export", str(stage / "tests.exe"),
        "--instr-profile", str(profile),
        "--ignore-filename-regex", COVERAGE_IGNORED,
    ]
    for module in MODULES:
        command += ["--object", str(stage / MODULE_BINARIES[module])]
    return command


def _coverage_export(repository: Path, architecture: str, profile: Path, llvm: Llvm) -> tuple[str, ...]:
    """The authoritative summary JSON export; ``--summary-only`` never appears on the LCOV export."""

    command = _coverage_export_arguments(repository, architecture, profile, llvm)
    command.append("--summary-only")
    return tuple(command)


def _coverage_lcov_export(repository: Path, architecture: str, profile: Path, llvm: Llvm) -> tuple[str, ...]:
    """The full LCOV export carrying both line and branch records over the same objects."""

    command = _coverage_export_arguments(repository, architecture, profile, llvm)
    command.append("--format=lcov")
    return tuple(command)


def _coverage(
    repository: Path, architecture: str, msbuild: Path, vcpkg: Path, llvm: Llvm, *,
    corpus: Path | str | None = None, jobs: int | None = None,
) -> Path:
    _restore(repository, architecture, "Coverage", vcpkg)
    run(
        _msbuild_command(repository, repository / TEST_PROJECT, architecture, "Coverage", jobs, msbuild, vcpkg, llvm=llvm),
        cwd=repository,
    )
    stage = bin_directory(repository, architecture, "Coverage")
    profiles = artifacts_root(repository) / "coverage" / architecture
    suites: list[tuple[str, Path | None]] = [("unit", None)]
    if corpus is not None:
        suites.append(("corpus", Path(corpus).resolve(strict=True)))
    raw: list[Path] = []
    for scope, corpus_path in suites:
        scope_dir = profiles / scope
        scope_dir.mkdir(parents=True, exist_ok=True)
        for stale in scope_dir.glob("*.profraw"):
            stale.unlink()
        env = {"LLVM_PROFILE_FILE": str(scope_dir / "coverage-%m-%p.profraw")}
        if corpus_path is not None:
            env["UNPACKRAT_TEST_CORPUS"] = str(corpus_path)
        report = _report_path(repository, architecture, "Coverage", scope)
        report.parent.mkdir(parents=True, exist_ok=True)
        run(test_command(repository, architecture, "Coverage", report, corpus_path), cwd=stage, env=env)
        # The hidden fatal case is excluded from the regular suite, so the unit scope runs it under
        # the same profile file; its flushed coverage joins the enumeration below.  The corpus scope
        # never repeats the probe.
        if scope == "unit":
            _cleanup_fatal_probe(stage, env=env)
        produced = sorted(scope_dir.glob("*.profraw"))
        if not produced:
            raise BuildError("coverage suite produced no LLVM raw profiles")
        raw.extend(produced)
    profile = profiles / "coverage.profdata"
    run([str(llvm.profdata), "merge", "-sparse", *(str(path) for path in raw), "-o", str(profile)])
    document = json.loads(run(_coverage_export(repository, architecture, profile, llvm), capture=True).stdout)
    lcov = run(_coverage_lcov_export(repository, architecture, profile, llvm), capture=True).stdout
    # Write both reports before the gate so incomplete coverage stays reviewable when the run fails.
    summary = profiles / "coverage.json"
    summary.write_text(json.dumps(document), encoding="utf-8")
    (profiles / "coverage.lcov").write_text(lcov, encoding="utf-8")
    coverage_complete(document)
    return summary


def _sanitizer(
    repository: Path, sanitizer: str, architecture: str, msbuild: Path, vcpkg: Path,
    llvm: Llvm | None, *, jobs: int | None = None,
) -> Path:
    configuration = "ASan" if sanitizer == "asan" else "UBSan"
    _restore(repository, architecture, configuration, vcpkg)
    # ASan is the MSVC 14.51 dynamic ASan build and takes no LLVM toolchain; only UBSan forwards
    # the matched LLVM 21 runtime to MSBuild.
    props = None if sanitizer == "asan" else llvm
    run(
        _msbuild_command(repository, repository / TEST_PROJECT, architecture, configuration, jobs, msbuild, vcpkg, llvm=props),
        cwd=repository,
    )
    stage = bin_directory(repository, architecture, configuration)
    if sanitizer == "asan":
        stage.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_asan_runtime(architecture), stage / _ASAN_RUNTIMES[architecture])
        env = {"ASAN_OPTIONS": _ASAN_OPTIONS}
    else:
        env = {"UBSAN_OPTIONS": _UBSAN_OPTIONS}
    report = _report_path(repository, architecture, configuration, sanitizer)
    report.parent.mkdir(parents=True, exist_ok=True)
    if sanitizer == "asan" and architecture == "x86":
        shard_count = 4
        first_report = report.with_name("tests-shard-0.xml")
        for index in range(shard_count):
            shard_report = report.with_name(f"tests-shard-{index}.xml")
            run(
                (*test_command(repository, architecture, configuration, shard_report, None),
                 "--shard-count", str(shard_count), "--shard-index", str(index), "--rng-seed", "1"),
                cwd=stage, env=env,
            )
        return first_report
    run(test_command(repository, architecture, configuration, report, None), cwd=stage, env=env)
    return report


def _seed_corpus(repository: Path, target: str, directory: Path) -> tuple[Path, ...]:
    source = repository / "src" / "fuzz" / "corpus" / target
    if not source.is_dir():
        raise FileNotFoundError(f"no checked-in fuzzer seeds for {target}")
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
    seeded: list[Path] = []
    for seed in sorted(source.iterdir()):
        if not seed.is_file():
            continue
        if seed.suffix == ".hex":
            destination = directory / seed.stem
            destination.write_bytes(bytes.fromhex("".join(seed.read_text(encoding="ascii").split())))
        else:
            destination = directory / seed.name
            shutil.copyfile(seed, destination)
        seeded.append(destination)
    if not seeded:
        raise FileNotFoundError(f"no checked-in fuzzer seeds for {target}")
    return tuple(seeded)


def _fuzz(
    repository: Path, architecture: str, msbuild: Path, vcpkg: Path, llvm: Llvm, *,
    jobs: int | None = None, seconds: int = FUZZ_SECONDS,
) -> Path:
    _restore(repository, architecture, "Fuzz", vcpkg)
    for target in FUZZ_TARGETS:
        project = repository / "build" / "projects" / f"fuzz-{target}.vcxproj"
        run(
            _msbuild_command(repository, project, architecture, "Fuzz", jobs, msbuild, vcpkg, llvm=llvm),
            cwd=repository,
        )
        executable = bin_directory(repository, architecture, "Fuzz") / f"fuzz-{target}.exe"
        runtime_name = _ASAN_RUNTIMES[architecture]
        executable.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(llvm.runtime / runtime_name, executable.parent / runtime_name)
        work = artifacts_root(repository) / "fuzz" / architecture / target
        seeds = _seed_corpus(repository, target, work / "seeds")
        length = FUZZ_MAX_LENGTH[target]
        if architecture == "x64":
            arguments = [str(executable), str(work / "seeds"), f"-max_total_time={seconds}", f"-max_len={length}"]
        else:
            arguments = [str(executable), *(str(path) for path in seeds), f"-max_len={length}"]
        run([*arguments, "-rss_limit_mb=1024"], env={"ASAN_OPTIONS": _ASAN_OPTIONS})
    return artifacts_root(repository) / "fuzz" / architecture


def _prepare_leak_probe(
    repository: Path, msbuild: Path, vcpkg: Path, *, jobs: int | None = None,
    analysis: bool = True,
) -> Path:
    """Build the x64 Release UMDH probe once, sharing the Release build's own analysis flags."""

    _restore(repository, "x64", "Release", vcpkg)
    run(
        _msbuild_command(
            repository, repository / LEAK_PROBE_PROJECT, "x64", "Release", jobs, msbuild, vcpkg,
            analysis=analysis),
        cwd=repository,
    )
    return bin_directory(repository, "x64", "Release") / LEAK_PROBE


def _leak_sources(repository: Path) -> tuple[Path, ...]:
    """Return the ready x64 Release leak binaries and their symbols, or fail closed."""

    stage = bin_directory(repository, "x64", "Release")
    binaries = (stage / LEAK_PROBE, *(stage / MODULE_BINARIES[module] for module in MODULES))
    missing = [path for path in binaries if not path.is_file()]
    if missing:
        raise BuildError("the leak stage needs the x64 Release binaries: " + ", ".join(str(path) for path in missing))
    symbols = [path.with_suffix(".pdb") for path in binaries]
    missing = [path for path in symbols if not path.is_file()]
    if missing:
        raise BuildError("the leak stage needs the x64 Release symbols: " + ", ".join(str(path) for path in missing))
    return binaries


def _leak_measure(repository: Path) -> Path:
    """Stage the ready x64 Release binaries and run every UMDH mode/scenario measurement.

    This stage measures an already-built tree: it never restores vcpkg, invokes MSBuild,
    runs BinSkim or resolves LLVM.  Only the preinstalled UMDH tool and the Python worker
    are needed.
    """

    binaries = _leak_sources(repository)
    output = artifacts_root(repository) / "leaks"
    build_root = repository / "build"
    umdh = str(_find_umdh())

    def leak(arguments: Sequence[str], scope: str) -> None:
        directory = output / scope
        directory.mkdir(parents=True, exist_ok=True)
        run([sys.executable, "-m", "leaks", *arguments], cwd=build_root, env={"UNPACKRAT_OUT_DIR": str(directory)})

    leak(["setup", *(str(path) for path in binaries)], "setup")
    prepared = str(output / "setup")
    for mode in LEAK_MODES:
        for scenario in LEAK_SCENARIOS:
            leak(["measure", prepared, umdh, mode, scenario, "8", "100", "3", "0"], f"{mode}/{scenario}")
    return output


def require_release_pe(architecture: str, headers: str, dependents: str, exports: str) -> None:
    """Gate one shipped module DLL: right machine, no foreign DLL, exact two exports."""

    try:
        machine = _MACHINES[architecture]
    except KeyError as error:
        raise BuildError(f"unsupported machine architecture: {architecture}") from error
    if re.search(machine, headers) is None:
        raise BuildError(f"wrong PE machine for {architecture}")
    dependencies = {
        match.group(1)
        for line in dependents.splitlines()
        if (match := re.fullmatch(r"\s+([A-Za-z0-9._-]+\.dll)\s*", line))
    }
    unexpected = sorted(
        dependency
        for dependency in dependencies
        if _FORBIDDEN_DLL.match(dependency)
        or (
            dependency.casefold() not in _ALLOWED_DLLS
            and re.match(r"^(?:api|ext)-ms-win-.*\.dll$", dependency, re.IGNORECASE) is None
        )
    )
    if unexpected:
        raise BuildError("unexpected DLL dependencies: " + ", ".join(unexpected))
    actual = {
        match.group(1)
        for line in exports.splitlines()
        if (match := re.match(r"^\s+\d+\s+[0-9A-F]+\s+[0-9A-F]+\s+(\S+)", line))
    }
    if actual != {"LoadSubModule", "UnloadSubModule"}:
        raise BuildError("unexpected exports: " + ", ".join(sorted(actual)))


def _audit(repository: Path, architecture: str) -> None:
    dumpbin = str(_find_dumpbin())
    stage = bin_directory(repository, architecture, "Release")
    for module in MODULES:
        binary = stage / MODULE_BINARIES[module]
        headers = run([dumpbin, "/headers", str(binary)], capture=True).stdout
        dependents = run([dumpbin, "/dependents", str(binary)], capture=True).stdout
        exports = run([dumpbin, "/exports", str(binary)], capture=True).stdout
        require_release_pe(architecture, headers, dependents, exports)


def require_clean_binskim(document: dict[str, Any]) -> None:
    """Fail on any reported BinSkim error or warning but for the one documented BA2027 warning.

    A result that omits its own ``level`` follows the rule's ``defaultConfiguration`` level, so a
    rule declared ``note`` stays clean while one declared ``error`` still fails; only a BA2027
    *warning* is exempt.
    """

    findings: list[str] = []
    for run_ in document.get("runs") or []:
        defaults = {
            rule.get("id"): rule.get("defaultConfiguration", {}).get("level", "warning")
            for rule in (run_.get("tool") or {}).get("driver", {}).get("rules") or []
        }
        for result in run_.get("results") or []:
            rule = result.get("ruleId")
            level = result.get("level", defaults.get(rule, "warning"))
            if level in ("error", "warning") and not (level == "warning" and rule == "BA2027"):
                findings.append(str(rule))
    if findings:
        raise BuildError("BinSkim reported unapproved findings: " + ", ".join(findings))


def _binskim(repository: Path, architecture: str) -> None:
    binskim = str(_find_binskimm())
    stage = bin_directory(repository, architecture, "Release")
    report = artifacts_root(repository) / "analysis" / f"binskim-{architecture}.sarif"
    report.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            binskim, "analyze",
            *(str(stage / MODULE_BINARIES[module]) for module in MODULES),
            "--output", str(report),
        ],
        cwd=repository,
    )
    require_clean_binskim(json.loads(report.read_text(encoding="utf-8")))


def _native(
    repository: Path, architectures: Sequence[str], msbuild: Path, vcpkg: Path,
    corpus: Path | str | None, jobs: int | None,
) -> None:
    """The Debug and Release suites, the x64 leak probe, then the dumpbin and BinSkim audits."""

    for architecture in architectures:
        test(architecture, "Debug", repository=repository, corpus=corpus, jobs=jobs)
        test(architecture, "Release", repository=repository, corpus=corpus, jobs=jobs)
    if "x64" in architectures:
        _prepare_leak_probe(repository, msbuild, vcpkg, jobs=jobs)
    for architecture in architectures:
        _audit(repository, architecture)
        _binskim(repository, architecture)


def _analysis_projects(repository: Path, architecture: str) -> tuple[Path, ...]:
    """The standalone analyzer project set: every module, the tests and the x64 leak probe."""

    projects = [repository / "build" / "projects" / f"{module}.vcxproj" for module in MODULES]
    projects.append(repository / TEST_PROJECT)
    if architecture == "x64":
        projects.append(repository / LEAK_PROBE_PROJECT)
    return tuple(projects)


def _analysis_command(
    repository: Path, project: Path, architecture: str, msbuild: Path, vcpkg: Path, *,
    tidy: bool, clang_tidy: Path | None, jobs: int | None,
) -> tuple[str, ...]:
    """One standalone analyzer request: compile-only, single-project, references off.

    The command starts from the plain Release command (no analyzer flags, no clang-tidy
    lookup), retargets the build at ``ClCompile`` or ``GetCompileCommands`` and uses one
    MSBuild node. MSVC analysis delegates bounded per-file parallelism to MultiToolTask.
    """

    command = list(_msbuild_command(
        repository, project, architecture, "Release", 1, msbuild, vcpkg, analysis=False))
    command[command.index("/t:Build")] = "/t:GetCompileCommands" if tidy else "/t:ClCompile"
    command.append("/p:BuildProjectReferences=false")
    if tidy:
        command += list(_CLANG_TIDY_ANALYSIS_PROPERTIES)
        command += ["/getTargetResult:GetCompileCommands", "/getProperty:IncludePath"]
    else:
        command += list(_MSVC_ANALYSIS_PROPERTIES)
        command.append(f"/p:MultiProcMaxCount={_job_count(jobs)}")
    return tuple(command)


def _tidy_database(output: str, directory: Path, *, compiler_override: Path | None = None) -> str:
    """Save MSBuild's exact compiler options as a standard compilation database."""

    result = json.loads(output)
    include = result["Properties"]["IncludePath"]
    items = result["TargetResults"]["GetCompileCommands"]["Items"]
    if not isinstance(include, str) or not include or not isinstance(items, list) or not items:
        raise BuildError("MSBuild returned incomplete clang-tidy metadata")
    commands: list[dict[str, str]] = []
    for item in items:
        compiler = item["ToolPath"]
        working = item["WorkingDirectory"]
        options = item["Identity"]
        files = item["Files"]
        if not all(isinstance(value, str) and value for value in (compiler, working, options, files)):
            raise BuildError("MSBuild returned an incomplete compile command")
        sources = files.split(";")
        if not Path(compiler).is_absolute() or not Path(working).is_absolute() or any(
            not source or not Path(source).is_absolute() for source in sources
        ):
            raise BuildError("MSBuild returned non-absolute compile paths")
        for source in sources:
            commands.append({
                "directory": working,
                "file": source,
                "command": subprocess.list2cmdline([str(compiler_override or compiler)]) + " " + options + " "
                + ("/clang:-frewrite-includes " if compiler_override else "")
                + subprocess.list2cmdline([source]),
            })
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "compile_commands.json").write_text(json.dumps(commands), encoding="utf-8")
    return include


def _analyze(
    repository: Path, architecture: str, msbuild: Path, vcpkg: Path, jobs: int | None, *,
    tidy: bool,
) -> None:
    """Run the MSVC analyzer or clang-tidy over one architecture's project set.

    Each selected architecture restores the Release triplets once so the headers every project
    needs are present; only the tidy pass resolves the separately pinned clang-tidy.
    """

    marker = (artifacts_root(repository) / "analysis" / f"clang-tidy-{architecture}.complete"
              if tidy else None)
    if marker is not None:
        marker.unlink(missing_ok=True)
    _restore(repository, architecture, "Release", vcpkg)
    clang_tidy = _clang_tidy() if tidy else None
    script = clang_tidy.with_name("run-clang-tidy") if clang_tidy is not None else None
    if script is not None and not script.is_file():
        raise FileNotFoundError(f"run-clang-tidy was not found beside {clang_tidy}")
    cache_path = os.environ.get("UNPACKRAT_CLANG_TIDY_CACHE") if tidy else None
    cache_bridge = Path(cache_path) if cache_path else None
    compiler_override = clang_tidy.with_name("clang-cl.exe") if cache_bridge else None
    if cache_bridge is not None and (not cache_bridge.is_absolute() or not cache_bridge.is_file()):
        raise FileNotFoundError(f"clang-tidy cache bridge was not found: {cache_bridge}")
    if compiler_override is not None and not compiler_override.is_file():
        raise FileNotFoundError(f"Pinned clang-cl was not found beside {clang_tidy}")
    first_failure: subprocess.CalledProcessError | None = None
    for project in _analysis_projects(repository, architecture):
        result = run(
            _analysis_command(
                repository, project, architecture, msbuild, vcpkg,
                tidy=tidy, clang_tidy=clang_tidy, jobs=jobs),
            cwd=repository,
            capture=tidy,
        )
        if tidy:
            directory = (artifacts_root(repository) / "obj" / architecture / "Release"
                         / project.stem / f"{project.stem}.ClangTidy")
            include = _tidy_database(result.stdout, directory, compiler_override=compiler_override)
            try:
                run((sys.executable, "-c", _CLANG_TIDY_RUNNER_BOOTSTRAP, str(script),
                     "-p", str(directory), "-j", str(_job_count(jobs)),
                     "-clang-tidy-binary", str(cache_bridge or clang_tidy), "-warnings-as-errors=*"),
                    cwd=repository, env={"INCLUDE": include})
            except subprocess.CalledProcessError as failure:
                if first_failure is None:
                    first_failure = failure
    if marker is not None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("complete\n", encoding="utf-8")
    if first_failure is not None:
        raise first_failure


def _runtime(llvm: dict[str, Llvm], architecture: str) -> Llvm:
    """Resolve an architecture's LLVM tool-chain once and remember it for the whole run."""

    if architecture not in llvm:
        llvm[architecture] = _llvm(architecture)
    return llvm[architecture]


def _dispatch(
    stage: str, repository: Path, architectures: Sequence[str], corpus: Path | str | None, jobs: int | None,
    llvm: dict[str, Llvm],
) -> None:
    """Run one CI stage by plain dispatch; there is no dependency graph to evaluate.

    ``llvm`` is the run-wide cache of resolved LLVM tool-chains, so each architecture's runtime
    is resolved once and forwarded unchanged to every stage that needs it.
    """

    if stage == "leaks":
        _leak_measure(repository)
        return
    msbuild, vcpkg = _native_tools()
    if stage == "native":
        _native(repository, architectures, msbuild, vcpkg, corpus, jobs)
        return
    if stage == "native-debug":
        for architecture in architectures:
            test(
                architecture, "Debug", repository=repository, corpus=corpus, jobs=jobs,
                analysis=False)
        return
    if stage == "native-release":
        for architecture in architectures:
            test(
                architecture, "Release", repository=repository, corpus=corpus, jobs=jobs,
                analysis=False)
        if "x64" in architectures:
            _prepare_leak_probe(repository, msbuild, vcpkg, jobs=jobs, analysis=False)
        for architecture in architectures:
            _audit(repository, architecture)
            _binskim(repository, architecture)
        return
    if stage == "msvc-analysis":
        for architecture in architectures:
            _analyze(repository, architecture, msbuild, vcpkg, jobs, tidy=False)
        return
    if stage == "clang-tidy":
        for architecture in architectures:
            _analyze(repository, architecture, msbuild, vcpkg, jobs, tidy=True)
        return
    if stage == "coverage":
        for architecture in architectures:
            _coverage(repository, architecture, msbuild, vcpkg, _runtime(llvm, architecture), corpus=corpus, jobs=jobs)
    elif stage == "asan":
        for architecture in architectures:
            # The MSVC 14.51 dynamic ASan build needs no LLVM 21 instrumentation toolchain, so
            # this stage never has to resolve one.
            _sanitizer(repository, "asan", architecture, msbuild, vcpkg, None, jobs=jobs)
    elif stage == "ubsan":
        _sanitizer(repository, "ubsan", "x64", msbuild, vcpkg, _runtime(llvm, "x64"), jobs=jobs)
    else:
        for architecture in architectures:
            _fuzz(repository, architecture, msbuild, vcpkg, _runtime(llvm, architecture), jobs=jobs)


def ci_arch(
    architectures: Sequence[str], *, stage: str = "all", repository: Path | str = REPOSITORY,
    corpus: Path | str | None = None, jobs: int | None = None,
) -> Path:
    """Run the real per-architecture CI, split into independent stages.

    ``all`` (the default) runs every stage in the fixed order native, coverage, asan, ubsan,
    fuzz and leaks, preparing the Release tree and the leak probe once.  A single stage runs
    only itself, so ``native-debug``, ``native-release``, ``msvc-analysis`` and ``clang-tidy``
    each drive just their own slice; the ``ubsan`` and ``leaks`` stages are the x64 specialists
    and refuse any other selection up front.
    """

    repository = Path(repository).resolve()
    _job_count(jobs)
    if stage not in CI_STAGES:
        raise BuildError(f"unknown CI stage: {stage}")
    selected = tuple(architectures)
    if stage in _X64_ONLY_STAGES and selected != ("x64",):
        raise BuildError(f"the {stage} stage is x64 only")
    names = _LEGACY_ALL_STAGES if stage == "all" else (stage,)
    llvm: dict[str, Llvm] = {}
    for name in names:
        if name in _X64_ONLY_STAGES and "x64" not in selected:
            continue
        _dispatch(name, repository, selected, corpus, jobs, llvm)
    return repository


def _selection(choices: tuple[str, ...]):
    lookup = {item.casefold(): item for item in choices}

    def parse(value: str) -> tuple[str, ...]:
        parts = tuple(item.strip() for item in value.split(","))
        if not all(parts):
            raise argparse.ArgumentTypeError(f"expected all or comma-separated: {','.join(choices)}")
        if any(item.casefold() == "all" for item in parts):
            if len(parts) != 1:
                raise argparse.ArgumentTypeError("'all' must be selected on its own")
            return choices
        try:
            return tuple(dict.fromkeys(lookup[item.casefold()] for item in parts))
        except KeyError as error:
            raise argparse.ArgumentTypeError(f"expected all or comma-separated: {','.join(choices)}") from error

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="unpackrat-build")
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build")
    test_parser = commands.add_parser("test")
    source_parser = commands.add_parser("ci-source")
    arch_parser = commands.add_parser("ci-arch")
    package_parser = commands.add_parser("package")
    for command in (build_parser, test_parser, source_parser, arch_parser, package_parser):
        command.add_argument("-Repository", "--repository", type=Path, default=REPOSITORY)
        command.add_argument("-Jobs", "--jobs", type=int, default=None)
    for command in (build_parser, test_parser, arch_parser):
        command.add_argument("-Arch", "--arch", dest="architecture", type=_selection(ARCHITECTURES), default=("x64",))
    arch_parser.add_argument("-Stage", "--stage", dest="stage", choices=CI_STAGES, default="all")
    for command in (build_parser, test_parser):
        command.add_argument("-Config", "--config", dest="configuration", type=_selection(CONFIGURATIONS), default=("Debug",))
    for command in (test_parser, arch_parser):
        command.add_argument("-Corpus", "--corpus", type=Path, default=None)
    package_parser.add_argument("-Output", "--output", type=Path, default=OUT_PACKAGES)
    package_parser.add_argument("-Module", "--module", dest="module", type=_selection(MODULES), default=MODULES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command == "build":
        for architecture in args.architecture:
            for configuration in args.configuration:
                build(architecture, configuration, repository=args.repository, jobs=args.jobs)
    elif args.command == "test":
        for architecture in args.architecture:
            for configuration in args.configuration:
                test(
                    architecture, configuration,
                    repository=args.repository, corpus=args.corpus, jobs=args.jobs,
                )
    elif args.command == "ci-source":
        ci_source(repository=args.repository, jobs=args.jobs)
    elif args.command == "ci-arch":
        ci_arch(
            args.architecture, stage=args.stage,
            repository=args.repository, corpus=args.corpus, jobs=args.jobs,
        )
    else:
        package(repository=args.repository, output=args.output, modules=args.module)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

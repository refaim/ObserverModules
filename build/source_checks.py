"""Direct source-format/analyzer checks and compiler analysis for unpackrat.

These functions are the plain-command successors to ``graphs/source.py`` and
``graphs/analysis.py``.  They compose the repository's existing validators --
clang-format, Cppcheck, PSScriptAnalyzer, the ``*.Tests.ps1`` contracts, MSVC
``/analyze`` and clang-tidy -- and reuse the ``core.sarif`` command line for
normalization and the warning/error gate.  There is no graph, cache, scheduler
or discovery compile: ``build/native.py`` restores the pinned vcpkg tree and
MSBuild owns project evaluation and incremental state, while every report lands
under one explicit output directory that the caller owns.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import native
from core.toolchain import discover_msvc_toolchain


Runner = native.Runner

_CPP_SUFFIXES = frozenset({".cpp", ".h", ".hpp"})
_POWERSHELL_SUFFIXES = frozenset({".ps1", ".psm1"})
# Cppcheck needs the platform word and the target architecture macro, exactly as the former
# per-architecture graph nodes supplied them.
_CPPCHECK = {
    "x86": ("win32W", "_M_IX86=600"),
    "x64": ("win64", "_M_X64=100"),
    "arm64": ("win64", "_M_ARM64=1"),
}
# A fuzz project links the LLVM ASan/libFuzzer archives and has no ordinary entry point, so the
# analyzer compiles its translation units with the compile-only target instead of rebuilding.
_FUZZ_PREFIX = "fuzz-"
_COMPILE_TARGET = "/t:ClCompile"
_REBUILD_TARGET = "/t:Rebuild"
# Transient build state, VCS metadata and the local virtualenv are never sources.
_IGNORED_DIRECTORIES = frozenset({".git", ".venv", "__pycache__", "out"})
_PS_SCRIPT = Path(__file__).resolve().parent / "checks" / "psscriptanalyzer.ps1"

# PSScriptAnalyzer severities map onto SARIF levels; any other severity is informational.
_SARIF_LEVELS = {"Error": "error", "Warning": "warning"}

_PSSA_QUERY = """
$module = Get-Module -ListAvailable PSScriptAnalyzer |
    Sort-Object Version -Descending |
    Select-Object -First 1
if (-not $module) { throw 'PSScriptAnalyzer was not found' }
$module.Path
"""


@dataclass(frozen=True, slots=True)
class CheckTools:
    """The external tools the direct source checks invoke.

    ``llvm_dir`` is the compiler-analysis LLVM installation and stays ``None`` for the
    format/policy checks, which only need a standalone formatter and the native MSBuild locator.
    """

    pwsh: Path
    clang_format: Path
    cppcheck: Path
    psscriptanalyzer: Path
    native: native.NativeTools
    llvm_dir: Path | None = None


def _on_path(name: str) -> Path:
    candidate = shutil.which(name)
    if candidate is None:
        raise FileNotFoundError(f"{name} was not found on PATH")
    return Path(candidate).resolve(strict=True)


# The Visual Studio C++ tools ship clang-format under ``VC\Tools\Llvm``; the x64 copy is the
# host formatter and the sibling ``bin`` directory is the architecture-neutral one.
_LLVM_FORMATTER_SUBDIRECTORIES = (Path("bin"), Path("x64") / "bin")


def _visual_studio_clang_format(msbuild: Path) -> Path:
    """Fall back to the clang-format that ships inside the Visual Studio C++ tools.

    Only the formatter is required: clang-tidy and the wider LLVM/Clang toolchain stay optional
    compiler-analysis prerequisites, so a compiler install without them still formats.
    """

    for parent in Path(msbuild).resolve().parents:
        if parent.name.casefold() != "msbuild":
            continue
        llvm = parent.parent / "VC" / "Tools" / "Llvm"
        for relative in _LLVM_FORMATTER_SUBDIRECTORIES:
            formatter = llvm / relative / "clang-format.exe"
            if formatter.is_file():
                return formatter.resolve()
    raise FileNotFoundError(
        f"clang-format was not found on PATH or beside the MSBuild installation: {msbuild}"
    )


def _source_clang_format(native_tools: native.NativeTools) -> Path:
    """Resolve the standalone formatter, preferring PATH but accepting the VS install."""

    try:
        return _on_path("clang-format")
    except FileNotFoundError:
        return _visual_studio_clang_format(native_tools.msbuild)


def _psscriptanalyzer_path(pwsh: Path) -> Path:
    completed = subprocess.run(
        [str(pwsh), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _PSSA_QUERY],
        check=True,
        capture_output=True,
        text=True,
    )
    candidate = completed.stdout.strip()
    if not candidate:
        raise FileNotFoundError("PSScriptAnalyzer was not found")
    return Path(candidate).resolve(strict=True)


def locate_tools() -> CheckTools:
    """Resolve the source-check tools without the compiler-analysis LLVM toolchain.

    The format, PSScriptAnalyzer and contract checks only need a standalone formatter, Cppcheck
    and the native MSBuild locator used to restore the Cppcheck third-party headers; the LLVM
    ``clang-format``/``clang-tidy`` installation is a compiler-analysis prerequisite, not a
    source one.
    """

    pwsh = _on_path("pwsh")
    native_tools = native.locate_tools()
    return CheckTools(
        pwsh=pwsh,
        clang_format=_source_clang_format(native_tools),
        cppcheck=_on_path("cppcheck"),
        psscriptanalyzer=_psscriptanalyzer_path(pwsh),
        native=native_tools,
    )


def locate_analysis_tools() -> CheckTools:
    """Add the compiler-analysis LLVM toolchain the MSVC/clang-tidy route requires."""

    return replace(locate_tools(), llvm_dir=discover_msvc_toolchain().llvm_dir)


def source_checks(
    repository: Path,
    architectures: Sequence[str],
    output: Path,
    *,
    jobs: int = 4,
    runner: Runner | None = None,
    tools: CheckTools | None = None,
) -> tuple[Path, ...]:
    """Run clang-format, Cppcheck, PSScriptAnalyzer and the repository contracts.

    Every check that reads the third-party headers runs after one vcpkg restore per architecture,
    and the Cppcheck and PSScriptAnalyzer findings are normalized, merged and gated through the
    shared ``core.sarif`` command line.  The gated SARIF report under ``output`` is returned.

    ``jobs`` is validated for interface symmetry with the build operations; the direct checks run
    sequentially because this operation owns no scheduler.
    """

    repository = Path(repository).resolve()
    selected = _architectures(architectures)
    _positive_jobs(jobs)
    tools = tools or locate_tools()
    execute = runner or native.run_tool
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    native.restore(repository, selected, tools=tools.native, runner=execute)

    cpp_sources = _files(repository / "src", _CPP_SUFFIXES)
    if cpp_sources:
        execute(
            [
                str(tools.clang_format),
                "--dry-run",
                "--Werror",
                *(str(path) for path in cpp_sources),
            ],
            repository,
            None,
        )

    findings: list[Path] = []
    for architecture in selected:
        findings.append(_cppcheck(repository, tools, architecture, output, execute))

    powershell_sources = _powershell_sources(repository)
    if powershell_sources:
        findings.append(
            _psscriptanalyzer(repository, tools, powershell_sources, output, execute)
        )

    _contracts(repository, tools, execute)

    merged = output / "analysis.sarif"
    _sarif(
        repository,
        output,
        execute,
        ["merge", *(str(report) for report in findings), "--output-name", "analysis.sarif"],
    )
    _sarif(repository, output, execute, ["gate", str(merged)])
    return (merged,)


def compiler_analysis(
    repository: Path,
    architectures: Sequence[str],
    output: Path,
    *,
    jobs: int = 2,
    runner: Runner | None = None,
    tools: CheckTools | None = None,
) -> tuple[Path, ...]:
    """Compile every production translation unit under MSVC ``/analyze`` and clang-tidy.

    Each project is rebuilt into an isolated output variant so the analyzers never pollute the
    ordinary incremental build.  The raw MSVC SARIF and clang-tidy logs are normalized, merged
    and gated per architecture through the shared ``core.sarif`` command line, and the gated
    report of every architecture is returned.
    """

    repository = Path(repository).resolve()
    selected = _architectures(architectures)
    _positive_jobs(jobs)
    tools = tools or locate_analysis_tools()
    execute = runner or native.run_tool
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    native.restore(repository, selected, tools=tools.native, runner=execute)

    projects = _projects(repository)
    reports: list[Path] = []
    for architecture in selected:
        normalized: list[Path] = []
        for name, project in projects:
            if name == "leak-probe" and architecture != "x64":
                continue
            configuration = "Release" if name == "leak-probe" else "Debug"
            normalized.append(
                _analyze_msvc(
                    repository, tools, architecture, configuration, name, project,
                    output, jobs, execute,
                )
            )
            normalized.append(
                _analyze_tidy(
                    repository, tools, architecture, configuration, name, project,
                    output, jobs, execute,
                )
            )
        merged = output / architecture / "analysis.sarif"
        _sarif(
            repository,
            output,
            execute,
            [
                "merge",
                *(str(report) for report in normalized),
                "--output-name",
                f"{architecture}/analysis.sarif",
            ],
        )
        _sarif(repository, output, execute, ["gate", str(merged)])
        reports.append(merged)
    return tuple(reports)


def _architectures(architectures: Iterable[str]) -> tuple[str, ...]:
    selected = tuple(architectures)
    if not selected:
        raise ValueError("at least one architecture is required")
    if len(set(selected)) != len(selected):
        raise ValueError("source architectures must be unique")
    for architecture in selected:
        if architecture not in native.PLATFORMS:
            raise ValueError(f"unsupported architecture: {architecture}")
    return selected


def _positive_jobs(jobs: object) -> None:
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise ValueError("jobs must be a positive integer")


def _files(root: Path, suffixes: frozenset[str]) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    found = []
    for directory, directories, names in root.walk():
        directories[:] = sorted(set(directories) - _IGNORED_DIRECTORIES)
        for name in names:
            path = directory / name
            if path.suffix in suffixes:
                found.append(path)
    return tuple(sorted(found, key=str))


def _powershell_sources(repository: Path) -> tuple[Path, ...]:
    return _files(repository, _POWERSHELL_SUFFIXES)


def _projects(repository: Path) -> tuple[tuple[str, Path], ...]:
    return tuple(
        (project.stem, project)
        for project in sorted((repository / "build" / "projects").glob("*.vcxproj"))
    )


def _cppcheck(
    repository: Path, tools: CheckTools, architecture: str, output: Path, execute: Runner
) -> Path:
    platform, define = _CPPCHECK[architecture]
    triplet = native.triplet(architecture)
    include_dir = native.restore_root(repository, architecture) / triplet / "include"
    report = output / f"cppcheck-{architecture}.sarif"
    # The installed Cppcheck CLI refuses to run unless its build directory already exists.
    build_directory = output / f"cppcheck-{architecture}-build"
    build_directory.mkdir(parents=True, exist_ok=True)
    execute(
        [
            str(tools.cppcheck),
            str(repository / "src"),
            "--std=c++23",
            f"--platform={platform}",
            "-DWIN32=1",
            "-D_WIN32=1",
            "-DUNICODE=1",
            "-D_UNICODE=1",
            f"-D{define}",
            f"-I{repository / 'src'}",
            f"-I{include_dir}",
            "--enable=warning,style,performance,portability",
            "--check-level=exhaustive",
            "--inconclusive",
            "--inline-suppr",
            "--suppress=missingIncludeSystem",
            "--suppress=uninitMemberVarNoCtor:src/api.h",
            f"--suppress=*:*\\{triplet}\\include\\*",
            "--suppress=functionStatic",
            f"--relative-paths={repository}",
            "--output-format=sarif",
            f"--output-file={report}",
            f"--cppcheck-build-dir={build_directory}",
        ],
        repository,
        None,
    )
    if not report.is_file():
        raise FileNotFoundError(f"Cppcheck did not produce {report}")
    _stamp_cppcheck(report, f"cppcheck/{architecture}/")
    return report


def _stamp_cppcheck(report: Path, automation_id: str) -> None:
    """Give every Cppcheck SARIF run the automation identity the merge requires."""

    document = json.loads(report.read_text(encoding="utf-8-sig"))
    runs = document.get("runs")
    if not isinstance(runs, list):
        raise ValueError(f"Cppcheck SARIF report has no runs: {report}")
    for index, run in enumerate(runs):
        run.setdefault("automationDetails", {})["id"] = f"{automation_id}{index}/"
    report.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _psscriptanalyzer(
    repository: Path,
    tools: CheckTools,
    sources: tuple[Path, ...],
    output: Path,
    execute: Runner,
) -> Path:
    """Run the branchless PowerShell collector, then convert its findings to SARIF in Python.

    The script only gathers raw ``Invoke-ScriptAnalyzer`` records so the severity mapping and the
    SARIF assembly stay in covered, independently testable Python.
    """

    raw = output / "psscriptanalyzer-findings.json"
    execute(
        [
            str(tools.pwsh),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(_PS_SCRIPT),
            "-Module",
            str(tools.psscriptanalyzer),
            "-Settings",
            str(repository / "build" / "PSScriptAnalyzerSettings.psd1"),
            "-Output",
            str(raw),
            *(str(path) for path in sources),
        ],
        repository,
        None,
    )
    report = output / "psscriptanalyzer.sarif"
    _write_sarif(report, _pssa_runs(repository, sources, _read_findings(raw)))
    return report


def _read_findings(path: Path) -> list[dict[str, object]]:
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, list):
        raise ValueError(f"PSScriptAnalyzer findings must be a list: {path}")
    return document


def _uri(repository: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repository).as_posix()
    except ValueError as error:
        raise ValueError(f"PSScriptAnalyzer path is outside the repository: {path}") from error


def _severity(severity: str) -> str:
    return _SARIF_LEVELS.get(severity, "note")


def _pssa_result(finding: dict[str, object], uri: str) -> dict[str, object]:
    return {
        "ruleId": str(finding["RuleName"]),
        "level": _severity(str(finding["Severity"])),
        "message": {"text": str(finding["Message"])},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": uri},
                    "region": {
                        "startLine": int(finding["Line"]),
                        "startColumn": int(finding["Column"]),
                    },
                }
            }
        ],
    }


def _pssa_runs(
    repository: Path, sources: tuple[Path, ...], findings: Iterable[dict[str, object]]
) -> list[dict[str, object]]:
    """Group findings per source, preserving the scanned order and keeping empty runs."""

    grouped: dict[str, list[dict[str, object]]] = {
        _uri(repository, source): [] for source in sources
    }
    for finding in findings:
        uri = _uri(repository, Path(str(finding["ScriptPath"])))
        grouped.setdefault(uri, []).append(_pssa_result(finding, uri))
    return [
        {
            "automationDetails": {"id": f"psscriptanalyzer/{uri}/"},
            "tool": {"driver": {"name": "PSScriptAnalyzer"}},
            "results": results,
        }
        for uri, results in grouped.items()
    ]


def _write_sarif(path: Path, runs: list[dict[str, object]]) -> None:
    document = {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": runs,
    }
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _contracts(repository: Path, tools: CheckTools, execute: Runner) -> None:
    for test in sorted((repository / "build" / "tests").glob("*.Tests.ps1")):
        execute(
            [
                str(tools.pwsh),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(test),
            ],
            repository,
            None,
        )


def _analyze_msvc(
    repository: Path,
    tools: CheckTools,
    architecture: str,
    configuration: str,
    name: str,
    project: Path,
    output: Path,
    jobs: int,
    execute: Runner,
) -> Path:
    variant = output / architecture / "raw" / "msvc" / name
    report = variant / f"{name}.sarif"
    execute(
        _analysis_command(
            repository, tools, architecture, configuration, project, variant, jobs
        )
        + [
            "/p:EnableMicrosoftCodeAnalysis=true",
            "/p:unpackratEnableClangTidy=false",
            f"/p:unpackratAnalysisReportName={name}",
            f"/p:unpackratAnalysisReportPath={report}",
        ],
        repository,
        None,
    )
    if not report.is_file():
        raise FileNotFoundError(f"MSVC analysis did not produce {report}")
    return _normalize_msvc(
        repository, report, f"msvc-analyze/{architecture}/{name}/",
        f"{architecture}/msvc/{name}.sarif", output, execute,
    )


def _analyze_tidy(
    repository: Path,
    tools: CheckTools,
    architecture: str,
    configuration: str,
    name: str,
    project: Path,
    output: Path,
    jobs: int,
    execute: Runner,
) -> Path:
    variant = output / architecture / "raw" / "tidy" / name
    log = variant / "obj" / f"{name}.ClangTidy.log"
    execute(
        _analysis_command(
            repository, tools, architecture, configuration, project, variant, jobs
        )
        + [
            "/p:EnableMicrosoftCodeAnalysis=false",
            "/p:unpackratEnableClangTidy=true",
            f"/p:LLVMInstallDir={tools.llvm_dir}",
            f"/p:ClangTidyLogFile={name}.ClangTidy.log",
        ],
        repository,
        None,
    )
    if not log.is_file():
        raise FileNotFoundError(f"clang-tidy did not produce {log}")
    return _convert_tidy(
        repository, log.parent, f"clang-tidy/{architecture}/{name}/",
        f"{architecture}/tidy/{name}.sarif", output, execute,
    )


def _analysis_command(
    repository: Path,
    tools: CheckTools,
    architecture: str,
    configuration: str,
    project: Path,
    variant: Path,
    jobs: int,
) -> list[str]:
    """One project compiled for the analyzer into its own isolated output variant.

    Ordinary projects are rebuilt so the analysis never pollutes the incremental build.  A fuzz
    project is not linked because its harness has no ordinary entry point and links the LLVM ASan
    runtime, so every translation unit is compiled with the compile-only target instead.
    """

    fuzz_project = project.stem.startswith(_FUZZ_PREFIX)
    command = [
        str(tools.native.msbuild),
        str(project),
        "/nologo",
        f"/m:{jobs}",
        "/nr:false",
        _COMPILE_TARGET if fuzz_project else _REBUILD_TARGET,
        "/p:BuildProjectReferences=false",
        f"/p:Configuration={configuration}",
        f"/p:Platform={native.PLATFORMS[architecture]}",
        f"/p:OutDir={variant}{os.sep}",
        f"/p:IntDir={variant / 'obj'}{os.sep}",
        "/p:unpackratCompileAnalysis=true",
        f"/p:VcpkgRoot={tools.native.vcpkg_root}",
        f"/p:VcpkgInstalledDir={native.restore_root(repository, architecture)}{os.sep}",
        "/p:unpackratRunCodeAnalysis=true",
        "/p:RunCodeAnalysis=true",
    ]
    if fuzz_project:
        # A compile-only target still honours incremental tracking, so a stable output directory
        # could silently reuse an earlier analysis.  ForceRebuild recompiles every translation
        # unit for the fuzz harnesses; ordinary projects already rebuild explicitly.
        command.append("/p:ForceRebuild=true")
    return command


def _normalize_msvc(
    repository: Path,
    report: Path,
    automation_id: str,
    output_name: str,
    output: Path,
    execute: Runner,
) -> Path:
    _sarif(
        repository,
        output,
        execute,
        ["normalize-msvc", str(report), automation_id, "--output-name", output_name],
    )
    return output / output_name


def _convert_tidy(
    repository: Path,
    log_root: Path,
    automation_id: str,
    output_name: str,
    output: Path,
    execute: Runner,
) -> Path:
    _sarif(
        repository,
        output,
        execute,
        [
            "convert-tidy",
            str(repository),
            str(log_root),
            automation_id,
            "--output-name",
            output_name,
        ],
    )
    return output / output_name


def _sarif(
    repository: Path, output: Path, execute: Runner, arguments: Sequence[str]
) -> None:
    environment = {
        "UNPACKRAT_OUT_DIR": str(output),
        "UNPACKRAT_BUILD_DIR": str(output / "work"),
    }
    execute(
        [sys.executable, "-m", "core.sarif", *arguments],
        repository / "build",
        environment,
    )

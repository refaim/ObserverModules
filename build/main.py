"""Console entry point for the direct MSBuild build and verification route.

This replaces the content-addressed graph driver with plain, coarse calls: the
native route (:mod:`native`) lets MSBuild own project dependencies, scheduling
and incremental state under a stable ``out/native`` tree, and every other
family is a direct function that shells out to the retained ``core`` validators
(:mod:`diagnostics`, :mod:`source_checks`, :mod:`packaging_ops`).  The argument
and command surface is preserved.  Verification runs explicit, ordered gate
calls with a small keep-going evidence collector: independent gates always run,
only a dependent package is blocked by an earlier failure, and the current
reports are exported before the run exits nonzero.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import traceback
from typing import TextIO

import win32api
import win32process

import diagnostics
import native
import packaging_ops
import source_checks
from core.clean import CleanError, _is_reparse, clean as clean_output, prepare_fresh
from core.doctor import main as doctor_main
from core.host import detect_host_architecture, verify_route
from core.package import MODULES
from core.quality_tools import (
    resolve_asan_runtimes,
    resolve_fuzzer_runtime,
    resolve_llvm,
    resolve_profile_runtime,
    resolve_ubsan_runtime,
    resolve_umdh,
)
from core.toolchain import discover_msvc_toolchain
from core.windows_process import WindowsProcessRunner
from core import result_export


_ARCHITECTURES = native.ARCHITECTURES
_CONFIGURATIONS = native.CONFIGURATIONS
_FUZZ_TARGETS = diagnostics.FUZZ_TARGETS
_FUZZ_ARCHITECTURES = ("x86", "x64")

# A job-wide priority class keeps an interactive desktop responsive while the build saturates
# the machine.  It is applied to this process and to the kill-on-close job every child lives in,
# so the whole tree inherits it; a hosted runner has no desktop to protect and asks for the
# normal class explicitly.
_PRIORITIES: dict[str, int | None] = {
    "below-normal": win32process.BELOW_NORMAL_PRIORITY_CLASS,
    "normal": None,
}

_OPERATION_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    ValueError,
    CleanError,
    packaging_ops.PackagingError,
    diagnostics.DiagnosticsError,
    result_export.ResultExportError,
)


class VerificationFailed(RuntimeError):
    """One or more verification operations failed; the evidence was exported first."""

    def __init__(self, outputs: tuple[Path, ...], failures: tuple[str, ...]) -> None:
        super().__init__("verification failed: " + ", ".join(failures))
        self.outputs = outputs
        self.failures = failures


@dataclass(frozen=True, slots=True)
class _Processes:
    """One job-owned runner adapted to each repository runner contract."""

    native: Callable[..., None]
    tools: Callable[..., diagnostics.ToolResult]
    packaging: Callable[..., None]


class _Tee:
    """Write-through stream so an operation both shows its output and records it."""

    def __init__(self, *streams: TextIO) -> None:
        self._streams = streams

    def write(self, text: str) -> int:
        for stream in self._streams:
            stream.write(text)
        return len(text)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def _selection(choices: tuple[str, ...]) -> Callable[[str], tuple[str, ...]]:
    lookup = {item.casefold(): item for item in choices}

    def parse(value: str) -> tuple[str, ...]:
        parts = tuple(item.strip() for item in value.split(","))
        if not all(parts):
            raise argparse.ArgumentTypeError(
                f"expected all or comma-separated: {','.join(choices)}"
            )
        if any(item.casefold() == "all" for item in parts):
            if len(parts) != 1:
                raise argparse.ArgumentTypeError("'all' must be selected on its own")
            return choices
        try:
            return tuple(dict.fromkeys(lookup[item.casefold()] for item in parts))
        except KeyError as error:
            raise argparse.ArgumentTypeError(
                f"expected all or comma-separated: {','.join(choices)}"
            ) from error

    return parse


def _integer(minimum: int, maximum: int | None = None) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("expected an integer") from error
        if number < minimum or maximum is not None and number > maximum:
            limit = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
            raise argparse.ArgumentTypeError(f"expected {limit}")
        return number

    return parse


_OPTIONS: dict[str, tuple[tuple[str, ...], dict[str, object]]] = {
    "arch": (("-Arch", "--arch"), {"type": _selection(_ARCHITECTURES), "default": ("x64",)}),
    "config": (("-Config", "--config"), {"type": _selection(_CONFIGURATIONS), "default": ("Debug",)}),
    "corpus": (("-Corpus", "--corpus"), {"type": Path}),
    "restore": (("-RestoreFlavor", "--restore-flavor"), {
        "type": _selection(("default", "asan")), "default": ("default",),
    }),
    "fuzz_seconds": (("-FuzzSeconds", "--fuzz-seconds"), {"type": _integer(1, 86400), "default": 60}),
    "leak_warmup": (("-LeakWarmup", "--leak-warmup"), {"type": _integer(1, 1_000_000), "default": 8}),
    "leak_iterations": (("-LeakIterations", "--leak-iterations"), {
        "type": _integer(1, 1_000_000), "default": 100,
    }),
    "leak_windows": (("-LeakWindows", "--leak-windows"), {"type": _integer(3, 10), "default": 3}),
    "leak_tolerance": (("-LeakToleranceBytes", "--leak-tolerance-bytes"), {
        "type": _integer(0, 1_073_741_824), "default": 0,
    }),
    "export_dir": (("-ExportDir", "--export-dir"), {"type": Path}),
    "module": (("-Module", "--module"), {"type": _selection(MODULES), "default": MODULES}),
    "clean_mode": (("-CleanMode", "--clean-mode"), {"choices": ("all", "reports"), "default": "all"}),
}

_COMMAND_OPTIONS: dict[str, tuple[str, ...]] = {
    "restore": ("arch", "restore"),
    "build": ("arch", "config"),
    "test": ("arch", "config", "corpus"),
    "source-checks": ("arch",),
    "compiler-analysis": ("arch",),
    "test-coverage": ("arch", "corpus"),
    "test-asan": ("arch",),
    "test-ubsan": ("arch",),
    "test-leaks": ("arch", "leak_warmup", "leak_iterations", "leak_windows", "leak_tolerance"),
    "fuzz": ("arch", "fuzz_seconds", "fuzz_target"),
    "audit-binaries": ("arch", "module"),
    "package": ("arch", "module", "export_dir"),
    "verify-source": ("export_dir",),
    "verify-arch": (
        "arch", "module", "corpus", "fuzz_seconds",
        "leak_warmup", "leak_iterations", "leak_windows", "leak_tolerance", "export_dir",
    ),
    "verify": (
        "arch", "module", "corpus", "fuzz_seconds",
        "leak_warmup", "leak_iterations", "leak_windows", "leak_tolerance", "export_dir",
    ),
}

_OPTIONS["fuzz_target"] = (
    ("-FuzzTarget", "--fuzz-target"),
    {"type": _selection(_FUZZ_TARGETS), "default": _FUZZ_TARGETS},
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="observer-build")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    for name, options in _COMMAND_OPTIONS.items():
        command = commands.add_parser(name)
        command.add_argument("-Repository", "--repository", type=Path, default=Path(__file__).parents[1])
        command.add_argument("-Jobs", "--jobs", type=_integer(1))
        command.add_argument("-Priority", "--priority", choices=tuple(_PRIORITIES), default="below-normal")
        for option in options:
            flags, settings = _OPTIONS[option]
            command.add_argument(*flags, dest=option, **settings)
    clean = commands.add_parser("clean")
    clean.add_argument("-Repository", "--repository", type=Path, default=Path(__file__).parents[1])
    flags, settings = _OPTIONS["clean_mode"]
    clean.add_argument(*flags, dest="clean_mode", **settings)
    return parser


def _concise(error: BaseException) -> str:
    return " ".join(str(error).split()) or type(error).__name__


def _jobs(value: int | None, default: int) -> int:
    return default if value is None else value


def _prioritize(priority_class: int | None) -> tuple[int, int] | None:
    if priority_class is None:
        return None
    handle = win32api.GetCurrentProcess()
    previous = win32process.GetPriorityClass(handle)
    win32process.SetPriorityClass(handle, priority_class)
    return handle, previous


def _restore_priority(state: tuple[int, int] | None) -> None:
    if state is not None:
        handle, previous = state
        win32process.SetPriorityClass(handle, previous)


def _processes(priority_class: int | None) -> _Processes:
    """Adapt one job-owned runner to the native, diagnostics and packaging contracts."""

    runner = WindowsProcessRunner(priority_class=priority_class)

    def echo(completed: subprocess.CompletedProcess[str]) -> None:
        """Show a child's combined output, so direct commands are never silent."""

        if completed.stdout:
            print(completed.stdout, end="")

    def native_runner(argv, cwd=None, env=None) -> None:
        completed = runner.run(argv, cwd, env)
        echo(completed)
        if completed.returncode:
            raise subprocess.CalledProcessError(
                completed.returncode, [str(item) for item in argv], completed.stdout
            )

    def tool_runner(argv, cwd=None, env=None) -> diagnostics.ToolResult:
        completed = runner.run(argv, cwd, env)
        echo(completed)
        return diagnostics.ToolResult(completed.returncode, completed.stdout or "")

    def packaging_runner(argv, *, cwd=None, env=None, stdout=None) -> None:
        completed = runner.run(argv, cwd, env, stdout=stdout)
        if stdout is None:
            echo(completed)
        if completed.returncode:
            raise subprocess.CalledProcessError(
                completed.returncode, [str(item) for item in argv], completed.stdout
            )

    return _Processes(native_runner, tool_runner, packaging_runner)


def _reports_root(repository: Path) -> Path:
    return Path(repository).resolve(strict=True) / "out" / "reports"


def _output_dir(repository: Path, name: str) -> Path:
    output = _reports_root(repository) / name
    output.mkdir(parents=True, exist_ok=True)
    return output


def _reset_output(repository: Path, name: str) -> Path:
    root = Path(repository).resolve(strict=True)
    return prepare_fresh(root / "out" / "reports" / name, root)


def _diagnostics_tools(
    architectures: Sequence[str], *, profile: bool = False, asan: bool = False, ubsan: bool = False,
    fuzzer: bool = False, umdh: bool = False,
) -> diagnostics.DiagnosticsTools:
    """Resolve only the tools and runtimes the requested architectures actually consume."""

    native_tools = native.locate_tools()
    toolchain = discover_msvc_toolchain()
    asan_runtimes: dict[str, Path] = {}
    if asan:
        asan_runtimes = {
            architecture: tool.path
            for architecture, tool in resolve_asan_runtimes(toolchain, tuple(architectures))
        }
    llvm_runtimes: dict[str, Path] = {}
    if profile:
        llvm_runtimes = {
            architecture: resolve_profile_runtime(toolchain, architecture).path
            for architecture in architectures
        }
    elif ubsan:
        # UBSan ships one x64 archive pair; only an x64 request consumes it.
        llvm_runtimes = (
            {"x64": resolve_ubsan_runtime(toolchain).path} if "x64" in architectures else {}
        )
    elif fuzzer:
        llvm_runtimes = {
            architecture: resolve_fuzzer_runtime(toolchain, architecture).path
            for architecture in architectures
        }
    return diagnostics.DiagnosticsTools(
        msbuild=native_tools.msbuild,
        vcpkg=native_tools.vcpkg,
        llvm_install=toolchain.llvm_dir,
        llvm_cov=resolve_llvm(toolchain, "llvm-cov").path if profile else Path(),
        llvm_profdata=resolve_llvm(toolchain, "llvm-profdata").path if profile else Path(),
        llvm_runtimes=llvm_runtimes,
        asan_runtimes=asan_runtimes,
        umdh=resolve_umdh().path if umdh else Path(),
    )


def _files_under(directory: Path) -> tuple[Path, ...]:
    """Every real file below ``directory``, never descending through a reparse point or link."""

    files: list[Path] = []
    pending = [Path(directory)]
    while pending:
        current = pending.pop()
        if _is_reparse(current):
            continue
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            child = Path(entry.path)
            if _is_reparse(child):
                continue
            if entry.is_dir(follow_symlinks=False):
                pending.append(child)
            else:
                files.append(child)
    return tuple(sorted(files, key=str))


def _evidence_reports(paths: Sequence[Path]) -> tuple[Path, ...]:
    """Expand any declared directory report into the real files it currently holds.

    A diagnostic operation such as :func:`diagnostics.fuzz` returns its per-target evidence
    directory, which the exporter can only publish file by file.  A plain file path is left
    untouched, so the exporter still validates it (and still rejects a missing claim).
    """

    reports: list[Path] = []
    for path in paths:
        candidate = Path(path)
        if candidate.is_dir():
            reports.extend(_files_under(candidate))
        else:
            reports.append(candidate)
    return tuple(reports)


def _owned_reports(
    declared: Sequence[Path], evidence: Path | None, exclude: Path | None,
) -> tuple[Path, ...]:
    """Every current evidence file of one operation, deduplicated in declaration order.

    ``declared`` is what the operation returned; ``evidence`` is its operation-specific output
    root.  A gate may write artifacts it does not name -- a nested JUnit tree, a SARIF, sanitizer
    logs, a coverage data file -- so the declared reports are combined with a scan of the current
    evidence root and the two are deduplicated.  ``exclude`` drops one owned sub-tree (the release
    ``packages`` directory) from both the success and failure paths, so a release payload is never
    mistaken for a report.
    """

    candidates = list(declared)
    if evidence is not None:
        candidates.extend(_files_under(evidence))
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        if exclude is not None and exclude in path.parents:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return tuple(unique)


def _export(destination: Path, command: str, operations: Sequence[result_export.Operation],
            *, packages: Sequence[Path] = ()) -> Path:
    return result_export.export_results(Path(destination), command, operations, packages=packages)


def _attempt(
    records: list[result_export.Operation],
    name: str,
    action: Callable[[], Sequence[Path]],
    *,
    evidence: Path | None = None,
    exclude: Path | None = None,
) -> bool:
    """Run one gate, record its verdict and captured output, and keep going on any failure."""

    captured = StringIO()
    try:
        with redirect_stdout(_Tee(sys.stdout, captured)):
            declared = _evidence_reports(tuple(action()))
    except Exception as error:
        records.append(result_export.Operation(
            name, result_export.FAILED, detail=_concise(error),
            reports=_owned_reports((), evidence, exclude),
            log=captured.getvalue() + traceback.format_exc(),
        ))
        return False
    records.append(result_export.Operation(
        name, result_export.PASSED, reports=_owned_reports(declared, evidence, exclude),
        log=captured.getvalue(),
    ))
    return True


def _blocked(records: list[result_export.Operation], name: str, reason: str) -> None:
    records.append(result_export.Operation(name, result_export.DEFERRED, detail=reason))


def _finish(
    command: str,
    args: argparse.Namespace,
    records: Sequence[result_export.Operation],
    *,
    packages: Sequence[Path],
) -> tuple[Path, ...]:
    failed = any(item.status == result_export.FAILED for item in records)
    if args.export_dir is not None:
        _export(args.export_dir, command, records, packages=packages)
    for item in records:
        if item.status == result_export.DEFERRED:
            print(f"[DEFERRED] {item.name}: {item.detail}")
    outputs = tuple(report for item in records for report in item.reports)
    if failed:
        raise VerificationFailed(
            outputs, tuple(item.name for item in records if item.status == result_export.FAILED)
        )
    return outputs


def _restore_asan(
    repository: Path, architectures: tuple[str, ...], tools: native.NativeTools,
    processes: _Processes,
) -> tuple[Path, ...]:
    roots: list[Path] = []
    for architecture in architectures:
        root = native.artifacts_root(repository) / "vcpkg_installed" / f"{architecture}-asan"
        scratch = native.artifacts_root(repository) / "vcpkg_scratch" / f"{architecture}-asan"
        processes.native(
            (
                str(tools.vcpkg),
                "install",
                f"--x-install-root={root}",
                f"--x-buildtrees-root={scratch / 'buildtrees'}",
                f"--x-packages-root={scratch / 'packages'}",
                "--triplet",
                f"{native.triplet(architecture)}-asan",
                f"--x-manifest-root={repository}",
                f"--overlay-triplets={repository / 'build' / 'vcpkg' / 'triplets'}",
            ),
            repository,
            None,
        )
        roots.append(root)
    return tuple(roots)


def _restore(repository: Path, args: argparse.Namespace, processes: _Processes) -> tuple[Path, ...]:
    tools = native.locate_tools()
    outputs: tuple[Path, ...] = ()
    if "default" in args.restore:
        outputs += native.restore(repository, args.arch, tools=tools, runner=processes.native)
    if "asan" in args.restore:
        architectures = tuple(item for item in args.arch if item != "arm64")
        if architectures:
            outputs += _restore_asan(repository, architectures, tools, processes)
    return outputs


def _build(repository: Path, args: argparse.Namespace, processes: _Processes) -> tuple[Path, ...]:
    return native.build(
        repository, args.arch, args.config, jobs=args.jobs,
        tools=native.locate_tools(), runner=processes.native,
    )


def _test(repository: Path, args: argparse.Namespace, processes: _Processes) -> tuple[Path, ...]:
    return native.test(
        repository, args.arch, args.config, corpus=args.corpus, jobs=args.jobs,
        tools=native.locate_tools(), runner=processes.native,
    )


def _source_checks(repository: Path, args: argparse.Namespace, processes: _Processes) -> tuple[Path, ...]:
    output = _output_dir(repository, "source-checks")
    return source_checks.source_checks(
        repository, args.arch, output, jobs=_jobs(args.jobs, 4),
        tools=source_checks.locate_tools(), runner=processes.native,
    )


def _compiler_analysis(repository: Path, args: argparse.Namespace, processes: _Processes) -> tuple[Path, ...]:
    output = _output_dir(repository, "compiler-analysis")
    return source_checks.compiler_analysis(
        repository, args.arch, output, jobs=_jobs(args.jobs, 2),
        tools=source_checks.locate_analysis_tools(), runner=processes.native,
    )


def _test_coverage(repository: Path, args: argparse.Namespace, processes: _Processes) -> tuple[Path, ...]:
    output = _output_dir(repository, "test-coverage")
    return diagnostics.test_coverage(
        repository, args.arch, output, tools=_diagnostics_tools(args.arch, profile=True),
        runner=processes.tools, jobs=args.jobs, corpus=args.corpus,
    )


def _sanitizer(sanitizer: str) -> Callable[[Path, argparse.Namespace, _Processes], tuple[Path, ...]]:
    def handler(repository: Path, args: argparse.Namespace, processes: _Processes) -> tuple[Path, ...]:
        output = _output_dir(repository, f"test-{sanitizer}")
        return diagnostics.test_sanitizer(
            repository, sanitizer, args.arch, output,
            tools=_diagnostics_tools(args.arch, **{sanitizer: True}),
            runner=processes.tools, jobs=args.jobs,
        )

    return handler


def _test_leaks(repository: Path, args: argparse.Namespace, processes: _Processes) -> tuple[Path, ...]:
    output = _output_dir(repository, "test-leaks")
    return diagnostics.test_leaks(
        repository, output, args.leak_warmup, args.leak_iterations, args.leak_windows,
        args.leak_tolerance, tools=_diagnostics_tools(("x64",), umdh=True),
        runner=processes.tools, jobs=args.jobs,
    )


def _fuzz(repository: Path, args: argparse.Namespace, processes: _Processes) -> tuple[Path, ...]:
    output = _output_dir(repository, "fuzz")
    return diagnostics.fuzz(
        repository, args.arch, args.fuzz_target, args.fuzz_seconds, output,
        tools=_diagnostics_tools(args.arch, fuzzer=True), runner=processes.tools, jobs=args.jobs,
    )


def _audit(repository: Path, args: argparse.Namespace, processes: _Processes) -> tuple[Path, ...]:
    output = _output_dir(repository, "audit-binaries")
    # The public audit gates the stable Release binaries, so it builds Release first (MSBuild is
    # incremental on a warm tree) exactly as the retired driver's audited-release did.
    native.build(
        repository, args.arch, ("Release",), jobs=args.jobs,
        tools=native.locate_tools(), runner=processes.native,
    )
    return packaging_ops.audit_binaries(
        repository, args.arch, output, modules=args.module,
        tools=packaging_ops.locate_packaging_tools(), runner=processes.packaging,
    )


def _package_command(repository: Path, args: argparse.Namespace, processes: _Processes) -> tuple[Path, ...]:
    host = detect_host_architecture()
    route = verify_route(args.arch, host)
    # A direct package run owns a fresh operation output root instead of the shared ``out`` tree,
    # so a rerun can never republish the stale archives left under ``out/packages`` and a failure
    # keeps only the audit evidence this run actually produced.
    output = _reset_output(repository, "package")
    packages_root = output / "packages"
    captured = StringIO()
    try:
        # Packaging freezes and audits the stable Release binaries, so build Release first.
        with redirect_stdout(_Tee(sys.stdout, captured)):
            native.build(
                repository, args.arch, ("Release",), jobs=args.jobs,
                tools=native.locate_tools(), runner=processes.native,
            )
            result = packaging_ops.package(
                repository, args.arch, output, modules=args.module,
                smoke_architectures=route.runnable,
                tools=packaging_ops.locate_packaging_tools(), runner=processes.packaging, host=host,
            )
    except Exception as error:
        if args.export_dir is not None:
            _export(args.export_dir, "package", (
                result_export.Operation(
                    "package", result_export.FAILED, detail=_concise(error),
                    reports=_owned_reports((), output, packages_root),
                    log=captured.getvalue() + traceback.format_exc(),
                ),
            ))
        raise
    if args.export_dir is not None:
        _export(
            args.export_dir, "package",
            (result_export.Operation(
                "package", result_export.PASSED,
                # The just-produced audit and smoke evidence is the operation's report; the
                # release payload below the ``packages`` root is exported only as packages.
                reports=_owned_reports((), output, packages_root),
                log=captured.getvalue(),
            ),),
            packages=(*result.archives, packages_root / "packages.json"),
        )
    return result.archives


def _tests_action(
    repository: Path, args: argparse.Namespace, architecture: str,
    runnable: tuple[str, ...], processes: _Processes, host: str,
) -> Callable[[], tuple[Path, ...]]:
    def action() -> tuple[Path, ...]:
        # MSBuild keeps its JUnit reports in a persistent per-architecture tree, so a fresh run
        # must clear the selected architecture first; otherwise an early build failure would
        # publish a stale previous success from this same architecture.
        root = Path(repository).resolve(strict=True)
        prepare_fresh(native.artifacts_root(root) / "reports" / "tests" / architecture, root)
        tools = native.locate_tools()
        if architecture in runnable:
            return native.test(
                repository, (architecture,), ("Debug", "Release"), corpus=args.corpus,
                jobs=args.jobs, tools=tools, runner=processes.native, host=host,
            )
        return native.build(
            repository, (architecture,), ("Debug", "Release"), jobs=args.jobs,
            tools=tools, runner=processes.native,
        )

    return action


def _python_coverage(repository: Path, output: Path, processes: _Processes) -> tuple[Path, ...]:
    build_root = Path(repository).resolve(strict=True) / "build"
    output.mkdir(parents=True, exist_ok=True)
    # The retained gate stores its raw ``.coverage`` data file in the build directory and then
    # copies it into the output directory, so the two must be different directories or its
    # ``shutil.copyfile`` raises SameFileError before publishing any evidence.
    data = output / "work"
    data.mkdir(parents=True, exist_ok=True)
    processes.native(
        (sys.executable, "-m", "core.python_coverage", sys.executable, str(build_root)),
        build_root,
        {"OBSERVER_OUT_DIR": str(output), "OBSERVER_BUILD_DIR": str(data)},
    )
    # The retained gate copies its raw data file into the output directory, so the published
    # evidence is the five native reports it writes; the ``work`` copy is scratch, not a report.
    return (
        output / ".coverage",
        output / "coverage.json",
        output / "coverage.xml",
        output / "coverage.toml",
        output / "coverage.txt",
    )


def _verify_source(repository: Path, args: argparse.Namespace, processes: _Processes) -> tuple[Path, ...]:
    work = _reset_output(repository, "verify-source")
    records: list[result_export.Operation] = []
    _attempt(records, "source", lambda: source_checks.source_checks(
        repository, ("x64",), work / "source", jobs=_jobs(args.jobs, 4),
        tools=source_checks.locate_tools(), runner=processes.native,
    ), evidence=work / "source")
    _attempt(records, "python-coverage", lambda: _python_coverage(
        repository, work / "python-coverage", processes,
    ), evidence=work / "python-coverage")
    return _finish("verify-source", args, records, packages=())


def _verification(
    command: str, repository: Path, args: argparse.Namespace, processes: _Processes,
) -> tuple[Path, ...]:
    host = detect_host_architecture()
    route = verify_route(args.arch, host)
    work = _reset_output(repository, command)
    records: list[result_export.Operation] = []

    _attempt(records, "source", lambda: source_checks.source_checks(
        repository, args.arch, work / "source", jobs=_jobs(args.jobs, 4),
        tools=source_checks.locate_tools(), runner=processes.native,
    ), evidence=work / "source")
    _attempt(records, "compiler-analysis", lambda: source_checks.compiler_analysis(
        repository, args.arch, work / "compiler-analysis", jobs=_jobs(args.jobs, 2),
        tools=source_checks.locate_analysis_tools(), runner=processes.native,
    ), evidence=work / "compiler-analysis")
    if command == "verify":
        _attempt(records, "python-coverage", lambda: _python_coverage(
            repository, work / "python-coverage", processes,
        ), evidence=work / "python-coverage")

    tests_root = native.artifacts_root(Path(repository).resolve(strict=True)) / "reports" / "tests"
    for architecture in args.arch:
        # A non-runnable target is only cross-built here, so its record is a ``build-<arch>``
        # gate; the deferred ``tests-<arch>`` verdict is added separately below.  Naming the
        # cross-build ``tests-<arch>`` would collide with that deferral and the exporter would
        # reject the duplicate operation.
        name = (
            f"tests-{architecture}" if architecture in route.runnable
            else f"build-{architecture}"
        )
        _attempt(
            records, name,
            _tests_action(repository, args, architecture, route.runnable, processes, host),
            evidence=tests_root / architecture,
        )

    if route.coverage:
        _attempt(records, "coverage", lambda: diagnostics.test_coverage(
            repository, route.coverage, work / "coverage",
            tools=_diagnostics_tools(route.coverage, profile=True), runner=processes.tools,
            jobs=args.jobs, host=host, corpus=args.corpus,
        ), evidence=work / "coverage")
    if route.asan:
        _attempt(records, "asan", lambda: diagnostics.test_sanitizer(
            repository, "asan", route.asan, work / "asan",
            tools=_diagnostics_tools(route.asan, asan=True),
            runner=processes.tools, jobs=args.jobs, host=host,
        ), evidence=work / "asan")
    if route.ubsan:
        _attempt(records, "ubsan", lambda: diagnostics.test_sanitizer(
            repository, "ubsan", route.ubsan, work / "ubsan",
            tools=_diagnostics_tools(route.ubsan, ubsan=True),
            runner=processes.tools, jobs=args.jobs, host=host,
        ), evidence=work / "ubsan")
    if route.fuzz:
        _attempt(records, "fuzz", lambda: diagnostics.fuzz(
            repository, route.fuzz, _FUZZ_TARGETS, args.fuzz_seconds, work / "fuzz",
            tools=_diagnostics_tools(route.fuzz, fuzzer=True), runner=processes.tools,
            jobs=args.jobs, host=host,
        ), evidence=work / "fuzz")
    if route.run_x64_specialists:
        _attempt(records, "leaks", lambda: diagnostics.test_leaks(
            repository, work / "leaks", args.leak_warmup, args.leak_iterations,
            args.leak_windows, args.leak_tolerance,
            tools=_diagnostics_tools(("x64",), umdh=True), runner=processes.tools,
            jobs=args.jobs, host=host,
        ), evidence=work / "leaks")

    # The shared ``work`` tree is reset fresh per run and ``audit_binaries`` fails closed unless
    # its output root already exists, so the gate creates its root; keeping the mkdir inside the
    # action records a creation failure as failed audit evidence and suppresses packaging.
    audit_root = work / "audit"

    def audit_gate() -> tuple[Path, ...]:
        audit_root.mkdir(parents=True, exist_ok=True)
        return packaging_ops.audit_binaries(
            repository, args.arch, audit_root, modules=args.module,
            include_leak_probe=route.run_x64_specialists,
            tools=packaging_ops.locate_packaging_tools(), runner=processes.packaging,
        )

    _attempt(records, "audit", audit_gate, evidence=audit_root)

    packages: tuple[Path, ...] = ()
    # Packaging is the only dependent operation: it publishes release payloads, so it stays
    # blocked until every independent gate above has passed.  Any failure (source, coverage,
    # sanitizer, fuzz, audit or a suite) suppresses the archives instead of exporting them.
    if any(item.status == result_export.FAILED for item in records):
        _blocked(records, "package", "blocked by failed gates")
    else:
        # Packaging owns a dedicated operation root, so a failure keeps the audit evidence it
        # has already produced instead of scanning only the never-published ``packages`` tree.
        package_root = work / "package"
        package_root.mkdir(parents=True, exist_ok=True)

        def package_gate() -> tuple[Path, ...]:
            return packaging_ops.package(
                repository, args.arch, package_root, modules=args.module,
                smoke_architectures=route.runnable,
                tools=packaging_ops.locate_packaging_tools(), runner=processes.packaging, host=host,
            ).archives

        if _attempt(
            records, "package", package_gate,
            evidence=package_root, exclude=package_root / "packages",
        ):
            packages = _files_under(package_root / "packages")

    for item in route.deferred:
        _blocked(records, f"{item.gate}-{item.architecture}", item.reason)

    return _finish(command, args, records, packages=packages)


def _verify_arch(repository: Path, args: argparse.Namespace, processes: _Processes) -> tuple[Path, ...]:
    return _verification("verify-arch", repository, args, processes)


def _verify(repository: Path, args: argparse.Namespace, processes: _Processes) -> tuple[Path, ...]:
    return _verification("verify", repository, args, processes)


_HANDLERS: dict[str, Callable[[Path, argparse.Namespace, _Processes], tuple[Path, ...]]] = {
    "restore": _restore,
    "build": _build,
    "test": _test,
    "source-checks": _source_checks,
    "compiler-analysis": _compiler_analysis,
    "test-coverage": _test_coverage,
    "test-asan": _sanitizer("asan"),
    "test-ubsan": _sanitizer("ubsan"),
    "test-leaks": _test_leaks,
    "fuzz": _fuzz,
    "audit-binaries": _audit,
    "package": _package_command,
    "verify-source": _verify_source,
    "verify-arch": _verify_arch,
    "verify": _verify,
}


def _validate_architecture_rules(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.command == "test-leaks" and args.arch != ("x64",):
        parser.error("test-leaks requires -Arch x64")
    if args.command == "fuzz" and any(item not in _FUZZ_ARCHITECTURES for item in args.arch):
        parser.error("fuzz requires -Arch x86, x64, or x86,x64")
    if args.command == "verify-arch" and len(args.arch) != 1:
        parser.error("verify-arch requires exactly one architecture")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or len(arguments) == 1 and arguments[0].casefold() == "help":
        parser.print_help()
        return 0
    args = parser.parse_args(arguments)
    if args.command == "doctor":
        return doctor_main(())
    if args.command != "clean":
        _validate_architecture_rules(parser, args)
    priority = _PRIORITIES[getattr(args, "priority", "below-normal")]
    previous = _prioritize(priority)
    try:
        if args.command == "clean":
            outputs = clean_output(args.repository, args.clean_mode)
        else:
            outputs = _HANDLERS[args.command](args.repository, args, _processes(priority))
    except VerificationFailed as error:
        outputs, failures = error.outputs, error.failures
    except subprocess.CalledProcessError as error:
        print(
            f"observer-build: command failed with exit code {error.returncode}: {error.cmd}",
            file=sys.stderr,
        )
        return error.returncode or 1
    except _OPERATION_ERRORS as error:
        print(f"observer-build: {_concise(error)}", file=sys.stderr)
        return 2
    else:
        failures = ()
    finally:
        _restore_priority(previous)
    for output in outputs:
        print(output)
    if failures:
        print(f"observer-build: failed gates: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

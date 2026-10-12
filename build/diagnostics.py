"""Direct coverage, sanitizer, fuzz and leak operations for ObserverModules.

The content-addressed graph rendered one PowerShell command per node.  This
module is the smaller successor for the diagnostic family: it drives MSBuild,
the Catch2 suites, LLVM and libFuzzer straight from Python over the stable
``out/native`` layout defined by :mod:`native`, and reuses the validated
``core.cpp_coverage``, ``core.sanitizer`` and ``core.leak`` module CLIs as
fail-closed gates.  Every external program is reached through the injected
:data:`Runner` seam, so the command shapes are unit-testable in isolation.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import native
from core.leak import MODES as LEAK_MODES
from core.leak import SCENARIOS as LEAK_SCENARIOS


PROJECTS: tuple[str, ...] = ("renpy", "rpgmaker", "zanzarah")
LEAK_PROBE = "leak-probe.exe"
COVERAGE_ARCHITECTURES: tuple[str, ...] = ("x86", "x64")
FUZZ_ARCHITECTURES: tuple[str, ...] = ("x86", "x64")
FUZZ_TARGETS: tuple[str, ...] = ("pickle", "renpy", "rpgmaker", "zanzarah")
FUZZ_MAX_LENGTH: Mapping[str, int] = {
    "pickle": 262144,
    "renpy": 1048576,
    "rpgmaker": 1048576,
    "zanzarah": 1048576,
}
# Timed libFuzzer budgets stay x64-only; x86 exists as a replay-only gate.
FUZZ_TIMED_ARCHITECTURE = "x64"
SANITIZER_CONFIGURATIONS: Mapping[str, str] = {"asan": "ASan", "ubsan": "UBSan"}
SANITIZER_ARCHITECTURES: Mapping[str, frozenset[str]] = {
    "asan": frozenset({"x86", "x64"}),
    "ubsan": frozenset({"x64"}),
}
_ASAN_OPTIONS = "halt_on_error=1:alloc_dealloc_mismatch=1"
SANITIZER_ENVIRONMENT: Mapping[str, tuple[str, str]] = {
    "asan": ("ASAN_OPTIONS", _ASAN_OPTIONS),
    "ubsan": ("UBSAN_OPTIONS", "halt_on_error=1:print_stacktrace=1"),
}
ASAN_RUNTIMES: Mapping[str, str] = {
    "x86": "clang_rt.asan_dynamic-i386.dll",
    "x64": "clang_rt.asan_dynamic-x86_64.dll",
}
# First-party C++ only: the suites, the fuzz harnesses, vcpkg and the SDKs are external.
COVERAGE_IGNORED_SOURCES = (
    r"([\\/]src[\\/](tests|fuzz)[\\/])|([\\/]vcpkg_installed[\\/])|"
    r"([\\/]Microsoft Visual Studio[\\/])|([\\/]Windows Kits[\\/])"
)
# ASan and Fuzz consume the sanitizer triplet from its own vcpkg install root.
_ASAN_SUFFIX = "-asan"
_WINDOWS_SEPARATOR = os.sep
_BUILD_ROOT = Path(__file__).resolve().parent
_PYTHON = Path(sys.executable)
# Symlinks and junctions both carry this Windows attribute; POSIX has neither.
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
# ``core.leak measure`` requires these two per-scope directories to be new.
_MEASURE_DIRECTORIES = ("snapshots", "diffs")


class DiagnosticsError(RuntimeError):
    """A direct diagnostic operation could not complete its contract."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    returncode: int
    stdout: str


# The single seam every external program is reached through.
Runner = Callable[[Sequence[str], Path | None, Mapping[str, str] | None], ToolResult]
# The Release PE/BinSkim auditor: repository, architectures, output, and a keyword.
Auditor = Callable[..., None]


def run_tool(
    argv: Sequence[str],
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ToolResult:
    """Run one program, merging the environment, and keep its combined output."""

    completed = subprocess.run(
        [os.fspath(item) for item in argv],
        cwd=None if cwd is None else os.fspath(cwd),
        env={**os.environ, **(env or {})},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    return ToolResult(completed.returncode, completed.stdout)


@dataclass(frozen=True, slots=True)
class DiagnosticsTools:
    """Explicit tool locations; discovery stays with the caller."""

    msbuild: Path
    vcpkg: Path
    llvm_install: Path
    llvm_cov: Path
    llvm_profdata: Path
    llvm_runtimes: Mapping[str, Path]
    asan_runtimes: Mapping[str, Path]
    umdh: Path

    @property
    def vcpkg_root(self) -> Path:
        return self.vcpkg.parent


def _job_count(value: int | None) -> int:
    if value is None:
        return os.cpu_count() or 1
    if isinstance(value, bool) or value < 1:
        raise ValueError("jobs must be a positive integer")
    return value


def _checked(
    runner: Runner,
    command: Sequence[str],
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ToolResult:
    result = runner(command, cwd, env)
    if result.returncode:
        raise DiagnosticsError(
            f"command failed with exit code {result.returncode}: {command[0]}"
        )
    return result


def _checked_runner(runner: Runner) -> Runner:
    """Adapt a result-returning runner so a nonzero exit raises.

    :func:`native.restore` drives its runner with ``check=True`` semantics and
    expects a nonzero vcpkg exit to raise.  Our :data:`Runner` returns the code
    instead, so every ``native.restore`` call is handed this adapter rather than
    the raw runner, keeping a failed restore fail-closed.
    """

    def execute(
        argv: Sequence[str], cwd: Path | None = None, env: Mapping[str, str] | None = None
    ) -> ToolResult:
        result = runner(argv, cwd, env)
        if result.returncode:
            raise DiagnosticsError(
                f"command failed with exit code {result.returncode}: {argv[0]}"
            )
        return result

    return execute


def _require_selection(
    values: Sequence[str], allowed: Sequence[str], label: str
) -> None:
    if not values:
        raise ValueError("at least one selection is required")
    seen: set[str] = set()
    for value in values:
        if value not in allowed:
            raise ValueError(f"{label} does not support: {value}")
        if value in seen:
            raise ValueError(f"duplicate selection: {value}")
        seen.add(value)


def _require_sanitizer(sanitizer: str) -> None:
    if sanitizer not in SANITIZER_CONFIGURATIONS:
        raise ValueError(f"unsupported sanitizer: {sanitizer}")


def _require_runnable(architectures: Sequence[str], host: str | None) -> None:
    native.require_runnable(
        tuple(architectures), native.host_architecture() if host is None else host
    )


def _validator(
    runner: Runner, module: str, arguments: Sequence[str], output: Path
) -> None:
    """Invoke a core module CLI with the operation-scoped output environment."""

    output.mkdir(parents=True, exist_ok=True)
    directory = os.fspath(output)
    _checked(
        runner,
        (os.fspath(_PYTHON), "-m", module, *arguments),
        _BUILD_ROOT,
        {"OBSERVER_OUT_DIR": directory, "OBSERVER_BUILD_DIR": directory},
    )


def _llvm_properties(
    command: list[str], tools: DiagnosticsTools, runtime: Path | None
) -> None:
    if runtime is not None:
        command.append(f"/p:LLVMInstallDir={os.fspath(tools.llvm_install)}")
        command.append(f"/p:LLVMRuntimeDir={os.fspath(runtime)}")


def _observer_command(
    repository: Path,
    architecture: str,
    configuration: str,
    jobs: int,
    tools: DiagnosticsTools,
    *,
    runtime: Path | None = None,
) -> tuple[str, ...]:
    command = list(native.build_command(repository, architecture, configuration, jobs, tools))
    _llvm_properties(command, tools, runtime)
    return tuple(command)


def _project_command(
    repository: Path,
    project: Path,
    architecture: str,
    configuration: str,
    jobs: int,
    tools: DiagnosticsTools,
    *,
    runtime: Path | None = None,
) -> tuple[str, ...]:
    command = [
        str(tools.msbuild),
        os.fspath(project),
        "/nologo",
        f"/m:{jobs}",
        "/nr:false",
        "/t:Build",
        f"/p:Configuration={configuration}",
        f"/p:Platform={native.PLATFORMS[architecture]}",
        f"/p:ArtifactsRoot={native.artifacts_root(repository)}{_WINDOWS_SEPARATOR}",
        f"/p:VcpkgRoot={tools.vcpkg_root}",
        "/p:VcpkgManifestInstall=false",
    ]
    _llvm_properties(command, tools, runtime)
    return tuple(command)


def _restore_asan(
    runner: Runner, repository: Path, architecture: str, tools: DiagnosticsTools
) -> None:
    root = native.artifacts_root(repository)
    scratch = root / "vcpkg_scratch" / f"{architecture}{_ASAN_SUFFIX}"
    command = (
        str(tools.vcpkg),
        "install",
        f"--x-install-root={root / 'vcpkg_installed' / f'{architecture}{_ASAN_SUFFIX}'}",
        f"--x-buildtrees-root={scratch / 'buildtrees'}",
        f"--x-packages-root={scratch / 'packages'}",
        "--triplet",
        f"{native.triplet(architecture)}{_ASAN_SUFFIX}",
        f"--x-manifest-root={repository}",
        f"--overlay-triplets={repository / 'build' / 'vcpkg' / 'triplets'}",
    )
    _checked(runner, command, repository, None)


def _clean_profiles(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.profraw"):
        stale.unlink()


def _run_suite(
    runner: Runner,
    repository: Path,
    architecture: str,
    configuration: str,
    report: Path,
    environment: Mapping[str, str],
    corpus: Path | None = None,
) -> ToolResult:
    report.parent.mkdir(parents=True, exist_ok=True)
    return _checked(
        runner,
        native.test_command(repository, architecture, configuration, report, corpus),
        native.bin_directory(repository, architecture, configuration),
        environment,
    )


def _coverage_export(
    repository: Path,
    architecture: str,
    profile: Path,
    tools: DiagnosticsTools,
    *,
    lcov: bool = False,
) -> tuple[str, ...]:
    command = [
        str(tools.llvm_cov),
        "export",
        os.fspath(native.test_executable(repository, architecture, "Coverage")),
        "--instr-profile",
        os.fspath(profile),
        "--ignore-filename-regex",
        COVERAGE_IGNORED_SOURCES,
    ]
    binary = native.bin_directory(repository, architecture, "Coverage")
    for name in PROJECTS:
        command += ["--object", os.fspath(binary / native.BINARIES[name])]
    # The trimmed summary alone drives the 100% gate; the LCOV form keeps every
    # file, line and branch so downstream reporting gets the full detail.
    command.append("--format=lcov" if lcov else "--summary-only")
    return tuple(command)


def _install_runtime(tools: DiagnosticsTools, architecture: str, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        tools.asan_runtimes[architecture], directory / ASAN_RUNTIMES[architecture]
    )


def _seed_corpus(repository: Path, target: str, directory: Path) -> tuple[Path, ...]:
    """Materialise the checked-in seeds, decoding ``.hex`` fixtures to bytes."""

    source = repository / "src" / "fuzz" / "corpus" / target
    entries = (
        sorted(path for path in source.iterdir() if path.is_file())
        if source.is_dir()
        else []
    )
    if not entries:
        raise FileNotFoundError(f"no checked-in fuzzer seeds for {target}")
    # The hermetic seed directory is rebuilt from scratch every run so nothing a
    # previous run (or a caller-supplied prior corpus) left behind can linger.
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
    seeded = []
    for seed in entries:
        if seed.suffix == ".hex":
            destination = directory / seed.stem
            destination.write_bytes(
                bytes.fromhex("".join(seed.read_text(encoding="ascii").split()))
            )
        else:
            destination = directory / seed.name
            shutil.copyfile(seed, destination)
        seeded.append(destination)
    return tuple(seeded)


def _merge_corpus(source: Path, directory: Path) -> tuple[Path, ...]:
    """Copy every file from ``source`` into ``directory``, overwriting by name."""

    directory.mkdir(parents=True, exist_ok=True)
    merged = []
    for item in sorted(source.iterdir()):
        destination = directory / item.name
        shutil.copyfile(item, destination)
        merged.append(destination)
    return tuple(merged)


def _copy_corpus(source: Path, directory: Path) -> tuple[Path, ...]:
    """Rebuild ``directory`` as an exact copy of the files in ``source``."""

    shutil.rmtree(directory, ignore_errors=True)
    return _merge_corpus(source, directory)


def _prior_source(prior: Path, target: str) -> Path | None:
    """Resolve one target's accumulated corpus, either published or bare."""

    candidate = prior / target
    published = candidate / "corpus"
    if published.is_dir():
        return published
    if candidate.is_dir():
        return candidate
    return None


def _publish_corpus(source: Path, directory: Path) -> None:
    """Copy a run's accumulated corpus into the operation output, never deleting it."""

    directory.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir()):
        shutil.copyfile(item, directory / item.name)


def _is_reparse_point(path: Path) -> bool:
    try:
        status = path.lstat()
    except OSError:
        return False
    return bool(getattr(status, "st_file_attributes", 0) & _REPARSE_POINT)


def _validate_owned(directory: Path, root: Path) -> None:
    """Refuse a caller-owned path that escapes ``root`` or hides behind a link.

    The containment check resolves the whole path, so a nested junction that
    redirects outside the operation output is caught even when the leaf does not
    exist yet; the ancestor walk additionally rejects any reparse point component
    before anything is created or removed.
    """

    resolved_root = Path(root).resolve()
    if not Path(directory).resolve().is_relative_to(resolved_root):
        raise DiagnosticsError(
            f"refusing to use outside the operation output: {directory}"
        )
    for item in (Path(directory), *Path(directory).parents):
        if _is_reparse_point(item):
            raise DiagnosticsError(f"refusing to use a reparse point: {item}")


def _remove_owned(directory: Path, root: Path) -> None:
    """Delete one caller-owned directory, refusing links and escapes out of ``root``."""

    _validate_owned(directory, root)
    if not directory.exists():
        return
    shutil.rmtree(directory)


def _fresh_measure(directory: Path, root: Path) -> None:
    """Recreate the per-scope measure output that ``core.leak`` expects to be new."""

    _validate_owned(directory, root)
    directory.mkdir(parents=True, exist_ok=True)
    for name in _MEASURE_DIRECTORIES:
        _remove_owned(directory / name, root)


def _fuzz_command(
    executable: Path, arguments: Sequence[str], artifacts: Path
) -> tuple[str, ...]:
    """Every libFuzzer run shares the bounded memory, timeout and artifact flags."""

    return (
        os.fspath(executable),
        *arguments,
        "-rss_limit_mb=1024",
        "-timeout=10",
        "-print_final_stats=1",
        f"-artifact_prefix={os.fspath(artifacts)}{_WINDOWS_SEPARATOR}",
    )


def _replay(
    runner: Runner,
    executable: Path,
    inputs: Sequence[Path],
    max_length: int,
    artifacts: Path,
    log: Path,
) -> int:
    """Replay exact inputs, always writing the log, and return the exit status."""

    result = runner(
        _fuzz_command(
            executable,
            [*(os.fspath(item) for item in inputs), f"-max_len={max_length}"],
            artifacts,
        ),
        None,
        {"ASAN_OPTIONS": _ASAN_OPTIONS},
    )
    log.write_text(result.stdout, encoding="utf-8")
    return result.returncode


def _timed(
    runner: Runner,
    executable: Path,
    corpus: Path,
    seconds: int,
    max_length: int,
    artifacts: Path,
    log: Path,
) -> int:
    """Run the bounded libFuzzer budget and return its exit status."""

    result = runner(
        _fuzz_command(
            executable,
            [os.fspath(corpus), f"-max_total_time={seconds}", f"-max_len={max_length}",
             "-use_value_profile=1"],
            artifacts,
        ),
        None,
        {"ASAN_OPTIONS": _ASAN_OPTIONS},
    )
    log.write_text(result.stdout, encoding="utf-8")
    return result.returncode


def _default_auditor(
    repository: Path, architectures: Sequence[str], output: Path, *, include_leak_probe: bool
) -> None:
    """Delegate the Release PE/BinSkim audit to the repository packaging operations.

    ``packaging_ops`` is imported lazily so a staging workspace can inject its
    own auditor without the repository module being importable.  The exact
    contract is ``audit_binaries(repository, architectures, output,
    include_leak_probe=True)``; it raises on any finding.
    """

    import packaging_ops

    # ``packaging_ops.audit_binaries`` requires an existing output root and runs its
    # own containment and reparse-point validation below that root; materialise the
    # operation's audit directory first so the default path never fails on a missing one.
    # Because packaging only validates the root and below, reject the audit directory and
    # every ancestor (including a linked parent) here, before anything is created.
    output = Path(output)
    _validate_owned(output, output.parent)
    output.mkdir(parents=True, exist_ok=True)
    packaging_ops.audit_binaries(
        repository, tuple(architectures), output, include_leak_probe=include_leak_probe
    )


def test_coverage(
    repository: Path,
    architectures: Sequence[str],
    output: Path,
    *,
    tools: DiagnosticsTools,
    runner: Runner = run_tool,
    jobs: int | None = None,
    host: str | None = None,
    corpus: Path | None = None,
) -> tuple[Path, ...]:
    """Build Coverage, run the ordinary suite and gate 100% LLVM coverage.

    The ordinary suite always runs.  When ``corpus`` names an existing
    directory the compatibility suite runs as a separate pass with
    ``OBSERVER_TEST_CORPUS`` scoped to it, and both suites' raw profiles are
    merged before the single immutable 100% line/branch gate.  Each architecture
    yields the summary JSON that gate consumes and the full LCOV detail that is
    written next to it under ``coverage/<arch>``.
    """

    repository = Path(repository)
    output = Path(output)
    architectures = tuple(architectures)
    _require_selection(architectures, COVERAGE_ARCHITECTURES, "coverage")
    _require_runnable(architectures, host)
    corpus_path: Path | None = None
    if corpus is not None:
        corpus_path = Path(corpus).resolve(strict=True)
        if not corpus_path.is_dir():
            raise NotADirectoryError(corpus_path)
    count = _job_count(jobs)
    native.restore(repository, architectures, tools=tools, runner=_checked_runner(runner))
    reports = []
    for architecture in architectures:
        _checked(
            runner,
            _observer_command(
                repository, architecture, "Coverage", count, tools,
                runtime=tools.llvm_runtimes[architecture],
            ),
            repository,
            None,
        )
        directory = output / "coverage" / architecture
        directory.mkdir(parents=True, exist_ok=True)
        profiles = native.artifacts_root(repository) / "coverage" / architecture
        suites: list[tuple[str, Path | None, Mapping[str, str]]] = [("unit", None, {})]
        if corpus_path is not None:
            suites.append(
                ("corpus", corpus_path, {"OBSERVER_TEST_CORPUS": os.fspath(corpus_path)})
            )
        raw: list[Path] = []
        for scope, suite_corpus, extra in suites:
            # Each scope gets its own JUnit report and its own profraw directory
            # so the compatibility pass can never contaminate the ordinary one.
            scope_profiles = profiles / scope
            _clean_profiles(scope_profiles)
            environment = {
                "LLVM_PROFILE_FILE": os.fspath(
                    scope_profiles / "coverage-%m-%p.profraw"
                ),
                **extra,
            }
            _run_suite(
                runner, repository, architecture, "Coverage",
                directory / scope / "tests.xml", environment, suite_corpus,
            )
            produced = sorted(scope_profiles.glob("*.profraw"))
            if not produced:
                raise DiagnosticsError(
                    f"coverage {scope} suite produced no LLVM raw profiles"
                )
            raw.extend(produced)
        profile = directory / "coverage.profdata"
        _checked(
            runner,
            (str(tools.llvm_profdata), "merge", "-sparse",
             *(os.fspath(item) for item in raw), "-o", os.fspath(profile)),
            None, None,
        )
        report = directory / "coverage.json"
        report.write_text(
            _checked(
                runner, _coverage_export(repository, architecture, profile, tools), None, None
            ).stdout,
            encoding="utf-8",
        )
        lcov = directory / "coverage.lcov"
        lcov.write_text(
            _checked(
                runner,
                _coverage_export(repository, architecture, profile, tools, lcov=True),
                None,
                None,
            ).stdout,
            encoding="utf-8",
        )
        _validator(runner, "core.cpp_coverage", ("gate", os.fspath(report)), directory)
        reports.append(report)
        reports.append(lcov)
    return tuple(reports)


def test_sanitizer(
    repository: Path,
    sanitizer: str,
    architectures: Sequence[str],
    output: Path,
    *,
    tools: DiagnosticsTools,
    runner: Runner = run_tool,
    jobs: int | None = None,
    host: str | None = None,
) -> tuple[Path, ...]:
    """Build ASan/UBSan, run the suite and gate the suite log for findings."""

    repository = Path(repository)
    output = Path(output)
    architectures = tuple(architectures)
    _require_sanitizer(sanitizer)
    _require_selection(architectures, tuple(SANITIZER_ARCHITECTURES[sanitizer]), sanitizer)
    _require_runnable(architectures, host)
    count = _job_count(jobs)
    configuration = SANITIZER_CONFIGURATIONS[sanitizer]
    name, value = SANITIZER_ENVIRONMENT[sanitizer]
    logs = []
    for architecture in architectures:
        if sanitizer == "asan":
            _restore_asan(runner, repository, architecture, tools)
            runtime = None
        else:
            native.restore(
                repository, (architecture,), tools=tools, runner=_checked_runner(runner)
            )
            runtime = tools.llvm_runtimes[architecture]
        _checked(
            runner,
            _observer_command(repository, architecture, configuration, count, tools,
                              runtime=runtime),
            repository,
            None,
        )
        directory = output / "sanitizer" / sanitizer / architecture
        directory.mkdir(parents=True, exist_ok=True)
        if sanitizer == "asan":
            _install_runtime(
                tools, architecture,
                native.bin_directory(repository, architecture, configuration),
            )
        result = _run_suite(
            runner, repository, architecture, configuration, directory / "tests.xml",
            {name: value},
        )
        if not (directory / "tests.xml").is_file():
            raise DiagnosticsError("sanitizer suite did not produce tests.xml")
        log = directory / "tests.log"
        log.write_text(result.stdout, encoding="utf-8")
        _validator(runner, "core.sanitizer", ("gate", sanitizer, os.fspath(log)), directory)
        logs.append(log)
    return tuple(logs)


def fuzz(
    repository: Path,
    architectures: Sequence[str],
    targets: Sequence[str],
    seconds: int,
    output: Path,
    *,
    tools: DiagnosticsTools,
    runner: Runner = run_tool,
    jobs: int | None = None,
    host: str | None = None,
    prior_corpus: Path | None = None,
) -> tuple[Path, ...]:
    """Build every target; x64 runs the timed budget, x86 the replay-only gate.

    Seeds live in their own directory and are replayed on their own, so a prior
    corpus sharing a seed name can never weaken the mandatory hermetic x64
    replay.  A successful x64 run publishes its accumulated corpus and exit
    status into the returned output tree, where the next invocation can reuse
    it; a failing run leaves its log, status and crash artifacts in place.
    """

    repository = Path(repository)
    output = Path(output)
    architectures = tuple(architectures)
    targets = tuple(targets)
    _require_selection(architectures, FUZZ_ARCHITECTURES, "fuzz")
    _require_selection(targets, FUZZ_TARGETS, "fuzz")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise ValueError("fuzz seconds must be a positive integer")
    _require_runnable(architectures, host)
    count = _job_count(jobs)
    prior = Path(prior_corpus) if prior_corpus is not None else None
    if prior is not None and not prior.is_dir():
        raise NotADirectoryError(prior)
    outputs = []
    for architecture in architectures:
        _restore_asan(runner, repository, architecture, tools)
        timed = architecture == FUZZ_TIMED_ARCHITECTURE
        for target in targets:
            _checked(
                runner,
                _project_command(
                    repository, repository / "build" / "projects" / f"fuzz-{target}.vcxproj",
                    architecture, "Fuzz", count, tools,
                    runtime=tools.llvm_runtimes[architecture],
                ),
                repository,
                None,
            )
            executable = (
                native.bin_directory(repository, architecture, "Fuzz") / f"fuzz-{target}.exe"
            )
            directory = output / "fuzz" / architecture / target
            directory.mkdir(parents=True, exist_ok=True)
            artifacts = directory / "artifacts"
            artifacts.mkdir(parents=True, exist_ok=True)
            work = native.artifacts_root(repository) / "fuzz" / architecture / target
            length = FUZZ_MAX_LENGTH[target]
            seeds = _seed_corpus(repository, target, work / "seeds")
            prior_files: tuple[Path, ...] = ()
            if prior is not None and (source := _prior_source(prior, target)) is not None:
                prior_files = _copy_corpus(source, work / "prior")
            status = directory / "status.txt"
            replay_log = directory / "replay.log"
            run_log = directory / "run.log"
            # A new run owns the phase evidence: drop the previous run's status and
            # logs so a stale success can never outlive a later failure.  The
            # accumulated corpus is deliberately preserved for reuse.
            for stale in (status, replay_log, run_log):
                stale.unlink(missing_ok=True)
            inputs = seeds if timed else (*seeds, *prior_files)
            replay = _replay(runner, executable, inputs, length, artifacts, replay_log)
            if replay:
                status.write_text(f"{replay}\n", encoding="utf-8")
                raise DiagnosticsError(f"libFuzzer replay failed with exit code {replay}")
            if timed:
                run = work / "run"
                _copy_corpus(work / "seeds", run)
                if prior_files:
                    _merge_corpus(work / "prior", run)
                code = _timed(
                    runner, executable, run, seconds, length, artifacts, run_log
                )
                status.write_text(f"{code}\n", encoding="utf-8")
                if code:
                    raise DiagnosticsError(f"libFuzzer failed with exit code {code}")
                _publish_corpus(run, directory / "corpus")
            else:
                _validator(
                    runner, "core.sanitizer", ("gate", "asan", os.fspath(replay_log)), directory
                )
            outputs.append(directory)
    return tuple(outputs)


def _require_leak_measurement(
    warmup: int, iterations: int, windows: int, tolerance_bytes: int
) -> None:
    for name, value in (("warmup", warmup), ("iterations", iterations), ("windows", windows)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"leak {name} must be a positive integer")
    if windows < 3:
        raise ValueError("leak measurement requires at least three windows")
    if isinstance(tolerance_bytes, bool) or not isinstance(tolerance_bytes, int) or tolerance_bytes < 0:
        raise ValueError("leak tolerance must be a non-negative integer")


def test_leaks(
    repository: Path,
    output: Path,
    warmup: int = 8,
    iterations: int = 100,
    windows: int = 3,
    tolerance_bytes: int = 0,
    *,
    tools: DiagnosticsTools,
    runner: Runner = run_tool,
    jobs: int | None = None,
    host: str | None = None,
    auditor: Auditor | None = None,
) -> tuple[Path, ...]:
    """Build the x64 Release probe and gate UMDH growth through ``core.leak``.

    The built Release binaries, including the leak probe, are audited for PE
    and BinSkim policy before any leak scenario runs.  ``auditor`` defaults to
    the repository ``packaging_ops.audit_binaries`` and is called with
    ``(repository, ("x64",), output/"audit", include_leak_probe=True)``; it must
    raise on a finding, and passing ``None`` never skips the audit.
    """

    repository = Path(repository)
    output = Path(output)
    _require_runnable(("x64",), host)
    _require_leak_measurement(warmup, iterations, windows, tolerance_bytes)
    count = _job_count(jobs)
    native.restore(repository, ("x64",), tools=tools, runner=_checked_runner(runner))
    _checked(
        runner, _observer_command(repository, "x64", "Release", count, tools), repository, None
    )
    _checked(
        runner,
        _project_command(
            repository, repository / "build" / "projects" / "leak-probe.vcxproj",
            "x64", "Release", count, tools,
        ),
        repository,
        None,
    )
    audit = _default_auditor if auditor is None else auditor
    audit(repository, ("x64",), output / "audit", include_leak_probe=True)
    binary = native.bin_directory(repository, "x64", "Release")
    prepared = output / "leak" / "setup"
    _validator(
        runner,
        "core.leak",
        ("setup", os.fspath(binary / LEAK_PROBE),
         *(os.fspath(binary / native.BINARIES[name]) for name in PROJECTS)),
        prepared,
    )
    summaries = []
    for mode in LEAK_MODES:
        for scenario in LEAK_SCENARIOS:
            scope = output / "leak" / mode / scenario
            _validator(
                runner, "core.leak",
                ("preflight", os.fspath(prepared), mode, scenario), scope / "preflight",
            )
            measure = scope / "measure"
            _fresh_measure(measure, output)
            _validator(
                runner, "core.leak",
                ("measure", os.fspath(prepared), os.fspath(tools.umdh), mode, scenario,
                 str(warmup), str(iterations), str(windows), str(tolerance_bytes)),
                measure,
            )
            summary = measure / "summary.json"
            _validator(runner, "core.leak", ("gate", os.fspath(summary)), scope / "gate")
            summaries.append(summary)
    return tuple(summaries)

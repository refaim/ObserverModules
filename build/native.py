"""Direct native MSBuild and Catch2 route for ObserverModules.

The content-addressed graph drove MSBuild project by project through rendered
PowerShell.  This module is the smaller successor for the native family: it
invokes vcpkg and MSBuild straight from Python with stable directories under
``out/native`` and lets MSBuild own project dependencies, scheduling and
incremental object state.  It needs only MSVC and vcpkg (not LLVM, UMDH or
BinSkim) and is self-contained: the standard library only, no graph and no CAS.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys

from core import host as host_policy


ARCHITECTURES: tuple[str, ...] = ("x86", "x64", "arm64")
PLATFORMS: Mapping[str, str] = {"x86": "Win32", "x64": "x64", "arm64": "ARM64"}
# The direct route builds only what MSVC alone provides.  Coverage, sanitizer,
# fuzz and UBSan configurations select ClangCL and LLVM, which stay out of scope.
CONFIGURATIONS: tuple[str, ...] = ("Debug", "Release")
BINARIES: Mapping[str, str] = {
    "renpy": "renpy.so",
    "rpgmaker": "rpgmaker.so",
    "zanzarah": "zanzarah.so",
    "tests": "tests.exe",
}
PROJECTS: tuple[str, ...] = tuple(BINARIES)

REPOSITORY = Path(__file__).resolve().parents[1]


Runner = Callable[[Sequence[str], Path | None, Mapping[str, str] | None], None]


@dataclass(frozen=True, slots=True)
class NativeTools:
    msbuild: Path
    vcpkg: Path

    @property
    def vcpkg_root(self) -> Path:
        return self.vcpkg.parent


def run_tool(argv: Sequence[str], cwd: Path | None = None, env: Mapping[str, str] | None = None) -> None:
    """Run one native tool, letting a nonzero exit raise ``CalledProcessError``."""

    subprocess.run(
        [os.fspath(item) for item in argv],
        cwd=None if cwd is None else os.fspath(cwd),
        env={**os.environ, **(env or {})},
        check=True,
    )


def _required_file(path: Path, description: str) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{description} not found: {path}")
    return resolved


def vswhere_path(environ: Mapping[str, str]) -> Path:
    base = environ.get("ProgramFiles(x86)")
    if not base:
        raise FileNotFoundError("ProgramFiles(x86) is not set; cannot locate vswhere.exe")
    return Path(base) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"


def find_msbuild(vswhere: Path, *, run: Callable[..., object] = subprocess.run) -> Path:
    """Ask vswhere for the newest MSVC installation and pick its MSBuild binary.

    A bare ``Microsoft.Component.MSBuild`` requirement is satisfied by .NET-only
    installations, so the query also requires the VS2022/v143 C++ build tools.
    Diagnostics tooling such as LLVM/Clang is deliberately not required.
    """

    argv = [
        str(vswhere),
        "-latest",
        "-products",
        "*",
        "-requires",
        "Microsoft.Component.MSBuild",
        "-requires",
        "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
        "-property",
        "installationPath",
    ]
    result = run(argv, capture_output=True, text=True, check=True)
    installation = next(
        (line.strip() for line in str(result.stdout).splitlines() if line.strip()), None
    )
    if installation is None:
        raise RuntimeError("vswhere did not report a Visual Studio installation")
    directory = Path(installation)
    amd64 = directory / "MSBuild" / "Current" / "Bin" / "amd64" / "MSBuild.exe"
    plain = directory / "MSBuild" / "Current" / "Bin" / "MSBuild.exe"
    return _required_file(amd64 if amd64.is_file() else plain, "MSBuild.exe")


def find_vcpkg(environ: Mapping[str, str]) -> Path:
    """Resolve vcpkg from ``VCPKG_ROOT``, refusing a bare PATH shim."""

    root = environ.get("VCPKG_ROOT")
    if not root:
        raise FileNotFoundError("VCPKG_ROOT is not set; refusing to trust a PATH shim")
    integration = Path(root) / "scripts" / "buildsystems" / "msbuild" / "vcpkg.props"
    if not integration.is_file():
        raise FileNotFoundError(f"VCPKG_ROOT is not a vcpkg checkout: {root}")
    return _required_file(Path(root) / "vcpkg.exe", "vcpkg.exe")


def locate_tools(
    *, environ: Mapping[str, str] | None = None, run: Callable[..., object] = subprocess.run
) -> NativeTools:
    values = os.environ if environ is None else environ
    msbuild = find_msbuild(_required_file(vswhere_path(values), "vswhere.exe"), run=run)
    return NativeTools(msbuild=msbuild, vcpkg=find_vcpkg(values))


def artifacts_root(repository: Path) -> Path:
    return Path(repository) / "out" / "native"


def restore_root(repository: Path, architecture: str) -> Path:
    return artifacts_root(repository) / "vcpkg_installed" / architecture


def bin_directory(repository: Path, architecture: str, configuration: str) -> Path:
    return artifacts_root(repository) / "bin" / architecture / configuration


def artifacts(repository: Path, architecture: str, configuration: str) -> tuple[Path, ...]:
    directory = bin_directory(repository, architecture, configuration)
    return tuple(directory / BINARIES[project] for project in PROJECTS)


def test_executable(repository: Path, architecture: str, configuration: str) -> Path:
    return bin_directory(repository, architecture, configuration) / BINARIES["tests"]


def triplet(architecture: str) -> str:
    return f"observer-{architecture}-windows-static"


def restore_command(repository: Path, architecture: str, tools: NativeTools) -> tuple[str, ...]:
    root = artifacts_root(repository)
    scratch = root / "vcpkg_scratch" / architecture
    return (
        str(tools.vcpkg),
        "install",
        f"--x-install-root={restore_root(repository, architecture)}",
        f"--x-buildtrees-root={scratch / 'buildtrees'}",
        f"--x-packages-root={scratch / 'packages'}",
        "--triplet",
        triplet(architecture),
        f"--x-manifest-root={repository}",
        f"--overlay-triplets={repository / 'build' / 'vcpkg' / 'triplets'}",
    )


def build_command(
    repository: Path, architecture: str, configuration: str, jobs: int, tools: NativeTools
) -> tuple[str, ...]:
    # The trailing separator is required: the props concatenate the root with a
    # relative segment, so ``out\native`` without it would build ``out\nativebin``.
    root = f"{artifacts_root(repository)}{os.sep}"
    return (
        str(tools.msbuild),
        str(repository / "build" / "Observer.proj"),
        "/nologo",
        f"/m:{jobs}",
        "/nr:false",
        "/t:Build",
        f"/p:Configuration={configuration}",
        f"/p:Platform={PLATFORMS[architecture]}",
        f"/p:ArtifactsRoot={root}",
        f"/p:VcpkgRoot={tools.vcpkg.parent}",
        "/p:VcpkgManifestInstall=false",
    )


def test_command(
    repository: Path, architecture: str, configuration: str, report: Path, corpus: Path | None
) -> tuple[str, ...]:
    command = [str(test_executable(repository, architecture, configuration))]
    if corpus is not None:
        command.append("[compatibility]")
    command += [
        "--reporter", "compact",
        "--reporter", f"JUnit::out={report}",
        "--durations", "yes",
        "--order", "lex",
    ]
    return tuple(command)


def host_architecture(machine: str | None = None) -> str:
    """Canonicalise a machine name through the shared ``core.host`` policy."""

    try:
        return host_policy.detect_host_architecture(machine)
    except RuntimeError as error:
        raise ValueError(str(error)) from error


def require_runnable(architectures: Sequence[str], host: str) -> None:
    """Refuse targets the host cannot execute, using the shared runnable matrix."""

    try:
        host_policy.require_runnable(tuple(architectures), host)
    except RuntimeError as error:
        raise ValueError(str(error)) from error


def _validate_architectures(architectures: Sequence[str]) -> None:
    if not architectures:
        raise ValueError("at least one architecture is required")
    for architecture in architectures:
        if architecture not in PLATFORMS:
            raise ValueError(f"unsupported architecture: {architecture}")


def _validate_configurations(configurations: Sequence[str]) -> None:
    if not configurations:
        raise ValueError("at least one configuration is required")
    for configuration in configurations:
        if configuration not in CONFIGURATIONS:
            raise ValueError(f"unsupported configuration: {configuration}")


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def restore(
    repository: Path,
    architectures: Sequence[str] = ("x64",),
    *,
    tools: NativeTools | None = None,
    runner: Runner | None = None,
) -> tuple[Path, ...]:
    repository = Path(repository).resolve()
    architectures = _unique(architectures)
    _validate_architectures(architectures)
    tools = tools or locate_tools()
    execute = runner or run_tool
    roots = []
    for architecture in architectures:
        execute(restore_command(repository, architecture, tools), repository, None)
        roots.append(restore_root(repository, architecture))
    return tuple(roots)


def build(
    repository: Path,
    architectures: Sequence[str] = ("x64",),
    configurations: Sequence[str] = ("Debug",),
    *,
    jobs: int | None = None,
    tools: NativeTools | None = None,
    runner: Runner | None = None,
) -> tuple[Path, ...]:
    repository = Path(repository).resolve()
    architectures = _unique(architectures)
    configurations = _unique(configurations)
    _validate_architectures(architectures)
    _validate_configurations(configurations)
    if jobs is None:
        jobs = os.cpu_count() or 1
    elif jobs < 1:
        raise ValueError("jobs must be a positive integer")
    tools = tools or locate_tools()
    execute = runner or run_tool
    restore(repository, architectures, tools=tools, runner=execute)
    outputs: list[Path] = []
    for architecture in architectures:
        for configuration in configurations:
            execute(
                build_command(repository, architecture, configuration, jobs, tools),
                repository, None,
            )
            outputs.extend(artifacts(repository, architecture, configuration))
    return tuple(outputs)


def test(
    repository: Path,
    architectures: Sequence[str] = ("x64",),
    configurations: Sequence[str] = ("Debug",),
    *,
    corpus: Path | None = None,
    host: str | None = None,
    jobs: int | None = None,
    tools: NativeTools | None = None,
    runner: Runner | None = None,
) -> tuple[Path, ...]:
    """Build and run Catch2 for every variant, returning each JUnit report.

    The ordinary hermetic suite always runs for every runnable variant.  A
    compatibility corpus is optional and, when supplied, is appended as a
    second suite per variant (with ``OBSERVER_TEST_CORPUS`` scoped to it only).
    Both report sets are returned together, unit reports first, so a caller
    never has to duplicate the ordinary run.
    """

    repository = Path(repository).resolve()
    architectures = _unique(architectures)
    configurations = _unique(configurations)
    _validate_architectures(architectures)
    _validate_configurations(configurations)
    require_runnable(architectures, host or host_architecture())
    corpus_path: Path | None = None
    if corpus is not None:
        corpus_path = Path(corpus).resolve(strict=True)
        if not corpus_path.is_dir():
            raise NotADirectoryError(corpus_path)
    execute = runner or run_tool
    build(repository, architectures, configurations, jobs=jobs, tools=tools, runner=execute)
    reports: list[Path] = []
    for architecture in architectures:
        for configuration in configurations:
            # The ordinary hermetic suite always runs; an optional compatibility
            # corpus is appended as a separate suite rather than replacing it.
            suites: list[tuple[Path | None, Mapping[str, str] | None]] = [(None, None)]
            if corpus_path is not None:
                suites.append(
                    (corpus_path, {"OBSERVER_TEST_CORPUS": str(corpus_path)})
                )
            for suite_corpus, environment in suites:
                scope = "corpus" if suite_corpus is not None else "unit"
                report = (
                    artifacts_root(repository) / "reports" / "tests"
                    / architecture / configuration / scope / "tests.xml"
                )
                report.parent.mkdir(parents=True, exist_ok=True)
                execute(
                    test_command(
                        repository, architecture, configuration, report, suite_corpus
                    ),
                    bin_directory(repository, architecture, configuration),
                    environment,
                )
                reports.append(report)
    return tuple(reports)


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


def _positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a positive integer") from error
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="observer-native")
    commands = parser.add_subparsers(dest="command", required=True)
    restore_parser = commands.add_parser("restore")
    build_parser = commands.add_parser("build")
    test_parser = commands.add_parser("test")
    for command in (restore_parser, build_parser, test_parser):
        command.add_argument("-Repository", "--repository", type=Path, default=REPOSITORY)
        command.add_argument("-Arch", "--arch", type=_selection(ARCHITECTURES), default=("x64",))
    for command in (build_parser, test_parser):
        command.add_argument("-Config", "--config", type=_selection(CONFIGURATIONS), default=("Debug",))
        command.add_argument("-Jobs", "--jobs", type=_positive_integer, default=None)
    test_parser.add_argument("-Corpus", "--corpus", type=Path, default=None)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    tools: NativeTools | None = None,
    runner: Runner | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(arguments)
    execute = runner or run_tool
    try:
        resolved = tools or locate_tools(environ=environ)
        if args.command == "restore":
            outputs = restore(args.repository, args.arch, tools=resolved, runner=execute)
        elif args.command == "build":
            outputs = build(
                args.repository, args.arch, args.config, jobs=args.jobs,
                tools=resolved, runner=execute,
            )
        else:
            outputs = test(
                args.repository, args.arch, args.config, corpus=args.corpus,
                tools=resolved, runner=execute,
            )
    except subprocess.CalledProcessError as error:
        print(
            f"observer-native: command failed with exit code {error.returncode}: {error.cmd}",
            file=sys.stderr,
        )
        return error.returncode or 1
    except (ValueError, OSError) as error:
        print(f"observer-native: {error}", file=sys.stderr)
        return 2
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

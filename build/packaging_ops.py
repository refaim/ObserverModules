"""Direct audit and packaging over the stable ``out/native`` Release artifacts.

This is the console-first, standard-library successor to the content-addressed
``graphs/audit.py`` and ``graphs/package.py`` families.  It expresses the same
release gates as plain Python that shells out to the already-validated ``core``
command line modules -- ``core.binary_audit``, ``core.module_version`` and
``core.package`` -- each with an operation-scoped ``OBSERVER_OUT_DIR``.  It owns no
graph, cache or scheduler: MSBuild keeps the incremental state and ``core`` keeps
the checks, so no gate is restated or weakened here.

Independent per-module releases (``renpy/vX.Y.Z`` and friends) ship only the
selected module's assets, and every PDB archive is scoped to one module and its
version.  A single-architecture run writes the same ``<module>-<version>-pdb.zip``
name as the all-architecture local run, carrying only that one architecture as
``<arch>/<module>.pdb``; the release gate flattens the per-architecture bundles into
the published one.  ``core.package``'s ``aggregate`` command hard-codes the full
module set, so this route reports a manifest that ``core.release`` accepts once its
complete-module-set requirement is relaxed to the selected set (see ``package``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import zipfile

from core.host import DeferredGate, runnable_architectures
from core.module_version import VERSION_FILE, content_identity, read_version, shared_content_inputs
# A per-module symbol bundle reuses the deterministic ZIP writer and the staging payload
# validator rather than restating them.
from core.package import LICENSES, MODULES, _zip, module_archive_name
from core.quality_tools import resolve_binskim, resolve_dumpbin
from core.toolchain import discover_msvc_toolchain
from native import ARCHITECTURES, bin_directory, locate_tools


BUILD_ROOT = Path(__file__).resolve().parent
LEAK_PROBE = "leak-probe"
ALL = "all"
RELEASE = "Release"
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
# Undeclared first-party headers reachable beside a production source must not be lost, so the
# conservative inventory keeps the shared trees and the API headers at the source root.
_CONSERVATIVE_HEADER_TREES = ("src/common", "src/core")
# The MSBuild configuration every selected project imports, mirroring the graph's
# ``COMMON_PROJECT_INPUTS``.  A change here recompiles a module without touching its sources, so
# the release identity must sign it; the module ``VERSION`` file stays excluded instead.
_SHARED_BUILD_INPUTS = (
    "build/ObserverProjectConfigurations.props",
    "build/ObserverConfiguration.props",
    "build/ObserverProject.props",
    "build/ObserverModuleVersion.props",
)
Runner = Callable[..., None]


class PackagingError(RuntimeError):
    """A release artifact, resource, or output path refused the release contract."""


@dataclass(frozen=True, slots=True)
class PackagingTools:
    """The external tools the direct route resolves once per operation."""

    msbuild: Path
    vcpkg_root: Path
    dumpbin: Path
    binskim: Path


@dataclass(frozen=True, slots=True)
class PackageResult:
    """The archives written and the gates deferred to a host that can run them."""

    archives: tuple[Path, ...]
    deferred: tuple[DeferredGate, ...]


def locate_packaging_tools(*, environ: Mapping[str, str] | None = None) -> PackagingTools:
    """Resolve MSBuild, vcpkg, dumpbin and BinSkim once from the installed toolchain."""

    native = locate_tools(environ=environ)
    toolchain = discover_msvc_toolchain()
    return PackagingTools(
        msbuild=native.msbuild,
        vcpkg_root=native.vcpkg_root,
        dumpbin=resolve_dumpbin(toolchain).path,
        binskim=resolve_binskim().path,
    )


def _default_runner(argv, *, cwd=None, env=None, stdout=None) -> None:
    """Run one tool, letting a nonzero exit raise ``CalledProcessError``."""

    subprocess.run(
        [os.fspath(item) for item in argv],
        cwd=None if cwd is None else os.fspath(cwd),
        env=None if env is None else {**os.environ, **env},
        check=True,
        stdout=stdout,
    )


def _modules(selection: str | Iterable[str] | None) -> tuple[str, ...]:
    if selection is None or selection == ALL:
        return MODULES
    values = (selection,) if isinstance(selection, str) else tuple(selection)
    if not values:
        raise ValueError("module selection must not be empty")
    unknown = next((module for module in values if module not in MODULES), None)
    if unknown is not None:
        raise ValueError(f"unknown module: {unknown}")
    if len(set(values)) != len(values):
        raise ValueError("module selection must not repeat a module")
    return tuple(sorted(values, key=MODULES.index))


def _architectures(architectures: Iterable[str]) -> tuple[str, ...]:
    values = tuple(architectures)
    if not values:
        raise ValueError("at least one architecture is required")
    unknown = next((item for item in values if item not in ARCHITECTURES), None)
    if unknown is not None:
        raise ValueError(f"unsupported architecture: {unknown}")
    if len(set(values)) != len(values):
        raise ValueError("architecture list must not repeat an architecture")
    return tuple(sorted(values, key=ARCHITECTURES.index))


def _smoke(architectures: tuple[str, ...], smoke_architectures: Iterable[str] | None) -> tuple[str, ...]:
    if smoke_architectures is None:
        return architectures
    values = tuple(smoke_architectures)
    unknown = next((item for item in values if item not in architectures), None)
    if unknown is not None:
        raise ValueError(f"smoke architecture is not selected: {unknown}")
    if len(set(values)) != len(values):
        raise ValueError("smoke architecture list must not repeat an architecture")
    return tuple(sorted(values, key=architectures.index))


def _is_reparse(path: Path) -> bool:
    try:
        information = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(information, "st_file_attributes", 0)
    return stat.S_ISLNK(information.st_mode) or bool(attributes & REPARSE)


def _confined(candidate: Path, root: Path) -> Path:
    """Return ``candidate`` only when it stays below ``root`` without any reparse hop."""

    path = Path(os.path.abspath(os.fspath(candidate)))
    base = Path(os.path.abspath(os.fspath(root)))
    if path != base and base not in path.parents:
        raise PackagingError(f"path escapes the output tree: {path}")
    current = path
    while True:
        if _is_reparse(current):
            raise PackagingError(f"reparse point is forbidden below the output tree: {current}")
        if current == base:
            return path
        current = current.parent


def _output_root(output: Path) -> Path:
    root = Path(output)
    if not root.is_dir():
        raise PackagingError(f"output directory must be an existing directory: {root}")
    return _confined(root, root)


def _directory(directory: Path, root: Path) -> Path:
    target = _confined(directory, root)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _reset_directory(directory: Path, root: Path) -> Path:
    """Remove one stale staging subdirectory and recreate it empty."""

    target = _confined(directory, root)
    if target.exists():
        if not target.is_dir():
            raise PackagingError(f"staging path is not a directory: {target}")
        _reject_reparse_children(target, root)
        shutil.rmtree(target)
    target.mkdir(parents=True)
    return target


def _reject_reparse_children(directory: Path, root: Path) -> None:
    """Refuse to remove a tree that hides a reparse hop anywhere below it."""

    pending = [_confined(directory, root)]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                child = Path(entry.path)
                if _is_reparse(child):
                    raise PackagingError(f"reparse point is forbidden below the output tree: {child}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(child)


def _leaf(path: Path, root: Path) -> Path:
    """Return one writable output leaf, never following an existing link or directory."""

    target = _confined(path, root)
    if target.is_dir():
        raise PackagingError(f"output leaf is a directory: {target}")
    return target


def _publish(pending: Path, publish: Path, root: Path) -> Path:
    """Swap a freshly staged tree into place, keeping the previous one if the move fails.

    A published release is moved aside to ``<name>.previous`` first, so a failed ``os.replace``
    (for example a sharing violation on the manifest) can roll the old bytes back instead of
    leaving the release destroyed.  No transaction framework is involved, only this one rename.
    """

    target = _confined(publish, root)
    backup: Path | None = None
    if target.exists():
        if not target.is_dir():
            raise PackagingError(f"publish path is not a directory: {target}")
        _reject_reparse_children(target, root)
        backup = _confined(target.parent / f"{target.name}.previous", root)
        if backup.exists():
            _reject_reparse_children(backup, root)
            shutil.rmtree(backup)
        os.replace(target, backup)
    try:
        os.replace(pending, target)
    except OSError:
        if backup is not None:
            os.replace(backup, target)
        raise
    if backup is not None:
        shutil.rmtree(backup)
    return target


def _environment(out: Path) -> dict[str, str]:
    return {"OBSERVER_OUT_DIR": os.fspath(out)}


def _python(module: str) -> tuple[str, ...]:
    return (sys.executable, "-m", module)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _binaries(repository: Path, architecture: str) -> Path:
    """Return the stable Release directory one architecture's binaries live in."""

    return bin_directory(repository, architecture, RELEASE)


def _artifact(binaries: Path, module: str) -> Path:
    name = "leak-probe.exe" if module == LEAK_PROBE else f"{module}.so"
    path = binaries / name
    if not path.is_file():
        raise PackagingError(f"release artifact is missing: {path}")
    return path


def _symbol(binaries: Path, module: str) -> Path:
    path = binaries / f"{module}.pdb"
    if not path.is_file():
        raise PackagingError(f"release symbols are missing: {path}")
    return path


def _test_runner(binaries: Path) -> Path:
    path = binaries / "tests.exe"
    if not path.is_file():
        raise PackagingError(f"test runner is missing: {path}")
    return path


def _item_paths(text: str, repository: Path) -> tuple[str, ...]:
    """Return the repository-relative items from an MSBuild ``-getItem`` document."""

    start = text.find("{")
    if start < 0:
        raise PackagingError("MSBuild returned no item inventory")
    try:
        document = json.loads(text[start:])
    except json.JSONDecodeError as error:
        raise PackagingError("MSBuild item inventory is not JSON") from error
    if not isinstance(document, dict) or not isinstance(document.get("Items"), dict):
        raise PackagingError("MSBuild item inventory has no Items")
    paths: set[str] = set()
    for name, values in document["Items"].items():
        if not isinstance(values, list):
            raise PackagingError(f"MSBuild item inventory for {name} is not a list")
        for value in values:
            identity = value.get("Identity") if isinstance(value, dict) else value
            if not isinstance(identity, str):
                raise PackagingError(f"MSBuild item inventory for {name} has a non-string identity")
            candidate = Path(identity)
            resolved = candidate if candidate.is_absolute() else repository / candidate
            try:
                relative = resolved.relative_to(repository)
            except ValueError:
                continue
            if not resolved.is_file():
                raise PackagingError(f"project item is missing: {resolved}")
            paths.add(relative.as_posix())
    if not paths:
        raise PackagingError("MSBuild reported no project items")
    return tuple(sorted(paths))


def _project_inventory(
    repository: Path, module: str, tools: PackagingTools, runner: Runner, scratch: Path
) -> tuple[str, ...]:
    """Ask MSBuild for one project's authoritative source and header inventory."""

    project = repository / "build" / "projects" / f"{module}.vcxproj"
    if not project.is_file():
        raise PackagingError(f"module project is missing: {project}")
    report = scratch / f"inventory-{module}.json"
    argv = (
        str(tools.msbuild), str(project), "-nologo",
        "-getItem:ClCompile", "-getItem:ClInclude",
        "-p:Configuration=Release", "-p:Platform=x64",
        "-p:VcpkgManifestInstall=false", f"-p:VcpkgRoot={tools.vcpkg_root}",
    )
    with report.open("wb") as stream:
        runner(argv, cwd=repository, env=None, stdout=stream)
    return _item_paths(report.read_text(encoding="utf-8-sig"), repository)


def module_content_inputs(
    repository: Path, module: str, inventory: Iterable[str]
) -> tuple[str, ...]:
    """Return the repository files whose bytes decide whether one module changed."""

    inputs = set(inventory)
    inputs.update(shared_content_inputs(repository))
    inputs.update(_SHARED_BUILD_INPUTS)
    inputs.add(f"build/projects/{module}.vcxproj")
    inputs.update({
        f"src/modules/{module}/observer_user.ini",
        "LICENSE.txt",
        *(f"licenses/{name}" for name in LICENSES[module]),
    })
    trees = (
        repository / "src" / "modules" / module,
        *(repository / name for name in _CONSERVATIVE_HEADER_TREES),
    )
    for tree in trees:
        if tree.is_dir():
            inputs.update(
                path.relative_to(repository).as_posix()
                for path in tree.rglob("*")
                if path.is_file()
            )
    inputs.update(
        path.relative_to(repository).as_posix()
        for path in (repository / "src").glob("*.h")
        if path.is_file()
    )
    return tuple(sorted(
        name for name in inputs if name.rsplit("/", 1)[-1] != VERSION_FILE
    ))


def _audit_modules(modules: tuple[str, ...], include_leak_probe: bool, architecture: str) -> tuple[str, ...]:
    """Audit the requested modules plus the leak probe, which only exists for x64."""

    probe = (LEAK_PROBE,) if include_leak_probe and architecture == "x64" else ()
    return (*probe, *modules)


def _audit(
    repository: Path,
    architectures: Iterable[str],
    output: Path,
    *,
    modules: str | Iterable[str] | None,
    include_leak_probe: bool,
    tools: PackagingTools,
    execute: Runner,
    binaries_for: Callable[[str], Path],
) -> tuple[Path, ...]:
    """Gate the stable Release binaries ``binaries_for`` resolves for each architecture."""

    selected_architectures = _architectures(architectures)
    selected_modules = _modules(modules)
    root = _output_root(output)
    evidence: list[Path] = []
    for architecture in selected_architectures:
        binaries = binaries_for(architecture)
        for module in _audit_modules(selected_modules, include_leak_probe, architecture):
            binary = _artifact(binaries, module)
            directory = _directory(root / "audit" / architecture / module, root)
            logs = {}
            for mode in ("headers", "dependents", "exports"):
                log = _leaf(directory / f"{mode}.txt", root)
                with log.open("wb") as stream:
                    execute(
                        (str(tools.dumpbin), f"/{mode}", str(binary)),
                        cwd=repository, env=None, stdout=stream,
                    )
                logs[mode] = log
                evidence.append(log)
            # The leak probe is a self-contained EXE without module exports; every shipped
            # module still exports exactly LoadSubModule/UnloadSubModule.
            kind = ("--executable",) if module == LEAK_PROBE else ()
            execute(
                (*_python("core.binary_audit"), "pe", *kind, architecture,
                 str(logs["headers"]), str(logs["dependents"]), str(logs["exports"])),
                cwd=BUILD_ROOT, env=None,
            )
            report = _leaf(directory / "binskim.sarif", root)
            if report.exists():
                report.unlink()
            execute(
                (*_python("core.binary_audit"), "run-binskim", str(tools.binskim), str(binary)),
                cwd=BUILD_ROOT, env=_environment(directory),
            )
            execute(
                (*_python("core.binary_audit"), "binskim", str(report)),
                cwd=BUILD_ROOT, env=None,
            )
            evidence.append(report)
            if module != LEAK_PROBE:
                execute(
                    (*_python("core.module_version"), "verify", read_version(repository, module),
                     str(binary)),
                    cwd=BUILD_ROOT, env=None,
                )
    return tuple(evidence)


def audit_binaries(
    repository: Path,
    architectures: Iterable[str],
    output: Path,
    *,
    modules: str | Iterable[str] | None = None,
    include_leak_probe: bool = False,
    tools: PackagingTools | None = None,
    runner: Runner | None = None,
) -> tuple[Path, ...]:
    """Gate every selected stable Release binary with dumpbin, BinSkim and its version."""

    repository = Path(repository).resolve(strict=True)
    return _audit(
        repository,
        architectures,
        output,
        modules=modules,
        include_leak_probe=include_leak_probe,
        tools=tools or locate_packaging_tools(),
        execute=runner or _default_runner,
        binaries_for=lambda architecture: _binaries(repository, architecture),
    )


def _validate_zip(archive: Path, files: Mapping[str, Path]) -> None:
    """Fail closed unless one deterministic archive carries exactly the staged bytes."""

    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) != len({member.filename for member in members}):
            raise PackagingError("symbols archive has duplicate entries")
        if sorted(member.filename for member in members) != sorted(files):
            raise PackagingError("symbols archive does not match the staged payload")
        for member in members:
            digest = hashlib.file_digest(bundle.open(member), "sha256").hexdigest()
            if digest != _sha256(files[member.filename]):
                raise PackagingError("symbols archive does not match the staged payload")


def _manifest(
    repository: Path,
    modules: tuple[str, ...],
    inventory: Mapping[str, tuple[str, ...]],
    archives: Iterable[Path],
) -> dict[str, object]:
    records = [
        {
            "content": content_identity(
                repository, module_content_inputs(repository, module, inventory[module])
            ),
            "module": module,
            "version": read_version(repository, module),
        }
        for module in modules
    ]
    return {
        "archives": [
            {"name": archive.name, "sha256": _sha256(archive)}
            for archive in sorted(archives, key=lambda item: item.name)
        ],
        "modules": records,
    }


def package(
    repository: Path,
    architectures: Iterable[str],
    output: Path,
    *,
    modules: str | Iterable[str] | None = None,
    smoke_architectures: Iterable[str] | None = None,
    tools: PackagingTools | None = None,
    runner: Runner | None = None,
    host: str | None = None,
) -> PackageResult:
    """Audit the stable Release binaries, then package and smoke exactly that build.

    Each selected Release binary and its PDB are frozen into one operation staging tree first, so
    the audit, the archive and the smoke all read the same bytes even if the build tree mutates
    underneath.  The selected archives are assembled in a fresh staging tree and only published
    once every gate passes, so a failed rerun can never leave a manifest describing overwritten or
    stale archives.

    A selected-module release writes only that module's archives and manifest records.  The
    manifest stays in the ``core.release`` shape, but its module list is the selected set, so the
    release gate must relax its complete-module-set requirement to the selected modules for an
    independent ``renpy/vX.Y.Z`` style release.
    """

    repository = Path(repository).resolve(strict=True)
    selected_architectures = _architectures(architectures)
    selected_modules = _modules(modules)
    selected_smoke = _smoke(selected_architectures, smoke_architectures)
    root = _output_root(output)
    tools = tools or locate_packaging_tools()
    execute = runner or _default_runner

    staging = _reset_directory(root / "staging", root)
    try:
        # Freeze the audited binaries once, then audit, archive and smoke only the frozen copies.
        inputs = _directory(staging / "inputs", root)
        snapshots: dict[str, Path] = {}
        for architecture in selected_architectures:
            binaries = _binaries(repository, architecture)
            directory = _directory(inputs / architecture, root)
            for module in selected_modules:
                for source in (_artifact(binaries, module), _symbol(binaries, module)):
                    shutil.copyfile(source, _leaf(directory / source.name, root))
            snapshots[architecture] = directory

        _audit(repository, selected_architectures, root, modules=selected_modules,
               include_leak_probe=False, tools=tools, execute=execute,
               binaries_for=lambda architecture: snapshots[architecture])

        pending = _reset_directory(staging / "publish", root)
        archives: list[Path] = []
        for architecture in selected_architectures:
            target = _directory(pending / architecture, root)
            snapshot = snapshots[architecture]
            for module in selected_modules:
                version = read_version(repository, module)
                binary = snapshot / f"{module}.so"
                stage = _reset_directory(staging / "stage" / architecture / module / "stage", root)
                execute(
                    (*_python("core.package"), "stage-module", architecture, module, version,
                     str(binary), str(repository)),
                    cwd=BUILD_ROOT, env=_environment(stage),
                )
                archive = target / module_archive_name(module, version, architecture)
                _leaf(archive, root)
                execute(
                    (*_python("core.package"), "archive-module", architecture, module, version,
                     str(stage)),
                    cwd=BUILD_ROOT, env=_environment(target),
                )
                validation = _reset_directory(
                    staging / "stage" / architecture / module / "validation", root
                )
                execute(
                    (*_python("core.package"), "validate-module", architecture, module, version,
                     str(archive), str(stage)),
                    cwd=BUILD_ROOT, env=_environment(validation),
                )
                archives.append(archive)

        # One deterministic PDB bundle per selected module carries every selected architecture as
        # ``<arch>/<module>.pdb``; the audited PDB snapshot frozen above is the only source, so a
        # single-architecture intermediate and the all-architecture local bundle share one name.
        for module in selected_modules:
            version = read_version(repository, module)
            files = {
                f"{architecture}/{module}.pdb": snapshots[architecture] / f"{module}.pdb"
                for architecture in selected_architectures
            }
            symbol_archive = pending / f"{module}-{version}-pdb.zip"
            _leaf(symbol_archive, root)
            _zip(symbol_archive, files)
            _validate_zip(symbol_archive, files)
            archives.append(symbol_archive)

        deferred: list[DeferredGate] = []
        runnable = runnable_architectures(selected_smoke, host)
        for architecture in selected_smoke:
            if architecture not in runnable:
                deferred.append(DeferredGate(
                    "package-runtime", architecture,
                    f"host cannot execute {architecture} package-runtime",
                ))
                continue
            tests = _test_runner(_binaries(repository, architecture))
            for module in selected_modules:
                version = read_version(repository, module)
                archive = pending / architecture / module_archive_name(module, version, architecture)
                directory = _reset_directory(staging / "smoke" / architecture / module, root)
                _leaf(directory / f"{module}.so", root)
                execute(
                    (*_python("core.package"), "smoke", architecture, module, str(archive),
                     str(tests)),
                    cwd=BUILD_ROOT, env=_environment(directory),
                )

        inventory = {
            module: _project_inventory(repository, module, tools, execute, staging)
            for module in selected_modules
        }
        document = _manifest(repository, selected_modules, inventory, archives)
        _leaf(pending / "packages.json", root)
        _write_json(pending / "packages.json", document)

        publish = _publish(pending, root / "packages", root)
        published = tuple(sorted(
            (publish / archive.relative_to(pending) for archive in archives),
            key=lambda item: item.name,
        ))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return PackageResult(published, tuple(deferred))

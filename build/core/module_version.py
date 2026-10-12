"""Per-module release versions, content identity, and binary version evidence."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
import hashlib
from pathlib import Path
import re

import pywintypes
import win32api


MODULES = ("renpy", "rpgmaker", "zanzarah")
VERSION_FILE = "VERSION"
# The props file injects the version macros into every module translation unit, so it belongs to
# the inputs every project signs; the resource script is compiled only into the shipped DLLs.
VERSION_PROPS = "build/ObserverModuleVersion.props"
VERSION_RESOURCE = "src/modules/version.rc"
_FUZZ_PREFIX = "fuzz-"
# Dependency bytes are chosen by the manifest and the repository-owned overlay tree; a bump there
# changes what is statically linked into every shipped module.
_DEPENDENCY_MANIFEST = "vcpkg.json"
_OPTIONAL_DEPENDENCY_INPUTS = ("vcpkg-configuration.json",)
_DEPENDENCY_TREE = "build/vcpkg"
_COMPONENT = r"(?:0|[1-9][0-9]{0,4})"
_VERSION = re.compile(rf"{_COMPONENT}\.{_COMPONENT}\.{_COMPONENT}")
_MAXIMUM = 0xFFFF


class ModuleVersionError(RuntimeError):
    """A module version is missing, malformed, or absent from the built binary."""


def version_path(module: str) -> str:
    """Return the repository-relative single source of truth for one module."""

    if module not in MODULES:
        raise ModuleVersionError(f"unknown module: {module}")
    return f"src/modules/{module}/{VERSION_FILE}"


def project_module(project: str) -> str | None:
    """Return the module whose VERSION decides one project's compiled version macros."""

    if project in MODULES:
        return project
    module = project.removeprefix(_FUZZ_PREFIX)
    return module if module != project and module in MODULES else None


def project_version_inputs(project: str) -> tuple[str, ...]:
    """Return the version inputs one project must sign beyond its declared inputs."""

    module = project_module(project)
    if module is None:
        return ()
    resource = (VERSION_RESOURCE,) if project in MODULES else ()
    return (*resource, version_path(module))


def shared_content_inputs(repository: Path) -> tuple[str, ...]:
    """Return the repository-wide files that decide every module's shipped bytes."""

    names = [VERSION_PROPS, VERSION_RESOURCE, _DEPENDENCY_MANIFEST]
    names.extend(
        name for name in _OPTIONAL_DEPENDENCY_INPUTS if (repository / name).is_file()
    )
    names.extend(
        path.relative_to(repository).as_posix()
        for path in (repository / _DEPENDENCY_TREE).rglob("*")
        if path.is_file()
    )
    return tuple(sorted(set(names)))


def parse_version(version: str) -> tuple[int, int, int]:
    """Validate one ``X.Y.Z`` release version and return its numeric components."""

    if _VERSION.fullmatch(version) is None:
        raise ModuleVersionError(f"module version must be X.Y.Z: {version!r}")
    major, minor, patch = (int(part) for part in version.split("."))
    if max(major, minor, patch) > _MAXIMUM:
        raise ModuleVersionError(f"module version component exceeds {_MAXIMUM}: {version}")
    return major, minor, patch


def read_version(repository: Path, module: str) -> str:
    """Read and validate one module's declared version from its VERSION file."""

    path = repository / version_path(module)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ModuleVersionError(f"module version file is unreadable: {path}") from error
    version = content.strip()
    parse_version(version)
    return version


def content_identity(repository: Path, paths: Iterable[str]) -> str:
    """Hash the exact first-party inputs that decide whether a module changed."""

    names = sorted(set(paths))
    if not names:
        raise ModuleVersionError("module content identity requires at least one input")
    digest = hashlib.sha256()
    for name in names:
        if name.rsplit("/", 1)[-1] == VERSION_FILE:
            raise ModuleVersionError(f"module content identity must exclude {name}")
        with (repository / name).open("rb") as stream:
            entry = hashlib.file_digest(stream, "sha256").hexdigest()
        digest.update(f"{name}\0{entry}\n".encode("utf-8"))
    return digest.hexdigest()


def _quad(high: int, low: int) -> tuple[int, int, int, int]:
    return (high >> 16 & 0xFFFF, high & 0xFFFF, low >> 16 & 0xFFFF, low & 0xFFFF)


def binary_version(binary: Path) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return the FileVersion and ProductVersion of a PE VERSIONINFO resource."""

    try:
        information = win32api.GetFileVersionInfo(str(binary), "\\")
    except pywintypes.error as error:
        raise ModuleVersionError(f"binary has no version resource: {binary}") from error
    return (
        _quad(information["FileVersionMS"], information["FileVersionLS"]),
        _quad(information["ProductVersionMS"], information["ProductVersionLS"]),
    )


def require_binary_version(binary: Path, version: str) -> None:
    """Fail closed unless a binary carries exactly its declared release version."""

    expected = (*parse_version(version), 0)
    file_version, product_version = binary_version(binary)
    if (file_version, product_version) != (expected, expected):
        raise ModuleVersionError(
            f"binary version resource is {file_version}/{product_version}, "
            f"expected {expected}: {binary}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("version")
    verify.add_argument("binary", type=Path)
    args = parser.parse_args(argv)
    require_binary_version(args.binary, args.version)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

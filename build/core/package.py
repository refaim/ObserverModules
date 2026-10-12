"""Deterministic staging and ZIP creation for release packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from collections.abc import Sequence
import zipfile

from core.module_version import ModuleVersionError, parse_version
from core.changelog import CHANGELOG_FILES, ChangelogError, latest_notes


ARCHITECTURES = ("x86", "x64", "arm64")
LICENSES = {
    "renpy": ("Observer.txt", "rpatool.txt", "serde-pickle.txt", "zlib.txt"),
    "rpgmaker": ("Observer.txt", "rgssad.txt"),
    "zanzarah": ("Observer.txt", "zanzapak.txt"),
}
MODULES = tuple(LICENSES)
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_IDENTITY = re.compile(r"[0-9a-f]{64}")
# A release module ships one deterministic LICENSES.txt whose sections are titled by a stable
# rule so the same source bytes always concatenate to the same archive entry.
_LICENSE_SEPARATOR = "=" * 72
_LICENSE_TITLE = "Observer Modules"


class PackageError(RuntimeError):
    pass


def module_archive_name(module: str, version: str, architecture: str) -> str:
    """Return the published archive name that carries the module's release version."""

    return f"{module}-{version}-{architecture}.zip"


def _require_version(version: str) -> str:
    try:
        parse_version(version)
    except ModuleVersionError as error:
        raise PackageError(f"invalid module version: {version}") from error
    return version


def _output() -> Path:
    value = os.environ.get("OBSERVER_OUT_DIR")
    if not value or not (output := Path(value)).is_dir():
        raise PackageError("OBSERVER_OUT_DIR must be an existing directory")
    return output


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def licenses_document(repository: Path, module: str) -> bytes:
    """Return the deterministic ``LICENSES.txt`` shipped beside one module.

    The project license and only that module's dependency notices are concatenated in titled
    sections, each keeping the complete original bytes; a missing or empty source fails closed.
    """

    sources = [(_LICENSE_TITLE, repository / "LICENSE.txt")]
    sources.extend((name, repository / "licenses" / name) for name in LICENSES[module])
    sections: list[bytes] = []
    for title, path in sources:
        try:
            text = path.read_bytes()
        except OSError as error:
            raise PackageError(f"license notice is missing or unreadable: {path}") from error
        if not text.strip():
            raise PackageError(f"license notice is empty: {path}")
        heading = f"{_LICENSE_SEPARATOR}\n{title}\n{_LICENSE_SEPARATOR}\n\n".encode("utf-8")
        body = text if text.endswith(b"\n") else text + b"\n"
        sections.append(heading + body)
    return b"\n".join(sections)


def _payload_files(payload: Path) -> dict[str, Path]:
    return {
        path.relative_to(payload).as_posix(): path
        for path in sorted(payload.rglob("*"))
        if path.is_file()
    }


def _entries(files: dict[str, Path]) -> list[dict[str, str]]:
    return [{"name": name, "sha256": _sha256(path)} for name, path in sorted(files.items())]


def _document(
    kind: str, architecture: str, module: str, payload: Path, version: str = ""
) -> dict[str, object]:
    document: dict[str, object] = {
        "architecture": architecture,
        "entries": _entries(_payload_files(payload)),
        "kind": kind,
        "module": module,
    }
    if version:
        document["version"] = version
    return document


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _stage(
    args: argparse.Namespace,
    kind: str,
    files: dict[str, Path],
    version: str = "",
    generated: dict[str, bytes] | None = None,
) -> None:
    output = _output()
    payload = output / "payload"
    for name, source in files.items():
        destination = payload / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    for name, content in (generated or {}).items():
        destination = payload / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    _write_json(
        output / "manifest.json",
        _document(kind, args.architecture, args.module, payload, version),
    )


def _stage_module(args: argparse.Namespace) -> None:
    repository = args.repository
    version = _require_version(args.version)
    for language in CHANGELOG_FILES:
        try:
            latest_notes(repository, args.module, version, language=language)
        except ChangelogError as error:
            raise PackageError(
                f"{args.module} ChangeLog does not describe {version}: {error}"
            ) from error
    module_directory = repository / f"src/modules/{args.module}"
    files = {
        f"{args.module}.so": args.binary,
        "observer_user.ini": module_directory / "observer_user.ini",
    } | {
        name: module_directory / name for name in CHANGELOG_FILES.values()
    }
    _stage(args, "module", files, version, generated={"LICENSES.txt": licenses_document(repository, args.module)})


def _stage_symbol(args: argparse.Namespace) -> None:
    _stage(args, "symbols", {f"{args.module}.pdb": args.symbol})


def _stage_payload(
    stage: Path, kind: str, architecture: str, module: str, version: str = ""
) -> dict[str, Path]:
    payload = stage / "payload"
    expected = _document(kind, architecture, module, payload, version)
    actual = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    if actual != expected:
        raise PackageError(f"{kind} stage manifest does not match its payload")
    return _payload_files(payload)


def _zip(destination: Path, files: dict[str, Path]) -> None:
    with zipfile.ZipFile(destination, "w") as archive:
        for name, path in sorted(files.items()):
            information = zipfile.ZipInfo(name, _ZIP_TIME)
            information.compress_type = zipfile.ZIP_DEFLATED
            information.create_system = 3
            information.external_attr = 0o100644 << 16
            with path.open("rb") as source, archive.open(information, "w") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)


def _archive_module(args: argparse.Namespace) -> None:
    version = _require_version(args.version)
    files = _stage_payload(args.stage, "module", args.architecture, args.module, version)
    _zip(_output() / module_archive_name(args.module, version, args.architecture), files)


def _archive_entries(path: Path) -> list[dict[str, str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) != len({member.filename for member in members}):
                raise PackageError("package archive does not match exact stage manifests")
            entries = []
            for member in sorted(members, key=lambda item: item.filename):
                with archive.open(member) as stream:
                    entries.append({"name": member.filename, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()})
            return entries
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise PackageError("package archive does not match exact stage manifests") from error


def _validate(archive: Path, files: dict[str, Path]) -> None:
    entries = _archive_entries(archive)
    if entries != _entries(files):
        raise PackageError("package archive does not match exact stage manifests")
    _write_json(_output() / "validation.json", {"entries": entries, "name": archive.name, "sha256": _sha256(archive)})


def _validate_module(args: argparse.Namespace) -> None:
    version = _require_version(args.version)
    _validate(
        args.archive,
        _stage_payload(args.stage, "module", args.architecture, args.module, version),
    )


def _module_records(values: Sequence[str]) -> list[dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    for value in values:
        module, _, remainder = value.partition("=")
        version, _, identity = remainder.partition("=")
        if module not in MODULES or _IDENTITY.fullmatch(identity) is None:
            raise PackageError(f"invalid module record: {value}")
        _require_version(version)
        if module in records:
            raise PackageError(f"duplicate module record: {module}")
        records[module] = {"content": identity, "module": module, "version": version}
    if set(records) != set(MODULES):
        raise PackageError("package manifest requires the expected module set")
    return [records[module] for module in sorted(records)]


def _aggregate(args: argparse.Namespace) -> None:
    names = [archive.name for archive in args.archives]
    if len(names) != len(set(names)):
        raise PackageError("duplicate archive name in package manifest")
    modules = _module_records(args.modules)
    output = _output()
    archives = sorted(args.archives, key=lambda path: path.name)
    for archive in archives:
        shutil.copyfile(archive, output / archive.name)
    _write_json(
        output / "packages.json",
        {
            "archives": [
                {"name": archive.name, "sha256": _sha256(archive)} for archive in archives
            ],
            "modules": modules,
        },
    )


def _smoke(args: argparse.Namespace) -> None:
    output = _output()
    try:
        with zipfile.ZipFile(args.archive) as archive:
            module = Path(archive.extract(f"{args.module}.so", output))
    except KeyError as error:
        raise PackageError(f"package archive has no {args.module}.so") from error
    subprocess.run(
        [str(args.tests), "[package-smoke]", "--reporter", "compact", "--rng-seed", "1"],
        check=True, cwd=output,
        env=os.environ | {"OBSERVER_PACKAGE_MODULE": str(module), "OBSERVER_PACKAGE_FORMAT": args.module},
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(required=True)
    actions = (
        ("stage-module", ("architecture", "module", "version", "binary", "repository"), _stage_module),
        ("stage-symbol", ("architecture", "module", "symbol"), _stage_symbol),
        ("archive-module", ("architecture", "module", "version", "stage"), _archive_module),
        ("validate-module", ("architecture", "module", "version", "archive", "stage"), _validate_module),
        ("aggregate", ("modules", "archives"), _aggregate),
        ("smoke", ("architecture", "module", "archive", "tests"), _smoke),
    )
    for command, names, action in actions:
        current = commands.add_parser(command)
        for name in names:
            if name == "modules":
                current.add_argument("--module", dest="modules", action="append", required=True)
                continue
            choices = ARCHITECTURES if name == "architecture" else MODULES if name == "module" else None
            current.add_argument(
                name,
                choices=choices,
                nargs="+" if name in {"stages", "archives"} else None,
                type=None if choices or name == "version" else Path,
            )
        current.set_defaults(run=action)
    args = parser.parse_args(argv)
    args.run(args)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

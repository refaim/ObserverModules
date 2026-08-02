"""Deterministic staging and ZIP creation for release packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from collections.abc import Sequence
import zipfile


ARCHITECTURES = ("x86", "x64", "arm64")
LICENSES = {
    "renpy": ("Observer.txt", "rpatool.txt", "serde-pickle.txt", "zlib.txt"),
    "rpgmaker": ("Observer.txt", "rgssad.txt"),
    "zanzarah": ("Observer.txt", "zanzapak.txt"),
}
MODULES = tuple(LICENSES)
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class PackageError(RuntimeError):
    pass


def _output() -> Path:
    value = os.environ.get("OBSERVER_OUT_DIR")
    if not value or not (output := Path(value)).is_dir():
        raise PackageError("OBSERVER_OUT_DIR must be an existing directory")
    return output


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _payload_files(payload: Path) -> dict[str, Path]:
    return {
        path.relative_to(payload).as_posix(): path
        for path in sorted(payload.rglob("*"))
        if path.is_file()
    }


def _entries(files: dict[str, Path]) -> list[dict[str, str]]:
    return [{"name": name, "sha256": _sha256(path)} for name, path in sorted(files.items())]


def _document(kind: str, architecture: str, module: str, payload: Path) -> dict[str, object]:
    return {
        "architecture": architecture,
        "entries": _entries(_payload_files(payload)),
        "kind": kind,
        "module": module,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _stage(args: argparse.Namespace, kind: str, files: dict[str, Path]) -> None:
    output = _output()
    payload = output / "payload"
    for name, source in files.items():
        destination = payload / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    _write_json(output / "manifest.json", _document(kind, args.architecture, args.module, payload))


def _stage_module(args: argparse.Namespace) -> None:
    repository = args.repository
    files = {
        f"{args.module}.so": args.binary,
        "observer_user.ini": repository / f"src/modules/{args.module}/observer_user.ini",
        "docs/license.txt": repository / "LICENSE.txt",
    } | {f"docs/thirdparty/{name}": repository / "licenses" / name for name in LICENSES[args.module]}
    _stage(args, "module", files)


def _stage_symbol(args: argparse.Namespace) -> None:
    _stage(args, "symbols", {f"{args.module}.pdb": args.symbol})


def _stage_payload(stage: Path, kind: str, architecture: str, module: str) -> dict[str, Path]:
    payload = stage / "payload"
    expected = _document(kind, architecture, module, payload)
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
    files = _stage_payload(args.stage, "module", args.architecture, args.module)
    _zip(_output() / f"{args.module}-{args.architecture}-dll.zip", files)


def _archive_symbols(args: argparse.Namespace) -> None:
    _zip(_output() / f"observer-modules-{args.architecture}-pdb.zip", _symbol_payload(args))


def _symbol_payload(args: argparse.Namespace) -> dict[str, Path]:
    documents = [json.loads((stage / "manifest.json").read_text(encoding="utf-8")) for stage in args.stages]
    modules = [document.get("module") for document in documents]
    if set(modules) != set(MODULES) or len(modules) != len(MODULES):
        raise PackageError("symbols archive requires the expected module set")
    files = {}
    for module, stage in sorted(zip(modules, args.stages, strict=True)):
        files.update(_stage_payload(stage, "symbols", args.architecture, str(module)))
    return files


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
    _validate(args.archive, _stage_payload(args.stage, "module", args.architecture, args.module))


def _validate_symbols(args: argparse.Namespace) -> None:
    _validate(args.archive, _symbol_payload(args))


def _aggregate(args: argparse.Namespace) -> None:
    names = [archive.name for archive in args.archives]
    if len(names) != len(set(names)):
        raise PackageError("duplicate archive name in package manifest")
    output = _output()
    for archive in sorted(args.archives, key=lambda path: path.name):
        shutil.copyfile(archive, output / archive.name)
    _write_json(
        output / "packages.json",
        [
            {"name": archive.name, "sha256": _sha256(archive)}
            for archive in sorted(args.archives, key=lambda path: path.name)
        ],
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
        ("stage-module", ("architecture", "module", "binary", "repository"), _stage_module),
        ("stage-symbol", ("architecture", "module", "symbol"), _stage_symbol),
        ("archive-module", ("architecture", "module", "stage"), _archive_module),
        ("archive-symbols", ("architecture", "stages"), _archive_symbols),
        ("validate-module", ("architecture", "module", "archive", "stage"), _validate_module),
        ("validate-symbols", ("architecture", "archive", "stages"), _validate_symbols),
        ("aggregate", ("archives",), _aggregate),
        ("smoke", ("architecture", "module", "archive", "tests"), _smoke),
    )
    for command, names, action in actions:
        current = commands.add_parser(command)
        for name in names:
            choices = ARCHITECTURES if name == "architecture" else MODULES if name == "module" else None
            current.add_argument(
                name,
                choices=choices,
                nargs="+" if name in {"stages", "archives"} else None,
                type=None if choices else Path,
            )
        current.set_defaults(run=action)
    args = parser.parse_args(argv)
    args.run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

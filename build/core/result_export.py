"""Publish one command's current evidence into a plain, self-describing export tree.

This is the console-first successor to the content-addressed result export.  It
copies the evidence the operations actually produced right now -- never a CAS
directory and never a stale previous success -- into an application-owned
``destination``:

``manifest.json``
    The passed/failed/deferred operations plus the relative evidence paths.
``reports/``
    The declared report files, nested below one directory per operation and the
    operation's common evidence directory, so two reports that share a basename
    (``Debug/tests.xml`` and ``Release/tests.xml``) keep distinct, meaningful paths.
``logs/``
    One log per operation that produced command output, success or failure.
``packages/``
    The flat, version-and-architecture named release ZIPs and ``packages.json``.

The destination must be a fresh, non-reparse path below an existing directory:
a second run never overwrites a previous export.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import stat
import tempfile


_COMMAND = re.compile(r"[a-z0-9][a-z0-9._-]*")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
PASSED = "passed"
FAILED = "failed"
DEFERRED = "deferred"
_STATUSES = (PASSED, FAILED, DEFERRED)
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class ResultExportError(RuntimeError):
    """Declared evidence could not be published safely and completely."""


@dataclass(frozen=True, slots=True)
class Operation:
    """One named operation, its verdict, and the evidence it produced."""

    name: str
    status: str
    detail: str = ""
    reports: tuple[Path, ...] = ()
    log: str = ""


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _is_reparse(path: Path) -> bool:
    information = os.lstat(path)
    attributes = getattr(information, "st_file_attributes", 0)
    return stat.S_ISLNK(information.st_mode) or bool(attributes & _REPARSE)


def _checked_file(path: Path) -> Path:
    """Return ``path`` only when it is a real regular file, never behind a link."""

    information = _lstat(path)
    if information is None:
        raise ResultExportError(f"claimed evidence is missing: {path}")
    for candidate in (path, *path.parents):
        if _is_reparse(candidate):
            raise ResultExportError(f"reparse points are forbidden in evidence: {candidate}")
    if not stat.S_ISREG(information.st_mode):
        raise ResultExportError(f"claimed evidence is not a regular file: {path}")
    return path


def _report_entries(operation: Operation) -> tuple[tuple[Path, str], ...]:
    """Name each report by its path below the operation's common evidence directory.

    A single report keeps its basename; several reports that share a directory keep
    only that directory as the ancestor so their subpaths stay unique and meaningful
    (``Debug/tests.xml`` versus ``Release/tests.xml``).
    """

    reports = tuple(Path(report) for report in operation.reports)
    if not reports:
        return ()
    common = Path(os.path.commonpath([os.fspath(report.parent) for report in reports]))
    return tuple(
        (report, f"{operation.name}/{report.relative_to(common).as_posix()}")
        for report in reports
    )


def _digest(path: Path) -> tuple[int, str]:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return path.stat().st_size, digest


def _copy(
    staging: Path, directory: str, sources: Iterable[tuple[Path, str]]
) -> list[dict[str, object]]:
    """Copy declared files flat below ``staging/<directory>`` and describe them."""

    (staging / directory).mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for source, name in sources:
        if name in seen:
            raise ResultExportError(f"duplicate evidence name: {name}")
        seen.add(name)
        _checked_file(source)
        destination = staging / directory / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination, follow_symlinks=False)
        size, digest = _digest(destination)
        records.append({"path": f"{directory}/{name}", "sha256": digest, "size": size})
    return records


def _validate_command(command: str) -> None:
    if not isinstance(command, str) or _COMMAND.fullmatch(command) is None:
        raise ResultExportError("command must be a lowercase logical name")


def _validate_operations(operations: Sequence[Operation]) -> None:
    seen: set[str] = set()
    for operation in operations:
        if _NAME.fullmatch(operation.name) is None:
            raise ResultExportError(f"invalid operation name: {operation.name!r}")
        if operation.name in seen:
            raise ResultExportError(f"duplicate operation: {operation.name}")
        if operation.status not in _STATUSES:
            raise ResultExportError(f"invalid operation status: {operation.status!r}")
        seen.add(operation.name)


def _validate_destination(destination: Path) -> None:
    if _lstat(destination) is not None:
        raise ResultExportError(f"export destination already exists: {destination}")
    information = _lstat(destination.parent)
    if information is None or not stat.S_ISDIR(information.st_mode):
        raise ResultExportError("export destination parent must be an existing directory")
    current = destination.parent
    while True:
        if _is_reparse(current):
            raise ResultExportError(f"reparse point in export destination: {current}")
        if current.parent == current:
            return
        current = current.parent


def _write_manifest(staging: Path, document: dict[str, object]) -> None:
    with (staging / "manifest.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def _publish_logs(staging: Path, operations: Sequence[Operation]) -> list[dict[str, object]]:
    (staging / "logs").mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    for operation in operations:
        if not operation.log:
            continue
        relative = f"logs/{operation.name}.log"
        path = staging / relative
        path.write_text(operation.log, encoding="utf-8", newline="\n")
        size, digest = _digest(path)
        records.append({"path": relative, "sha256": digest, "size": size})
    return records


def export_results(
    destination: Path | str,
    command: str,
    operations: Iterable[Operation],
    *,
    packages: Iterable[Path] = (),
) -> Path:
    """Publish ``operations`` and ``packages`` into a fresh ``destination`` directory."""

    _validate_command(command)
    ordered = tuple(operations)
    _validate_operations(ordered)
    published = Path(os.path.abspath(os.fspath(destination)))
    _validate_destination(published)
    try:
        with tempfile.TemporaryDirectory(
            prefix=f".{published.name}.tmp-", dir=published.parent
        ) as temporary:
            staging = Path(temporary)
            reports = _copy(
                staging,
                "reports",
                (entry for operation in ordered for entry in _report_entries(operation)),
            )
            logs = _publish_logs(staging, ordered)
            package_records = _copy(
                staging, "packages", ((Path(path), Path(path).name) for path in packages)
            )
            _write_manifest(
                staging,
                {
                    "schema": 1,
                    "command": command,
                    "status": "failed" if any(
                        operation.status == FAILED for operation in ordered
                    ) else "success",
                    "passed": [
                        operation.name for operation in ordered if operation.status == PASSED
                    ],
                    "failed": [
                        operation.name for operation in ordered if operation.status == FAILED
                    ],
                    "deferred": [
                        {"name": operation.name, "detail": operation.detail}
                        for operation in ordered
                        if operation.status == DEFERRED
                    ],
                    "operations": [
                        {
                            "name": operation.name,
                            "status": operation.status,
                            "detail": operation.detail,
                        }
                        for operation in ordered
                    ],
                    "reports": reports,
                    "logs": logs,
                    "packages": package_records,
                },
            )
            staging.rename(published)
    except OSError as error:
        raise ResultExportError(f"could not publish evidence export: {error}") from error
    return published

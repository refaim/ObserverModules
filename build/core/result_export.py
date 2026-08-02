"""Atomically publish declared graph results without exposing CAS paths."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any, Iterable, Mapping

from core.graph import Graph, Node, Result
from core.store import CasStore


_COMMAND = re.compile(r"[a-z0-9][a-z0-9._-]*")
_STATUSES = {"success", "failed"}
_PACKAGE_PREFIX = "packages/"
_RESERVED = ("manifest.json", "logs", "packages")
_PACKAGE_RESERVED = ("manifest.json",)


class ResultExportError(RuntimeError):
    """Declared results could not be published safely and completely."""


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _is_reparse(path: Path) -> bool:
    information = os.lstat(path)
    attributes = getattr(information, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(information.st_mode) or bool(attributes & reparse_attribute)


def _checked(path: Path) -> os.stat_result:
    information = _lstat(path)
    if information is None:
        raise ResultExportError(f"declared result is missing: {path.name}")
    if _is_reparse(path):
        raise ResultExportError(f"reparse points are forbidden in exported results: {path.name}")
    return information


def _source(output: Path, result: Result) -> tuple[Path, os.stat_result]:
    parts = result.relative_path.split("/")
    current = output / parts[0]
    information = _checked(current)
    for part in parts[1:]:
        if not stat.S_ISDIR(information.st_mode):
            raise ResultExportError(f"declared result path is not a directory: {current.name}")
        current /= part
        information = _checked(current)
    return current, information


def _digest_file(path: Path) -> tuple[int, str]:
    size = path.stat().st_size
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return size, digest


def _copy_file(source: Path, destination: Path) -> tuple[int, str]:
    information = _checked(source)
    if not stat.S_ISREG(information.st_mode):
        raise ResultExportError(f"result is not a regular file: {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination, follow_symlinks=False)
    return _digest_file(destination)


def _tree_record(
    digest: Any, marker: bytes, relative: Path, size: int, content_digest: str = ""
) -> None:
    encoded = relative.as_posix().encode("utf-8")
    digest.update(marker)
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    digest.update(size.to_bytes(8, "big"))
    digest.update(bytes.fromhex(content_digest))


def _copy_directory(source: Path, destination: Path) -> tuple[int, str]:
    destination.mkdir(parents=True)
    total = 0
    digest = hashlib.sha256()
    pending = [(source, Path())]
    while pending:
        current, relative_root = pending.pop()
        directories: list[tuple[Path, Path]] = []
        with os.scandir(current) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                source_entry = Path(entry.path)
                relative = relative_root / entry.name
                information = _checked(source_entry)
                if stat.S_ISDIR(information.st_mode):
                    (destination / relative).mkdir()
                    _tree_record(digest, b"D", relative, 0)
                    directories.append((source_entry, relative))
                elif stat.S_ISREG(information.st_mode):
                    size, file_digest = _copy_file(source_entry, destination / relative)
                    total += size
                    _tree_record(digest, b"F", relative, size, file_digest)
                else:
                    raise ResultExportError(f"unsupported result entry: {entry.name}")
        pending.extend(reversed(directories))
    return total, digest.hexdigest()


def _copy_result(source: Path, information: os.stat_result, destination: Path) -> tuple[str, int, str]:
    if stat.S_ISREG(information.st_mode):
        size, digest = _copy_file(source, destination)
        return "file", size, digest
    if stat.S_ISDIR(information.st_mode):
        size, digest = _copy_directory(source, destination)
        return "directory", size, digest
    raise ResultExportError(f"result must be a file or directory: {source.name}")


def _overlap(first: str, second: str) -> bool:
    return first == second or first.startswith(second + "/") or second.startswith(first + "/")


def _validate_result_paths(graph: Graph) -> None:
    occupied = list(_RESERVED)
    package_occupied = list(_PACKAGE_RESERVED)
    for current in graph.nodes:
        for result in current.results:
            if result.id.startswith(_PACKAGE_PREFIX):
                relative = result.id.removeprefix(_PACKAGE_PREFIX)
                paths = package_occupied
            else:
                relative = result.id
                paths = occupied
            if any(_overlap(relative, path) for path in paths):
                raise ResultExportError(f"colliding export path: {result.id}")
            paths.append(relative)


def _validate_manifest_inputs(
    graph: Graph, command: str, status: str, failures: Iterable[str]
) -> tuple[str, ...]:
    if not isinstance(command, str) or _COMMAND.fullmatch(command) is None:
        raise ResultExportError("command must be a lowercase logical name")
    if status not in _STATUSES:
        raise ResultExportError(f"invalid export status: {status!r}")
    failure_names = tuple(failures)
    known = {current.name for current in graph.nodes}
    if len(failure_names) != len(set(failure_names)) or any(
        name not in known for name in failure_names
    ):
        raise ResultExportError("failures must contain unique graph node names")
    if status != "failed" and failure_names:
        raise ResultExportError("successful export cannot contain failures")
    return failure_names


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


def _result_entry(
    node: Node, result: Result, staging: Path, store: CasStore, relative: str
) -> dict[str, object]:
    source, information = _source(store.paths_for(node).output, result)
    object_type, size, digest = _copy_result(source, information, staging / relative)
    return {
        "id": result.id,
        "kind": result.kind,
        "media_type": result.media_type,
        "object_type": object_type,
        "path": relative,
        "producer_uid": node.uid,
        "sha256": digest,
        "size": size,
    }


def export_results(
    graph: Graph,
    store: CasStore,
    destination: Path | str,
    command: str,
    status: str,
    *,
    failures: Iterable[str] = (),
    cache: Mapping[str, object] | None = None,
) -> Path:
    """Publish complete declared results and a relative-path-only manifest."""

    failure_names = _validate_manifest_inputs(graph, command, status, failures)
    _validate_result_paths(graph)
    published = Path(os.path.abspath(os.fspath(destination)))
    _validate_destination(published)
    try:
        with tempfile.TemporaryDirectory(
            prefix=f".{published.name}.tmp-", dir=published.parent
        ) as temporary:
            staging = Path(temporary)
            complete_results = [
                (current, result)
                for current in graph.nodes
                if store.is_complete(current)
                for result in current.results
            ]
            results = [
                _result_entry(current, result, staging, store, result.id)
                for current, result in complete_results
                if not result.id.startswith(_PACKAGE_PREFIX)
            ]
            package_results: list[dict[str, object]] = []
            if status == "success":
                package_root = staging / "packages"
                package_results = [
                    _result_entry(
                        current,
                        result,
                        package_root,
                        store,
                        result.id.removeprefix(_PACKAGE_PREFIX),
                    )
                    for current, result in complete_results
                    if result.id.startswith(_PACKAGE_PREFIX)
                ]
                if package_results:
                    _write_manifest(
                        package_root,
                        {
                            "schema": 1,
                            "command": command,
                            "status": "success",
                            "failures": [],
                            "results": package_results,
                            "logs": [],
                        },
                    )
            logs: list[dict[str, object]] = []
            if status == "failed":
                for current in graph.nodes:
                    source = store.paths_for(current).log
                    if _lstat(source) is None:
                        continue
                    relative = f"logs/{current.name}.log"
                    size, digest = _copy_file(source, staging / relative)
                    logs.append(
                        {"node": current.name, "path": relative, "sha256": digest, "size": size}
                    )
            document: dict[str, object] = {
                "schema": 1,
                "command": command,
                "status": status,
                "failures": list(failure_names),
                "results": results,
                "logs": logs,
            }
            if cache is not None:
                document["cache"] = dict(cache)
            _write_manifest(staging, document)
            if _lstat(published) is not None:
                raise ResultExportError(f"export destination already exists: {published}")
            staging.rename(published)
    except OSError as error:
        raise ResultExportError(f"could not publish result export: {error}") from error
    return published

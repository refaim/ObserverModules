"""Repository-local CAS using pg83/ix's zero-byte publication marker."""

from __future__ import annotations

import os
from pathlib import Path
import stat

from core.graph import Node
from core.paths import BuildPaths, CasPaths


class CasStateError(RuntimeError):
    """A CAS entry could not be prepared or safely published."""


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _lstat_as(path: Path, file_type: int) -> os.stat_result | None:
    information = _lstat(path)
    if information is None or stat.S_IFMT(information.st_mode) != file_type:
        return None
    return information


def _complete(paths: CasPaths) -> bool:
    marker = _lstat_as(paths.touch, stat.S_IFREG)
    return (
        _lstat_as(paths.entry, stat.S_IFDIR) is not None
        and _lstat_as(paths.output, stat.S_IFDIR) is not None
        and _lstat_as(paths.log, stat.S_IFREG) is not None
        and marker is not None
        and marker.st_size == 0
    )


class CasStore:
    """Own completion state for one build run; callers hold the UID lock."""

    def __init__(self, paths: BuildPaths, run_identifier: str) -> None:
        self._paths = paths
        self._run_work = paths.run_work(run_identifier)

    def paths_for(self, current: Node) -> CasPaths:
        return self._paths.cas(current.uid, current.name)

    def is_complete(self, current: Node) -> bool:
        return _complete(self.paths_for(current))

    def prepare_entry(self, current: Node) -> CasPaths:
        paths = self.paths_for(current)
        if _complete(paths):
            return paths

        self._prepare_run_directory()
        if _lstat(paths.entry) is not None:
            self._quarantine(paths)

        paths.entry.mkdir(exist_ok=False)
        paths.output.mkdir(exist_ok=False)
        with paths.log.open("xb"):
            pass
        return paths

    def mark_complete(self, current: Node) -> None:
        paths = self.paths_for(current)
        if _lstat_as(paths.entry, stat.S_IFDIR) is None or _lstat_as(
            paths.output, stat.S_IFDIR
        ) is None:
            raise CasStateError("cannot publish completion without output directory")
        if _lstat_as(paths.log, stat.S_IFREG) is None:
            raise CasStateError("cannot publish completion without a regular log file")

        try:
            with paths.touch.open("xb"):
                pass
        except FileExistsError as error:
            raise CasStateError("completion marker already exists") from error

    def _prepare_run_directory(self) -> None:
        self._paths.require_confined(self._run_work, self._paths.work_root)
        self._run_work.mkdir(exist_ok=True)

    def _quarantine(self, paths: CasPaths) -> None:
        quarantine_root = self._paths.require_confined(
            self._run_work / "quarantine", self._paths.work_root
        )
        quarantine_root.mkdir(exist_ok=True)
        destination = self._paths.require_confined(
            quarantine_root / paths.entry.name, self._paths.work_root
        )
        if _lstat(destination) is not None:
            raise CasStateError(
                f"quarantine destination already exists: {destination}"
            )
        try:
            paths.entry.rename(destination)
        except OSError as error:
            raise CasStateError(
                f"could not quarantine incomplete CAS entry {paths.entry}"
            ) from error

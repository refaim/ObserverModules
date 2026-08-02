from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .graph import NODE_SLUG_MAX_LENGTH


_UID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_NODE_PATTERN = re.compile(
    rf"[a-z0-9][a-z0-9._-]{{0,{NODE_SLUG_MAX_LENGTH - 1}}}\Z"
)
_RUN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class PathSafetyError(ValueError):
    """A build path did not satisfy the output confinement policy."""


@dataclass(frozen=True)
class CasPaths:
    entry: Path
    output: Path
    touch: Path
    log: Path


def _is_reparse(path: Path) -> bool:
    try:
        information = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = getattr(information, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(information.st_mode) or bool(attributes & reparse_attribute)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class BuildPaths:
    """Derive and validate the repository-local ``out/{cas,work}`` layout."""

    def __init__(self, repository: Path | str) -> None:
        repository_path = _absolute(Path(repository))
        if not repository_path.is_dir() or _is_reparse(repository_path):
            raise PathSafetyError("repository must be an existing directory, not a reparse point")

        self.repository = repository_path
        self.output_root = repository_path / "out"
        self.cas_root = self.output_root / "cas"
        self.work_root = self.output_root / "work"
        self.locks_root = self.work_root / ".locks"

    def prepare(self) -> None:
        for path, root in (
            (self.output_root, self.output_root),
            (self.cas_root, self.cas_root),
            (self.work_root, self.work_root),
            (self.locks_root, self.work_root),
        ):
            self.require_confined(path, root).mkdir(exist_ok=True)

    def cas(self, uid: str, node: str) -> CasPaths:
        self._require_uid(uid)
        if _NODE_PATTERN.fullmatch(node) is None:
            raise PathSafetyError(
                "invalid lowercase node slug; expected at most "
                f"{NODE_SLUG_MAX_LENGTH} ASCII characters: {node!r}"
            )
        entry = self.require_confined(self.cas_root / f"{uid}-{node}", self.cas_root)
        paths = CasPaths(
            entry=entry,
            output=entry / "out",
            touch=entry / "touch",
            log=entry / "log.txt",
        )
        for path in (paths.output, paths.touch, paths.log):
            self._reject_existing_reparse_points(path)
        return paths

    def run_work(self, run_identifier: str) -> Path:
        if _RUN_PATTERN.fullmatch(run_identifier) is None:
            raise PathSafetyError(f"invalid run identifier: {run_identifier!r}")
        return self.require_confined(self.work_root / run_identifier, self.work_root)

    def lock(self, uid: str) -> Path:
        self._require_uid(uid)
        return self.require_confined(self.locks_root / f"{uid}.lock", self.work_root)

    def coordination_lock(self) -> Path:
        return self.require_confined(self.locks_root / "coordination.lock", self.work_root)

    def lease(self, run_identifier: str) -> Path:
        self.run_work(run_identifier)
        return self.require_confined(
            self.locks_root / f"run-{run_identifier}.lease", self.work_root
        )

    def require_confined(self, candidate: Path | str, allowed_root: Path | str) -> Path:
        path = _absolute(Path(candidate))
        root = _absolute(Path(allowed_root))
        if root not in (self.output_root, self.cas_root, self.work_root):
            raise PathSafetyError(f"unexpected allowed root: {root}")
        if not _is_within(path, root):
            raise PathSafetyError(f"path is outside allowed root: {path}")
        self._reject_existing_reparse_points(path)
        return path

    def _reject_existing_reparse_points(self, path: Path) -> None:
        if not _is_within(path, self.repository):
            raise PathSafetyError(f"path is outside repository: {path}")
        current = path
        while True:
            if _is_reparse(current):
                raise PathSafetyError(f"reparse point is forbidden in build path: {current}")
            if current == self.repository:
                return
            current = current.parent

    @staticmethod
    def _require_uid(uid: str) -> None:
        if _UID_PATTERN.fullmatch(uid) is None:
            raise PathSafetyError(f"UID must be 32 lowercase hexadecimal characters: {uid!r}")

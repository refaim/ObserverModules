"""Conservative cleanup of the exact repository-local generated output tree."""

from __future__ import annotations

import argparse
from collections.abc import Collection, Sequence
import os
from pathlib import Path
import shutil
import stat

from filelock import FileLock, Timeout

from core.paths import BuildPaths, PathSafetyError


_MODES = ("all", "stale-work")


class CleanError(RuntimeError):
    """Generated output cannot be proven safe and inactive."""


def _tree(paths: BuildPaths, root: Path) -> None:
    for directory, directories, files in root.walk(follow_symlinks=False):
        for name in (*directories, *files):
            current = paths.require_confined(directory / name, paths.output_root)
            mode = os.lstat(current).st_mode
            if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                raise CleanError(f"unsupported output entry type: {current}")


def _validate(paths: BuildPaths) -> bool:
    output = paths.require_confined(paths.output_root, paths.output_root)
    try:
        mode = os.lstat(output).st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(mode):
        raise CleanError(f"output root is not a directory: {output}")
    if output.resolve(strict=True) != paths.repository / "out":
        raise CleanError(f"output root does not resolve to repository/out: {output}")
    for child in output.iterdir():
        current = paths.require_confined(child, paths.output_root)
        if current.name not in {"cas", "work"}:
            raise CleanError(f"unexpected output entry: {current}")
        if not stat.S_ISDIR(os.lstat(current).st_mode):
            raise CleanError(f"output entry is not a directory: {current}")
    _tree(paths, output)
    return True


def _runs(paths: BuildPaths) -> tuple[Path, ...]:
    result = []
    for entry in sorted(paths.work_root.iterdir()):
        if entry == paths.locks_root:
            continue
        if not entry.is_dir():
            raise CleanError(f"unexpected work entry: {entry}")
        try:
            result.append(paths.run_work(entry.name))
        except PathSafetyError as error:
            raise CleanError(f"unexpected work entry: {entry}") from error
    return tuple(result)


def _require_inactive(paths: BuildPaths) -> tuple[Path, ...]:
    inactive = []
    for entry in sorted(paths.locks_root.iterdir()):
        if entry == paths.coordination_lock():
            continue
        if not entry.is_file():
            raise CleanError(f"unexpected lock entry: {entry}")
        try:
            if entry.suffix == ".lock":
                paths.lock(entry.stem)
            elif entry.name.startswith("run-") and entry.suffix == ".lease":
                paths.lease(entry.name[4:-6])
            else:
                raise PathSafetyError("unknown lock name")
        except PathSafetyError as error:
            raise CleanError(f"unexpected lock entry: {entry}") from error
        lock = FileLock(entry, timeout=0, fallback_to_soft=False, preserve_lock_file=True)
        try:
            with lock:
                pass
        except Timeout as error:
            raise CleanError(f"active build lock: {entry}") from error
        inactive.append(entry)
    return tuple(inactive)


def _require_no_active_runs(paths: BuildPaths, owned_lease: Path | None) -> None:
    owned_active = owned_lease is None
    for entry in sorted(paths.locks_root.iterdir()):
        if not (entry.name.startswith("run-") and entry.suffix == ".lease"):
            continue
        try:
            paths.lease(entry.name[4:-6])
        except PathSafetyError as error:
            raise CleanError(f"unexpected run lease: {entry}") from error
        lock = FileLock(
            entry, timeout=0, fallback_to_soft=False, preserve_lock_file=True
        )
        try:
            with lock:
                if entry == owned_lease:
                    raise CleanError(f"owned run lease is not active: {entry}")
        except Timeout as error:
            if entry == owned_lease:
                owned_active = True
                continue
            raise CleanError(f"active build lease: {entry}") from error
    if not owned_active:
        raise CleanError(f"owned run lease is missing: {owned_lease}")


def sweep_cas(
    repository: Path | str, live_uids: Collection[str], *, owned_run_id: str | None = None
) -> tuple[Path, ...]:
    """Remove unlocked canonical CAS entries absent from the explicit live set."""

    paths = BuildPaths(repository)
    owned_lease = paths.lease(owned_run_id) if owned_run_id is not None else None
    live = frozenset(live_uids)
    for uid in live:
        paths.cas(uid)
    if not _validate(paths):
        return ()
    paths.work_root.mkdir(exist_ok=True)
    paths.locks_root.mkdir(exist_ok=True)
    coordination = FileLock(
        paths.coordination_lock(), timeout=0, fallback_to_soft=False,
        preserve_lock_file=True,
    )
    try:
        with coordination:
            if not _validate(paths) or not paths.cas_root.exists():
                return ()
            _require_no_active_runs(paths, owned_lease)
            removed = []
            for entry in sorted(paths.cas_root.iterdir()):
                uid = entry.name
                if (
                    len(uid) != 32
                    or any(character not in "0123456789abcdef" for character in uid)
                    or uid in live
                ):
                    continue
                candidate = paths.cas(uid)
                node = FileLock(
                    paths.lock(uid), timeout=0, fallback_to_soft=False,
                    preserve_lock_file=True,
                )
                try:
                    with node:
                        candidate = paths.cas(uid)
                        shutil.rmtree(candidate.entry)
                except Timeout:
                    continue
                removed.append(candidate.entry)
            return tuple(removed)
    except Timeout as error:
        raise CleanError("active build coordination lock") from error


def clean(repository: Path | str, mode: str = "all") -> tuple[Path, ...]:
    """Remove all output or inactive work, including legacy CAS entry names."""

    if mode not in _MODES:
        raise ValueError(f"unsupported clean mode: {mode}")
    paths = BuildPaths(repository)
    if not _validate(paths):
        return ()
    paths.work_root.mkdir(exist_ok=True)
    paths.locks_root.mkdir(exist_ok=True)
    coordination = FileLock(
        paths.coordination_lock(), timeout=0, fallback_to_soft=False,
        preserve_lock_file=True,
    )
    try:
        with coordination:
            if not _validate(paths):
                return ()
            runs = _runs(paths)
            inactive = _require_inactive(paths)
            if mode == "all":
                removed = []
                if paths.cas_root.exists():
                    removed.append(paths.cas_root)
                    shutil.rmtree(paths.cas_root)
                for run in runs:
                    shutil.rmtree(run)
                for lock in inactive:
                    lock.unlink()
                return tuple(removed) + runs + inactive
            for run in runs:
                shutil.rmtree(run)
            return runs
    except Timeout as error:
        raise CleanError("active build coordination lock") from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="observer-build clean")
    parser.add_argument("repository", type=Path)
    parser.add_argument("--mode", choices=_MODES, default="all")
    args = parser.parse_args(argv)
    for removed in clean(args.repository, args.mode):
        print(removed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

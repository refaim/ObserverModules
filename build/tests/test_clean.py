from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import runpy
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from filelock import FileLock, Timeout


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.clean import CleanError, clean, main  # noqa: E402
from core.paths import BuildPaths, PathSafetyError  # noqa: E402


UID = "0123456789abcdef0123456789abcdef"


class CleanTests(unittest.TestCase):
    def repository(self, root: Path) -> tuple[Path, BuildPaths]:
        repository = root / "repo"
        repository.mkdir(parents=True)
        return repository, BuildPaths(repository)

    def test_all_removes_only_exact_generated_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, paths = self.repository(Path(temporary))
            paths.prepare()
            (paths.cas_root / "entry").mkdir()
            (paths.cas_root / "entry/data").write_text("generated", encoding="utf-8")
            run = paths.run_work("old-run")
            run.mkdir()
            inactive_lock = paths.lock(UID)
            with FileLock(inactive_lock, timeout=0, fallback_to_soft=False,
                          preserve_lock_file=True):
                pass
            keep = repository / "keep.txt"
            keep.write_text("user", encoding="utf-8")

            self.assertEqual(clean(repository), (paths.cas_root, run, inactive_lock))

            self.assertFalse(paths.cas_root.exists())
            self.assertFalse(run.exists())
            self.assertEqual(
                tuple(paths.locks_root.iterdir()), (paths.coordination_lock(),)
            )
            self.assertEqual(keep.read_text(encoding="utf-8"), "user")

            paths.prepare()
            self.assertTrue(paths.cas_root.is_dir())
            self.assertTrue(paths.work_root.is_dir())

    def test_stale_work_removes_only_inactive_exact_run_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, paths = self.repository(Path(temporary))
            paths.prepare()
            cached = paths.cas_root / "entry/out"
            cached.mkdir(parents=True)
            (cached / "module.so").touch()
            runs = tuple(paths.run_work(name) for name in ("run-b", "run-a"))
            for run in runs:
                run.mkdir()
                (run / "scratch.obj").touch()
            lock = FileLock(paths.lock(UID), timeout=0, fallback_to_soft=False,
                            preserve_lock_file=True)
            with lock:
                pass

            removed = clean(repository, "stale-work")

            self.assertEqual(removed, tuple(sorted(runs)))
            self.assertTrue((cached / "module.so").is_file())
            self.assertTrue(paths.locks_root.is_dir())
            self.assertTrue(paths.lock(UID).is_file())
            self.assertTrue(all(not run.exists() for run in runs))

    def test_active_native_lock_refuses_every_mode_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, paths = self.repository(Path(temporary))
            paths.prepare()
            run = paths.run_work("active-run")
            run.mkdir()
            marker = run / "partial.obj"
            marker.touch()
            lock = FileLock(paths.lock(UID), timeout=0, fallback_to_soft=False,
                            preserve_lock_file=True)
            with lock:
                for mode in ("all", "stale-work"):
                    with self.subTest(mode=mode), self.assertRaisesRegex(CleanError, "active build"):
                        clean(repository, mode)
            self.assertTrue(marker.is_file())

    def test_active_run_lease_refuses_and_coordination_is_held_through_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, paths = self.repository(Path(temporary))
            paths.prepare()
            run = paths.run_work("leased-run")
            run.mkdir()
            lease = FileLock(paths.lease("leased-run"), timeout=0, fallback_to_soft=False,
                             preserve_lock_file=True)
            with lease, self.assertRaisesRegex(CleanError, "active build"):
                clean(repository, "stale-work")
            self.assertTrue(run.is_dir())

            original_remove = shutil.rmtree

            def remove(target: Path) -> None:
                with self.assertRaises(Timeout), FileLock(
                    paths.coordination_lock(), timeout=0, fallback_to_soft=False,
                    preserve_lock_file=True,
                ):
                    pass
                original_remove(target)

            with mock.patch("core.clean.shutil.rmtree", side_effect=remove):
                self.assertEqual(clean(repository, "stale-work"), (run,))

    def test_unsafe_layout_types_and_reparse_points_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file_repository, file_paths = self.repository(root / "file")
            file_paths.output_root.touch()
            with self.assertRaisesRegex(CleanError, "directory"):
                clean(file_repository)

            unexpected_repository, unexpected = self.repository(root / "unexpected")
            unexpected.output_root.mkdir()
            (unexpected.output_root / "personal.txt").touch()
            with self.assertRaisesRegex(CleanError, "unexpected output entry"):
                clean(unexpected_repository)

            child_repository, child = self.repository(root / "child-file")
            child.output_root.mkdir()
            child.cas_root.touch()
            with self.assertRaisesRegex(CleanError, "not a directory"):
                clean(child_repository)

            reparse_repository, reparse = self.repository(root / "reparse")
            reparse.prepare()
            (reparse.cas_root / "entry").mkdir()
            with mock.patch(
                "core.paths._is_reparse", side_effect=lambda path: Path(path) == reparse.cas_root / "entry"
            ), self.assertRaisesRegex(PathSafetyError, "reparse point"):
                clean(reparse_repository)

            resolved_repository, resolved = self.repository(root / "resolved")
            resolved.output_root.mkdir()
            original_resolve = Path.resolve

            def resolve(path: Path, *, strict: bool = False) -> Path:
                if path == resolved.output_root:
                    return resolved.repository / "elsewhere"
                return original_resolve(path, strict=strict)

            with mock.patch.object(Path, "resolve", resolve), self.assertRaisesRegex(
                CleanError, "does not resolve"
            ):
                clean(resolved_repository)

            special_repository, special = self.repository(root / "special")
            special.prepare()
            special_entry = special.cas_root / "special"
            special_entry.touch()
            original_lstat = os.lstat

            def lstat(path: Path) -> os.stat_result | SimpleNamespace:
                return (SimpleNamespace(st_mode=stat.S_IFIFO)
                        if Path(path) == special_entry else original_lstat(path))

            with mock.patch("core.clean.os.lstat", side_effect=lstat), self.assertRaisesRegex(
                CleanError, "unsupported output entry type"
            ):
                clean(special_repository)

    def test_invalid_lock_work_entry_mode_and_missing_output_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            empty_repository, empty = self.repository(root / "empty")
            self.assertEqual(clean(empty_repository), ())
            self.assertFalse(empty.output_root.exists())
            with self.assertRaisesRegex(ValueError, "mode"):
                clean(empty_repository, "unknown")

            no_work_repository, no_work = self.repository(root / "no-work")
            no_work.output_root.mkdir()
            no_work.cas_root.mkdir()
            self.assertEqual(clean(no_work_repository, "stale-work"), ())

            lock_repository, locks = self.repository(root / "bad-lock")
            locks.prepare()
            (locks.locks_root / "surprise.txt").touch()
            with self.assertRaisesRegex(CleanError, "lock entry"):
                clean(lock_repository)

            (locks.locks_root / "surprise.txt").unlink()
            (locks.locks_root / "bad.lock").touch()
            with self.assertRaisesRegex(CleanError, "lock entry"):
                clean(lock_repository)

            (locks.locks_root / "bad.lock").unlink()
            (locks.locks_root / "directory.lock").mkdir()
            with self.assertRaisesRegex(CleanError, "lock entry"):
                clean(lock_repository)

            work_repository, work = self.repository(root / "bad-work")
            work.prepare()
            (work.work_root / "unexpected.txt").touch()
            with self.assertRaisesRegex(CleanError, "work entry"):
                clean(work_repository, "stale-work")
            (work.work_root / "unexpected.txt").unlink()
            (work.work_root / "bad name").mkdir()
            with self.assertRaisesRegex(CleanError, "work entry"):
                clean(work_repository, "stale-work")

    def test_coordination_races_are_rejected_without_claiming_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, paths = self.repository(root / "active")
            paths.prepare()
            with FileLock(paths.coordination_lock(), timeout=0, fallback_to_soft=False,
                          preserve_lock_file=True), self.assertRaisesRegex(
                              CleanError, "coordination lock"
                          ):
                clean(repository)

            vanished_repository, vanished = self.repository(root / "vanished")
            vanished.prepare()
            with mock.patch("core.clean._validate", side_effect=(True, False)):
                self.assertEqual(clean(vanished_repository), ())

            no_cas_repository, no_cas = self.repository(root / "no-cas")
            no_cas.work_root.mkdir(parents=True)
            self.assertEqual(clean(no_cas_repository), ())
            self.assertEqual(
                tuple(no_cas.locks_root.iterdir()), (no_cas.coordination_lock(),)
            )

    def test_cli_prints_exact_removed_paths_and_module_entry_point_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, paths = self.repository(Path(temporary))
            paths.prepare()
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main((str(repository), "--mode", "stale-work")), 0)
            self.assertEqual(output.getvalue(), "")

            paths.run_work("old-run").mkdir()
            with redirect_stdout(output):
                self.assertEqual(main((str(repository), "--mode", "stale-work")), 0)
            self.assertIn(str(paths.run_work("old-run")), output.getvalue())

            safe = Path(temporary) / "entry"
            safe.mkdir()
            with mock.patch.object(sys, "argv", ["clean.py", str(safe)]), \
                 redirect_stdout(StringIO()), self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(BUILD_ROOT / "core/clean.py"), run_name="__main__")
            self.assertEqual(raised.exception.code, 0)
            self.assertFalse((safe / "out").exists())


if __name__ == "__main__":
    unittest.main()

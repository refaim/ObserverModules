from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.paths import BuildPaths, PathSafetyError


UID = "0123456789abcdef0123456789abcdef"


class BuildPathsTests(unittest.TestCase):
    def test_prepare_creates_only_cas_and_work_at_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths = BuildPaths(repository)

            paths.prepare()

            self.assertEqual(paths.output_root, repository / "out")
            self.assertEqual(
                {child.name for child in paths.output_root.iterdir()},
                {"cas", "work"},
            )
            self.assertTrue(paths.cas_root.is_dir())
            self.assertTrue(paths.work_root.is_dir())
            self.assertTrue(paths.locks_root.is_dir())

    def test_cas_paths_use_only_uid_for_entry_output_touch_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths = BuildPaths(repository)

            entry = paths.cas(UID)

            self.assertEqual(
                entry.entry,
                repository / "out" / "cas" / UID,
            )
            self.assertEqual(entry.output, entry.entry / "out")
            self.assertEqual(entry.touch, entry.entry / "touch")
            self.assertEqual(entry.log, entry.entry / "log.txt")

    def test_run_work_and_lock_paths_are_confined_under_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths = BuildPaths(repository)

            self.assertEqual(
                paths.run_work("20260801-abcdef"),
                repository / "out" / "work" / "20260801-abcdef",
            )
            self.assertEqual(
                paths.lock(UID),
                repository / "out" / "work" / ".locks" / f"{UID}.lock",
            )
            self.assertEqual(
                paths.coordination_lock(),
                repository / "out" / "work" / ".locks" / "coordination.lock",
            )
            self.assertEqual(
                paths.lease("20260801-abcdef"),
                repository / "out" / "work" / ".locks" / "run-20260801-abcdef.lease",
            )

    def test_invalid_uid_and_run_identifier_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths = BuildPaths(repository)

            for invalid_uid in ("", "ABCDEF" * 5 + "AB", "../escape", "0" * 31):
                with self.subTest(uid=invalid_uid):
                    with self.assertRaises(PathSafetyError):
                        paths.cas(invalid_uid)

            for invalid_run in ("", ".", "..", "../escape", "with/slash", "a" * 129):
                with self.subTest(run=invalid_run):
                    with self.assertRaises(PathSafetyError):
                        paths.run_work(invalid_run)
                    with self.assertRaises(PathSafetyError):
                        paths.lease(invalid_run)

    def test_cas_accepts_only_the_content_uid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths = BuildPaths(repository)

            self.assertEqual(paths.cas(UID).entry, repository / "out" / "cas" / UID)
            with self.assertRaises(TypeError):
                paths.cas(UID, "legacy-readable-name")  # type: ignore[call-arg]

    def test_confined_path_rejects_parent_escape_and_unexpected_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths = BuildPaths(repository)

            with self.assertRaisesRegex(PathSafetyError, "outside allowed root"):
                paths.require_confined(paths.cas_root / ".." / "work", paths.cas_root)
            with self.assertRaisesRegex(PathSafetyError, "unexpected allowed root"):
                paths.require_confined(repository / "elsewhere", repository / "elsewhere")
            with self.assertRaisesRegex(PathSafetyError, "outside repository"):
                paths._reject_existing_reparse_points(repository.parent / "outside")

    def test_existing_reparse_component_and_leaf_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            regular = BuildPaths(repository)
            regular.prepare()
            entry = regular.cas(UID)
            entry.entry.mkdir()
            entry.touch.touch()

            reparse_paths = {
                regular.cas_root,
                entry.touch,
            }

            def is_reparse(path: Path) -> bool:
                return path in reparse_paths

            with patch("core.paths._is_reparse", side_effect=is_reparse):
                paths = BuildPaths(repository)
                with self.assertRaisesRegex(PathSafetyError, "reparse point"):
                    paths.cas(UID)

                reparse_paths.remove(regular.cas_root)
                with self.assertRaisesRegex(PathSafetyError, "reparse point"):
                    paths.require_confined(entry.touch, paths.cas_root)

    def test_repository_must_be_an_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            with self.assertRaisesRegex(PathSafetyError, "existing directory"):
                BuildPaths(missing)

            file_path = Path(temporary) / "file"
            file_path.touch()
            with self.assertRaisesRegex(PathSafetyError, "existing directory"):
                BuildPaths(file_path)


if __name__ == "__main__":
    unittest.main()

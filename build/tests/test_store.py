from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.graph import Command, Node  # noqa: E402
from core.paths import BuildPaths, PathSafetyError  # noqa: E402
from core.store import CasStateError, CasStore  # noqa: E402


RUN_ID = "20260801-test"


def node(name: str = "analyze-renpy.pickle") -> Node:
    return Node(
        name=name,
        uid=hashlib.md5(name.encode("utf-8"), usedforsecurity=False).hexdigest(),
        pool="cpu",
        command=Command(("tool",)),
    )


class CasStoreTests(unittest.TestCase):
    def make_store(self, repository: Path) -> tuple[BuildPaths, CasStore]:
        paths = BuildPaths(repository)
        paths.prepare()
        return paths, CasStore(paths, RUN_ID)

    @staticmethod
    def publish_files(paths: BuildPaths, current: Node) -> None:
        cas = paths.cas(current.uid, current.name)
        cas.entry.mkdir()
        cas.output.mkdir()
        cas.log.write_text("command succeeded\n", encoding="utf-8")
        cas.touch.touch()

    def test_node_maps_to_readable_uid_and_name_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths, store = self.make_store(repository)
            current = node()

            cas = store.paths_for(current)

            self.assertEqual(
                cas.entry,
                paths.cas_root / f"{current.uid}-{current.name}",
            )

    def test_complete_entry_is_a_warm_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths, store = self.make_store(repository)
            current = node()
            self.publish_files(paths, current)

            self.assertTrue(store.is_complete(current))

    def test_missing_or_malformed_marker_and_missing_outputs_are_cache_misses(self) -> None:
        cases = (
            "missing-touch",
            "nonempty-touch",
            "touch-directory",
            "missing-log",
            "log-directory",
            "missing-output",
            "output-file",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary) / "repo"
                repository.mkdir()
                paths, store = self.make_store(repository)
                current = node()
                self.publish_files(paths, current)
                cas = paths.cas(current.uid, current.name)

                if case == "missing-touch":
                    cas.touch.unlink()
                elif case == "nonempty-touch":
                    cas.touch.write_bytes(b"not a completion marker")
                elif case == "touch-directory":
                    cas.touch.unlink()
                    cas.touch.mkdir()
                elif case == "missing-log":
                    cas.log.unlink()
                elif case == "log-directory":
                    cas.log.unlink()
                    cas.log.mkdir()
                elif case == "missing-output":
                    cas.output.rmdir()
                elif case == "output-file":
                    cas.output.rmdir()
                    cas.output.touch()

                self.assertFalse(store.is_complete(current))

    def test_prepare_quarantines_incomplete_entry_inside_current_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths, store = self.make_store(repository)
            current = node()
            old = paths.cas(current.uid, current.name)
            old.entry.mkdir()
            old.output.mkdir()
            (old.output / "partial.obj").write_bytes(b"partial")

            prepared = store.prepare_entry(current)

            quarantine = paths.run_work(RUN_ID) / "quarantine" / old.entry.name
            self.assertEqual(prepared, paths.cas(current.uid, current.name))
            self.assertEqual((quarantine / "out" / "partial.obj").read_bytes(), b"partial")
            self.assertTrue(prepared.entry.is_dir())
            self.assertTrue(prepared.output.is_dir())
            self.assertTrue(prepared.log.is_file())
            self.assertFalse(prepared.touch.exists())

    def test_prepare_resolves_node_paths_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths, store = self.make_store(repository)
            current = node()

            with mock.patch.object(paths, "cas", wraps=paths.cas) as resolve:
                store.prepare_entry(current)

            resolve.assert_called_once_with(current.uid, current.name)

    def test_prepare_never_mutates_complete_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths, store = self.make_store(repository)
            current = node()
            self.publish_files(paths, current)
            cas = paths.cas(current.uid, current.name)
            sentinel = cas.output / "result.bin"
            sentinel.write_bytes(b"immutable")
            before = {
                child.relative_to(cas.entry): (child.stat().st_mtime_ns, child.read_bytes())
                for child in (cas.log, cas.touch, sentinel)
            }

            prepared = store.prepare_entry(current)

            after = {
                child.relative_to(cas.entry): (child.stat().st_mtime_ns, child.read_bytes())
                for child in (cas.log, cas.touch, sentinel)
            }
            self.assertEqual(prepared, cas)
            self.assertEqual(after, before)
            self.assertFalse((paths.run_work(RUN_ID) / "quarantine").exists())

    def test_prepare_fails_if_incomplete_entry_cannot_be_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths, store = self.make_store(repository)
            current = node()
            cas = paths.cas(current.uid, current.name)
            cas.entry.mkdir()
            cas.output.mkdir()
            destination = paths.run_work(RUN_ID) / "quarantine" / cas.entry.name
            destination.mkdir(parents=True)

            with self.assertRaisesRegex(CasStateError, "quarantine destination already exists"):
                store.prepare_entry(current)

            self.assertTrue(cas.entry.is_dir())
            self.assertTrue(destination.is_dir())

    def test_prepare_reports_move_failure_without_deleting_or_retrying(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths, store = self.make_store(repository)
            current = node()
            cas = paths.cas(current.uid, current.name)
            cas.entry.mkdir()
            cas.output.mkdir()

            with mock.patch.object(Path, "rename", side_effect=OSError("locked")) as rename:
                with self.assertRaisesRegex(CasStateError, "could not quarantine"):
                    store.prepare_entry(current)

            rename.assert_called_once()
            self.assertTrue(cas.entry.is_dir())
            self.assertFalse(
                (paths.run_work(RUN_ID) / "quarantine" / cas.entry.name).exists()
            )

    def test_reparse_component_is_rejected_instead_of_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            regular = BuildPaths(repository)
            regular.prepare()
            current = node()
            self.publish_files(regular, current)
            cas = regular.cas(current.uid, current.name)

            reparse_paths = {cas.output}

            with mock.patch(
                "core.paths._is_reparse",
                side_effect=lambda path: path in reparse_paths,
            ):
                store = CasStore(BuildPaths(repository), RUN_ID)
                with self.assertRaisesRegex(PathSafetyError, "reparse point"):
                    store.is_complete(current)
                with self.assertRaisesRegex(PathSafetyError, "reparse point"):
                    store.prepare_entry(current)

    def test_mark_complete_publishes_zero_byte_marker_after_outputs_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths, store = self.make_store(repository)
            current = node()
            cas = store.prepare_entry(current)
            cas.log.write_text("success\n", encoding="utf-8")

            store.mark_complete(current)

            self.assertTrue(cas.touch.is_file())
            self.assertEqual(cas.touch.stat().st_size, 0)
            self.assertTrue(store.is_complete(current))

    def test_mark_complete_requires_output_and_log_and_never_overwrites_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths, store = self.make_store(repository)
            current = node()
            cas = store.prepare_entry(current)
            cas.log.unlink()

            with self.assertRaisesRegex(CasStateError, "log file"):
                store.mark_complete(current)
            self.assertFalse(cas.touch.exists())

            cas.log.write_text("success\n", encoding="utf-8")
            cas.touch.write_bytes(b"existing")
            original_stat = os.lstat(cas.touch)

            with self.assertRaisesRegex(CasStateError, "completion marker already exists"):
                store.mark_complete(current)

            self.assertEqual(cas.touch.read_bytes(), b"existing")
            self.assertEqual(os.lstat(cas.touch).st_mtime_ns, original_stat.st_mtime_ns)

    def test_mark_complete_reports_exclusive_publication_race(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            _paths, store = self.make_store(repository)
            current = node()
            cas = store.prepare_entry(current)

            with mock.patch.object(
                Path,
                "open",
                autospec=True,
                side_effect=FileExistsError("raced"),
            ):
                with self.assertRaisesRegex(CasStateError, "marker already exists"):
                    store.mark_complete(current)

            self.assertFalse(cas.touch.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

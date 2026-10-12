from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import core.clean as core_clean  # noqa: E402
from core.clean import CleanError, clean, main, prepare_fresh  # noqa: E402


class CleanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def output(self, *names: str) -> dict[str, Path]:
        out = self.repository / "out"
        out.mkdir(exist_ok=True)
        created = {}
        for name in names:
            target = out / name
            target.mkdir()
            (target / "artifact.txt").write_text(name, encoding="utf-8")
            created[name] = target
        return created

    def test_all_removes_every_owned_directory_and_preserves_unrelated_output(self) -> None:
        created = self.output("native", "reports", "packages", "sol-team")
        (self.repository / "out" / "notes.txt").write_text("keep", encoding="utf-8")

        removed = clean(self.repository, "all")

        self.assertEqual(removed, (created["native"], created["reports"], created["packages"]))
        self.assertTrue((self.repository / "out" / "sol-team" / "artifact.txt").is_file())
        self.assertTrue((self.repository / "out" / "notes.txt").is_file())

    def test_reports_mode_keeps_the_native_tree(self) -> None:
        created = self.output("native", "reports", "packages")

        removed = clean(self.repository, "reports")

        self.assertEqual(removed, (created["reports"], created["packages"]))
        self.assertTrue((created["native"] / "artifact.txt").is_file())

    def test_an_unsupported_mode_and_missing_repository_are_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported clean mode"):
            clean(self.repository, "everything")
        with self.assertRaises(FileNotFoundError):
            clean(self.repository / "absent", "all")

    def test_a_repository_without_output_is_a_no_op(self) -> None:
        self.assertEqual(clean(self.repository, "all"), ())
        (self.repository / "out").write_text("not a directory", encoding="utf-8")
        self.assertEqual(clean(self.repository, "all"), ())
        self.assertFalse(core_clean._is_reparse(self.repository / "out" / "absent"))

    def test_a_reparse_output_root_is_refused(self) -> None:
        out = self.repository / "out"
        out.mkdir()
        with (
            mock.patch.object(core_clean, "_is_reparse", return_value=True),
            self.assertRaisesRegex(CleanError, "reparse point is forbidden"),
        ):
            clean(self.repository, "all")

    def test_a_reparse_anywhere_below_an_owned_tree_is_refused(self) -> None:
        created = self.output("native")
        nested = created["native"] / "artifact.txt"
        with (
            mock.patch.object(core_clean, "_is_reparse", side_effect=lambda path: Path(path) == nested),
            self.assertRaisesRegex(CleanError, "reparse point is forbidden"),
        ):
            clean(self.repository, "all")
        self.assertTrue(nested.is_file())

    def test_a_reparse_owned_directory_itself_is_refused(self) -> None:
        created = self.output("native")
        with (
            mock.patch.object(
                core_clean, "_is_reparse", side_effect=lambda path: Path(path) == created["native"]
            ),
            self.assertRaisesRegex(CleanError, "reparse point is forbidden"),
        ):
            clean(self.repository, "all")
        self.assertTrue((created["native"] / "artifact.txt").is_file())

    def test_a_non_directory_owned_entry_is_unlinked(self) -> None:
        out = self.repository / "out"
        out.mkdir()
        entry = out / "native"
        entry.write_text("stale", encoding="utf-8")

        removed = clean(self.repository, "all")

        self.assertEqual(removed, (entry,))
        self.assertFalse(os.path.lexists(entry))

    def test_main_prints_every_removed_directory(self) -> None:
        created = self.output("reports")
        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main((str(self.repository), "--mode", "reports")), 0)
        self.assertEqual(stdout.getvalue().splitlines(), [str(created["reports"])])

    def test_prepare_fresh_rebuilds_an_empty_tree_under_the_boundary(self) -> None:
        stale = self.repository / "out" / "reports" / "verify" / "old.txt"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale", encoding="utf-8")
        target = prepare_fresh(self.repository / "out" / "reports" / "verify", self.repository)
        self.assertEqual(list(target.iterdir()), [])
        self.assertTrue(target.is_dir())

    def test_prepare_fresh_refuses_an_escape_from_the_boundary(self) -> None:
        outside = self.repository.parent / "outside-output"
        with self.assertRaisesRegex(CleanError, "escapes its owned root"):
            prepare_fresh(outside, self.repository)

    def test_prepare_fresh_refuses_a_reparse_target(self) -> None:
        target = self.repository / "out" / "reports" / "verify"
        with (
            mock.patch.object(core_clean, "_is_reparse", side_effect=lambda path: Path(path) == target),
            self.assertRaisesRegex(CleanError, "reparse point is forbidden"),
        ):
            prepare_fresh(target, self.repository)

    def test_prepare_fresh_refuses_a_reparse_ancestor(self) -> None:
        ancestor = self.repository / "out"
        with (
            mock.patch.object(core_clean, "_is_reparse", side_effect=lambda path: Path(path) == ancestor),
            self.assertRaisesRegex(CleanError, "reparse point is forbidden"),
        ):
            prepare_fresh(self.repository / "out" / "reports" / "verify", self.repository)


if __name__ == "__main__":
    unittest.main()

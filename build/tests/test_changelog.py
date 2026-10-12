from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


BUILD_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BUILD_ROOT))

from core.changelog import (  # noqa: E402
    CHANGELOG_FILES,
    ChangelogError,
    changelog_path,
    latest_notes,
    release_notes,
)
from core.module_version import MODULES  # noqa: E402


LANGUAGES = ("en", "ru")
HEADER = {"en": "Version", "ru": "\u0412\u0435\u0440\u0441\u0438\u044f"}


def write_bytes(root: Path, module: str, language: str, data: bytes) -> Path:
    path = root / "src" / "modules" / module / CHANGELOG_FILES[language]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_text(root: Path, module: str, language: str, text: str) -> Path:
    return write_bytes(root, module, language, text.encode("utf-8"))


def entry(language: str, version: str, *bullets: str) -> str:
    header = f"{HEADER[language]} {version}"
    lines = [header, "-" * len(header)]
    lines.extend(f" * {bullet}" for bullet in bullets)
    return "\n".join(lines) + "\n"


class LatestNotesTests(unittest.TestCase):
    def test_the_english_latest_entry_body_becomes_markdown_bullets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_text(
                root,
                "renpy",
                "en",
                "Version 3.1.0\n"
                "-------------\n"
                " * Support the RPA-2.0 and RPA-3.0 archive versions.\n"
                " + Unpack entries with extra headers without junk bytes at the end of the file.\n",
            )

            self.assertEqual(
                latest_notes(root, "renpy", "3.1.0"),
                "- Support the RPA-2.0 and RPA-3.0 archive versions.\n"
                "- Unpack entries with extra headers without junk bytes at the end of the file.",
            )

    def test_the_russian_latest_entry_body_becomes_markdown_bullets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_text(
                root,
                "renpy",
                "ru",
                "\u0412\u0435\u0440\u0441\u0438\u044f 3.1.0\n"
                "-------------\n"
                " * Russian note about RPA-2.0 and RPA-3.0.\n",
            )

            self.assertEqual(
                latest_notes(root, "renpy", "3.1.0", language="ru"),
                "- Russian note about RPA-2.0 and RPA-3.0.",
            )

    def test_only_the_top_entry_is_rendered_and_older_sections_never_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_text(
                root,
                "renpy",
                "en",
                "Version 3.1.0\n"
                "-------------\n"
                " * new behaviour\n"
                "\n"
                "Version 3.0.0\n"
                "-------------\n"
                " * old behaviour\n",
            )

            self.assertEqual(latest_notes(root, "renpy", "3.1.0"), "- new behaviour")

    def test_blank_lines_inside_the_body_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_text(
                root,
                "zanzarah",
                "ru",
                "\u0412\u0435\u0440\u0441\u0438\u044f 2.1.0\n"
                "-------------\n"
                "\n"
                " * one\n"
                "\n"
                " + two\n"
                "\n",
            )

            self.assertEqual(latest_notes(root, "zanzarah", "2.1.0", language="ru"), "- one\n- two")

    def test_a_bom_and_crlf_line_endings_are_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_bytes(
                root,
                "rpgmaker",
                "en",
                "\ufeffVersion 1.1.0\r\n-----\r\n * first\r\n + second\r\n".encode("utf-8"),
            )

            self.assertEqual(latest_notes(root, "rpgmaker", "1.1.0"), "- first\n- second")

    def test_the_path_helper_resolves_known_modules_and_languages_only(self) -> None:
        self.assertEqual(changelog_path("renpy"), "src/modules/renpy/ChangeLog.en.txt")
        self.assertEqual(changelog_path("renpy", "ru"), "src/modules/renpy/ChangeLog.ru.txt")
        for module in MODULES:
            for language in LANGUAGES:
                self.assertEqual(
                    changelog_path(module, language),
                    f"src/modules/{module}/ChangeLog.{language}.txt",
                )
        with self.assertRaisesRegex(ChangelogError, "unknown module"):
            changelog_path("pickle")
        with self.assertRaisesRegex(ChangelogError, "unknown changelog language"):
            changelog_path("renpy", "de")

    def test_invalid_inputs_fail_with_a_change_log_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_text(root, "renpy", "en", entry("en", "3.1.0", "note"))

            cases = (
                (lambda: latest_notes(root, "pickle", "1.0.0"), "unknown module"),
                (lambda: latest_notes(root, "renpy", "3.1", language="en"), "canonical"),
                (lambda: latest_notes(root, "renpy", "3.1.0", language="de"), "unknown changelog language"),
                (lambda: latest_notes(root, "rpgmaker", "1.1.0"), "missing or unreadable"),
                (lambda: latest_notes(root, "renpy", "3.0.0"), "does not match"),
            )
            for index, (call, message) in enumerate(cases):
                with self.subTest(index=index), self.assertRaisesRegex(ChangelogError, message):
                    call()

    def test_malformed_headers_underlines_and_bodies_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            documents = (
                ("", "3.1.0", "header"),
                ("ChangeLog for renpy\n * note\n", "3.1.0", "header"),
                ("Version 3.1\n-----\n * note\n", "3.1.0", "canonical"),
                ("Version 3.1.0", "3.1.0", "underlined"),
                ("Version 3.1.0\n * note\n", "3.1.0", "underlined"),
                ("Version 3.1.0\n-----\nplain prose\n", "3.1.0", "not a bullet"),
                ("Version 3.1.0\n-----\n", "3.1.0", "no notes"),
                (
                    "Version 3.1.0\n-----\nVersion 3.0.0\n-----\n * old\n",
                    "3.1.0",
                    "no notes",
                ),
            )
            for index, (text, version, message) in enumerate(documents):
                write_text(root, "renpy", "en", text)
                with self.subTest(index=index), self.assertRaisesRegex(ChangelogError, message):
                    latest_notes(root, "renpy", version)

    def test_the_russian_header_is_required_for_the_russian_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # A Russian file carrying an English header is malformed for its language.
            write_text(root, "renpy", "ru", entry("en", "3.1.0", "note"))

            with self.assertRaisesRegex(ChangelogError, "header"):
                latest_notes(root, "renpy", "3.1.0", language="ru")

    def test_non_utf8_bytes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_bytes(root, "renpy", "en", "Version 3.1.0\n-----\n * caf\xe9\n".encode("latin-1"))

            with self.assertRaisesRegex(ChangelogError, "UTF-8"):
                latest_notes(root, "renpy", "3.1.0")


class ReleaseNotesTests(unittest.TestCase):
    def test_the_release_body_carries_both_language_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_text(root, "renpy", "en", entry("en", "3.1.0", "english note"))
            write_text(root, "renpy", "ru", entry("ru", "3.1.0", "russian note"))

            notes = release_notes(root, "renpy", "3.1.0")

            self.assertIn("Version 3.1.0", notes)
            self.assertIn("\u0412\u0435\u0440\u0441\u0438\u044f 3.1.0", notes)
            self.assertIn("- english note", notes)
            self.assertIn("- russian note", notes)

    def test_either_language_entry_disagreeing_fails_the_release_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_text(root, "renpy", "en", entry("en", "3.1.0", "english note"))
            write_text(root, "renpy", "ru", entry("ru", "3.0.0", "stale russian note"))

            with self.assertRaisesRegex(ChangelogError, "does not match"):
                release_notes(root, "renpy", "3.1.0")


class RepositoryChangeLogTests(unittest.TestCase):
    def test_every_shipped_module_keeps_bilingual_change_logs(self) -> None:
        for module in MODULES:
            version = (
                REPOSITORY / "src" / "modules" / module / "VERSION"
            ).read_text(encoding="utf-8").strip()
            for language in LANGUAGES:
                with self.subTest(module=module, language=language):
                    notes = latest_notes(REPOSITORY, module, version, language=language)
                    lines = notes.splitlines()
                    self.assertTrue(lines)
                    self.assertTrue(all(line.startswith("- ") for line in lines))

    def test_the_bilingual_histories_start_at_one_and_agree_on_each_version(self) -> None:
        for module in MODULES:
            sequences = {
                language: _versions(
                    (REPOSITORY / changelog_path(module, language)).read_text(encoding="utf-8")
                )
                for language in LANGUAGES
            }
            with self.subTest(module=module):
                self.assertEqual(sequences["en"], sequences["ru"])
                self.assertGreaterEqual(len(sequences["en"]), 2)
                self.assertEqual(sequences["en"][-1], "1.0.0")
                self.assertEqual(
                    sequences["en"][0],
                    (
                        REPOSITORY / "src" / "modules" / module / "VERSION"
                    ).read_text(encoding="utf-8").strip(),
                )


def _versions(text: str) -> list[str]:
    versions: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("Version ", "\u0412\u0435\u0440\u0441\u0438\u044f ")):
            versions.append(stripped.split(" ", 1)[1])
    return versions


if __name__ == "__main__":
    unittest.main(verbosity=2)

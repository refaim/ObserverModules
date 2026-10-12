"""Read the latest human-written ChangeLog entry of one module as release notes."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re

from core.module_version import ModuleVersionError, parse_version, version_path


# Every module keeps one complete human history per language beside its VERSION file. The two
# files share the format but not the header word, so the release notes can carry both sections.
CHANGELOG_FILES = {"en": "ChangeLog.en.txt", "ru": "ChangeLog.ru.txt"}
LANGUAGES = tuple(CHANGELOG_FILES)
DEFAULT_LANGUAGE = "en"
_HEADERS = {"en": "Version", "ru": "\u0412\u0435\u0440\u0441\u0438\u044f"}
_UNDERLINE = re.compile(r"-+")
_BULLET = re.compile(r"[ \t]*[*+][ \t]+(.+)")


class ChangelogError(RuntimeError):
    """A module ChangeLog is missing, malformed, or does not describe the release."""


def _language(language: str) -> str:
    if language not in CHANGELOG_FILES:
        raise ChangelogError(f"unknown changelog language: {language!r}")
    return language


def changelog_path(module: str, language: str = DEFAULT_LANGUAGE) -> str:
    """Return the repository-relative human ChangeLog of one module and language."""

    _language(language)
    try:
        path = version_path(module)
    except ModuleVersionError as error:
        raise ChangelogError(f"unknown module: {module}") from error
    return PurePosixPath(path).with_name(CHANGELOG_FILES[language]).as_posix()


def _canonical(version: str, what: str) -> str:
    try:
        parse_version(version)
    except ModuleVersionError as error:
        raise ChangelogError(f"{what} must be a canonical X.Y.Z version: {version!r}") from error
    return version


def _header(language: str) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(_HEADERS[language])}[ \t]+(\S+)")


def latest_notes(
    repository: Path, module: str, expected_version: str, language: str = DEFAULT_LANGUAGE
) -> str:
    """Return the module's latest ChangeLog entry body as Markdown bullets.

    The top entry must name exactly ``expected_version``; only that entry's bullets are
    rendered, so an older history section can never leak into the release body.
    """

    language = _language(language)
    path = repository / changelog_path(module, language)
    _canonical(expected_version, "expected module version")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as error:
        raise ChangelogError(f"module ChangeLog is not valid UTF-8: {path}") from error
    except OSError as error:
        raise ChangelogError(f"module ChangeLog is missing or unreadable: {path}") from error

    header = _header(language)
    lines = [line.rstrip() for line in text.splitlines()]
    if not lines or (match := header.fullmatch(lines[0])) is None:
        raise ChangelogError(
            f"module ChangeLog has no '{_HEADERS[language]} X.Y.Z' header: {path}"
        )
    version = _canonical(match.group(1), "ChangeLog version")
    if version != expected_version:
        raise ChangelogError(
            f"module ChangeLog version {version!r} does not match {expected_version!r}"
        )
    if len(lines) < 2 or _UNDERLINE.fullmatch(lines[1]) is None:
        raise ChangelogError(f"module ChangeLog version is not underlined: {path}")

    bullets: list[str] = []
    for line in lines[2:]:
        if header.fullmatch(line) is not None:
            break
        if not line:
            continue
        bullet = _BULLET.fullmatch(line)
        if bullet is None:
            raise ChangelogError(f"module ChangeLog entry body is not a bullet: {line!r}")
        bullets.append(bullet.group(1))
    if not bullets:
        raise ChangelogError(f"module ChangeLog latest entry has no notes: {path}")
    return "\n".join(f"- {bullet}" for bullet in bullets)


def release_notes(repository: Path, module: str, expected_version: str) -> str:
    """Return the published release body: the English and Russian sections side by side."""

    sections: list[str] = []
    for language in LANGUAGES:
        title = f"{_HEADERS[language]} {expected_version}"
        bullets = latest_notes(repository, module, expected_version, language=language)
        sections.append(f"{title}\n{'-' * len(title)}\n{bullets}")
    return "\n\n".join(sections)

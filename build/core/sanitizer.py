"""Fail-closed semantic gates for native sanitizer logs."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import re


class SanitizerError(RuntimeError):
    pass


_FINDINGS = {
    "asan": re.compile(
        r"(?:ERROR|SUMMARY):\s*AddressSanitizer|AddressSanitizer:DEADLYSIGNAL",
        re.IGNORECASE,
    ),
    "ubsan": re.compile(
        r"runtime error:|UndefinedBehaviorSanitizer(?::DEADLYSIGNAL|: undefined-behavior)",
        re.IGNORECASE,
    ),
}


def require_clean_log(sanitizer: str, content: str) -> None:
    try:
        pattern = _FINDINGS[sanitizer]
    except KeyError as error:
        raise SanitizerError(f"unsupported sanitizer: {sanitizer}") from error
    if pattern.search(content):
        raise SanitizerError(f"{sanitizer} finding in test log")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    gate = commands.add_parser("gate")
    gate.add_argument("sanitizer", choices=tuple(_FINDINGS))
    gate.add_argument("log")
    args = parser.parse_args(argv)
    require_clean_log(
        args.sanitizer, Path(args.log).read_text(encoding="utf-8-sig")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

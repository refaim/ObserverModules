"""Semantic policy for LLVM coverage reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from collections.abc import Mapping, Sequence


class CoverageError(RuntimeError):
    pass


def _metric(totals: Mapping[str, object], name: str) -> tuple[int, int]:
    try:
        metric = totals[name]
        if not isinstance(metric, Mapping):
            raise TypeError
        count, covered = metric["count"], metric["covered"]
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or isinstance(covered, bool)
            or not isinstance(covered, int)
            or count < 0
            or covered < 0
            or covered > count
        ):
            raise TypeError
        return count, covered
    except (KeyError, TypeError) as error:
        raise CoverageError("malformed LLVM coverage totals") from error


def require_full_coverage(document: Mapping[str, object]) -> None:
    """Require nonempty, exact 100% first-party line and branch coverage."""

    try:
        data = document["data"]
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], Mapping):
            raise TypeError
        totals = data[0]["totals"]
        if not isinstance(totals, Mapping):
            raise TypeError
    except (KeyError, TypeError) as error:
        raise CoverageError("malformed LLVM coverage report") from error

    for name in ("lines", "branches"):
        count, covered = _metric(totals, name)
        if count == 0:
            raise CoverageError(f"no first-party {name} in LLVM coverage report")
        if covered != count:
            raise CoverageError(f"first-party {name} coverage is {covered}/{count}, required 100%")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    gate = commands.add_parser("gate")
    gate.add_argument("report")
    args = parser.parse_args(argv)
    require_full_coverage(json.loads(Path(args.report).read_text(encoding="utf-8-sig")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

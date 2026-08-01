#!/usr/bin/env python3
"""Fail unless a Mutation Testing Elements report is non-empty and fully killed."""

import argparse
import json
import sys
from pathlib import Path


class ReportError(ValueError):
    """The mutation report cannot prove the repository gate."""


def _mutant_identity(filename, mutant):
    mutant_id = mutant.get("id", "<missing-id>")
    location = mutant.get("location", {})
    start = location.get("start", {}) if isinstance(location, dict) else {}
    line = start.get("line") if isinstance(start, dict) else None
    source = f"{filename}:{line}" if isinstance(line, int) else filename
    return f"{source} [{mutant_id}]"


def validate(report):
    if not isinstance(report, dict):
        raise ReportError("report root must be a JSON object")

    files = report.get("files")
    if not isinstance(files, dict):
        raise ReportError("report field 'files' must be an object")

    reached = []
    for filename, file_result in files.items():
        if not isinstance(filename, str) or not isinstance(file_result, dict):
            raise ReportError("every report file entry must have a string name and object value")
        mutants = file_result.get("mutants")
        if not isinstance(mutants, list):
            raise ReportError(f"report file '{filename}' must contain a mutants array")
        for mutant in mutants:
            if not isinstance(mutant, dict):
                raise ReportError(f"report file '{filename}' contains a non-object mutant")
            status = mutant.get("status")
            if not isinstance(status, str) or not status:
                raise ReportError(f"mutant {_mutant_identity(filename, mutant)} has no valid status")
            reached.append((filename, mutant, status))

    if not reached:
        raise ReportError("report contains no reached mutants")

    failures = [item for item in reached if item[2] != "Killed"]
    if failures:
        details = ", ".join(
            f"{status}: {_mutant_identity(filename, mutant)}"
            for filename, mutant, status in failures[:20]
        )
        remainder = len(failures) - 20
        if remainder:
            details += f", and {remainder} more"
        raise ReportError(f"{len(failures)} reached mutants were not killed: {details}")

    return len(reached)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="Mutation Testing Elements JSON report")
    arguments = parser.parse_args(argv)

    try:
        with arguments.report.open(encoding="utf-8") as stream:
            report = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        print(f"mutation report is not valid JSON: {error}", file=sys.stderr)
        return 1

    try:
        count = validate(report)
    except ReportError as error:
        print(f"mutation gate failed: {error}", file=sys.stderr)
        return 1

    print(f"mutation report: {count} reached mutants, all killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

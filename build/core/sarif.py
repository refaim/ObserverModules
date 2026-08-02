from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any


_DIAGNOSTIC = re.compile(
    r"^(.+)\((\d+),(\d+)\):\s+(warning|error)\s*:\s*(.*?)\s+\[([^\]]+)\]"
    r"(?:\s+\[[^\]]+\.vcxproj\])?\s*$"
)


class SarifError(ValueError):
    pass


class SarifFindingsError(SarifError):
    pass


def _read(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise SarifError(f"cannot read SARIF report {path}: {error}") from error
    if not isinstance(document, dict) or document.get("version") != "2.1.0":
        raise SarifError(f"SARIF report is not version 2.1.0: {path}")
    runs = document.get("runs")
    if not isinstance(runs, list) or not runs or any(not isinstance(run, dict) for run in runs):
        raise SarifError(f"SARIF runs must be a non-empty list of objects: {path}")
    return document, runs


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _document(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "runs": runs, "version": "2.1.0"}


def _tidy_result(finding: tuple[str, int, int, str, str, str]) -> dict[str, Any]:
    path, line, column, level, rule, message = finding
    location = {
        "artifactLocation": {"uri": path},
        "region": {"startColumn": column, "startLine": line},
    }
    return {
        "level": level,
        "locations": [{"physicalLocation": location}],
        "message": {"text": message},
        "ruleId": rule,
    }


def _tidy_rule(rule: str) -> dict[str, Any]:
    return {"id": rule, "name": rule, "shortDescription": {"text": f"clang-tidy check {rule}"}}


def clang_tidy_to_sarif(
    repository: Path, log_root: Path, output: Path, automation_id: str
) -> None:
    repository = repository.resolve()
    findings: set[tuple[str, int, int, str, str, str]] = set()
    for log in sorted(log_root.rglob("*.ClangTidy.log")) if log_root.is_dir() else ():
        for line in log.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            match = _DIAGNOSTIC.match(line)
            if not match:
                continue
            rule = next(
                (check for raw in match[6].split(",") if (check := raw.strip()) and not check.startswith("-")),
                None,
            )
            if not rule:
                continue
            try:
                relative = Path(match[1]).resolve().relative_to(repository).as_posix()
            except (OSError, ValueError):
                continue
            line_number, column, level = int(match[2]), int(match[3]), match[4]
            findings.add((relative, line_number, column, level, rule, match[5].strip()))

    ordered = sorted(findings, key=lambda item: (item[0], item[1], item[2], item[4], item[5], item[3]))
    driver = {
        "informationUri": "https://clang.llvm.org/extra/clang-tidy/",
        "name": "clang-tidy",
        "rules": [_tidy_rule(rule) for rule in sorted({finding[4] for finding in findings})],
    }
    run = {
        "automationDetails": {"id": automation_id},
        "results": [_tidy_result(finding) for finding in ordered],
        "tool": {"driver": driver},
    }
    _write(output, _document([run]))


def normalize_msvc(source: Path, output: Path, automation_id: str) -> None:
    document, runs = _read(source)
    for index, run in enumerate(runs, 1):
        details = run.get("automationDetails")
        if not isinstance(details, dict):
            details = run["automationDetails"] = {}
        details["id"] = automation_id if len(runs) == 1 else f"{automation_id.rstrip('/')}/run-{index}/"
    _write(output, document)


def merge_sarif(inputs: Iterable[Path], output: Path) -> None:
    identified: list[tuple[str, dict[str, Any]]] = []
    for path in inputs:
        _, runs = _read(path)
        for run in runs:
            details = run.get("automationDetails")
            identity = details.get("id") if isinstance(details, dict) else None
            if not isinstance(identity, str) or not identity:
                raise SarifError(f"SARIF run has no automationDetails.id: {path}")
            identified.append((identity, run))
    if not identified:
        raise SarifError("SARIF merge requires at least one input")
    identities = [identity for identity, _ in identified]
    if len(set(identities)) != len(identities):
        raise SarifError("SARIF merge found duplicate automationDetails.id values")
    _write(output, _document([run for _, run in sorted(identified)]))


def require_clean(inputs: Iterable[Path]) -> None:
    count = 0
    for path in inputs:
        _, runs = _read(path)
        for run in runs:
            results = run.get("results", [])
            if not isinstance(results, list) or any(not isinstance(result, dict) for result in results):
                raise SarifError(f"SARIF results must be a list of objects: {path}")
            count += sum(result.get("level", "warning") in {"warning", "error"} for result in results)
    if count:
        raise SarifFindingsError(f"analysis found {count} warning/error finding(s)")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    def add_command(name: str, *arguments: str, output: str | None = None) -> None:
        command = commands.add_parser(name)
        for argument in arguments:
            command.add_argument(argument, **({"nargs": "+"} if argument == "inputs" else {}))
        if output:
            command.add_argument("--output-name", default=output)

    add_command("normalize-msvc", "input", "automation_id", output="renpy.sarif")
    add_command("convert-tidy", "repository", "log_root", "automation_id", output="renpy.sarif")
    add_command("merge", "inputs", output="analysis.sarif")
    add_command("gate", "input")
    args = parser.parse_args(argv)

    raw_root = os.environ.get("OBSERVER_OUT_DIR")
    if not raw_root:
        parser.error("OBSERVER_OUT_DIR is required")
    root = Path(raw_root).resolve()
    if not root.is_dir():
        parser.error("OBSERVER_OUT_DIR must be an existing directory")
    if args.command == "gate":
        require_clean((Path(args.input),))
        return 0
    output = (root / args.output_name).resolve()
    if output == root or not output.is_relative_to(root):
        parser.error("output name must be confined to OBSERVER_OUT_DIR")
    if args.command == "normalize-msvc":
        normalize_msvc(Path(args.input), output, args.automation_id)
    elif args.command == "convert-tidy":
        clang_tidy_to_sarif(Path(args.repository), Path(args.log_root), output, args.automation_id)
    else:
        merge_sarif([Path(path) for path in args.inputs], output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

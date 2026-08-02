"""Release PE and BinSkim policy shared by fine-grained audit leaves."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from collections.abc import Sequence


class AuditError(RuntimeError):
    pass


_MACHINES = {
    "x86": r"14C machine \(x86\)",
    "x64": r"8664 machine \(x64\)",
    "arm64": r"AA64 machine \(ARM64\)",
}
_ALLOWED_DLLS = {
    "advapi32.dll",
    "bcrypt.dll",
    "kernel32.dll",
    "ntdll.dll",
    "ole32.dll",
    "oleaut32.dll",
    "shell32.dll",
    "shlwapi.dll",
    "user32.dll",
}
_FORBIDDEN_DLL = re.compile(
    r"^(?:vcruntime|msvcp|ucrtbase|api-ms-win-crt-|ext-ms-win-crt-|zlib|zstd|xxhash|clang_rt\.).*\.dll$",
    re.IGNORECASE,
)


def require_release_pe(
    architecture: str, headers: str, dependents: str, exports: str
) -> None:
    try:
        machine = _MACHINES[architecture]
    except KeyError as error:
        raise AuditError(f"unsupported machine architecture: {architecture}") from error
    if re.search(machine, headers) is None:
        raise AuditError(f"wrong PE machine for {architecture}")

    dependencies = {
        match.group(1)
        for line in dependents.splitlines()
        if (match := re.fullmatch(r"\s+([A-Za-z0-9._-]+\.dll)\s*", line))
    }
    unexpected = sorted(
        dependency
        for dependency in dependencies
        if _FORBIDDEN_DLL.match(dependency)
        or (
            dependency.casefold() not in _ALLOWED_DLLS
            and re.match(r"^(?:api|ext)-ms-win-.*\.dll$", dependency, re.IGNORECASE) is None
        )
    )
    if unexpected:
        raise AuditError("unexpected DLL dependencies: " + ", ".join(unexpected))

    actual_exports = {
        match.group(1)
        for line in exports.splitlines()
        if (match := re.match(r"^\s+\d+\s+[0-9A-F]+\s+[0-9A-F]+\s+(\S+)", line))
    }
    expected_exports = {"LoadSubModule", "UnloadSubModule"}
    if actual_exports != expected_exports:
        raise AuditError("unexpected exports: " + ", ".join(sorted(actual_exports)))


def require_clean_binskim(document: dict[str, object]) -> None:
    findings = []
    for run in document.get("runs", []):
        rules = {
            rule["id"]: rule.get("defaultConfiguration", {}).get("level", "warning")
            for rule in run.get("tool", {}).get("driver", {}).get("rules", [])
        }
        for result in run.get("results", []):
            rule = result.get("ruleId", "<unknown>")
            level = result.get("level", rules.get(rule, "warning"))
            if level in {"warning", "error"} and not (level == "warning" and rule == "BA2027"):
                findings.append(f"{level}:{rule}")
    if findings:
        raise AuditError("BinSkim unapproved findings: " + ", ".join(findings))


def _run_binskim(tool: Path, binary: Path) -> None:
    raw_output = os.environ.get("OBSERVER_OUT_DIR")
    if not raw_output or not (output_root := Path(raw_output)).is_dir():
        raise AuditError("OBSERVER_OUT_DIR must be an existing directory")
    report = output_root / "binskim.sarif"
    argv = [
        str(tool), "analyze", str(binary), "--level", "Error;Warning", "--kind", "Fail",
        "--local-symbol-directories", str(binary.parent), "--output", str(report),
        "--log", "ForceOverwrite", "--quiet", "--disable-telemetry",
    ]
    subprocess.run(argv, check=True)
    if not report.is_file():
        raise AuditError("BinSkim did not produce binskim.sarif")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    pe = commands.add_parser("pe")
    for argument in ("architecture", "headers", "dependents", "exports"):
        pe.add_argument(argument)
    report = commands.add_parser("binskim")
    report.add_argument("report")
    run = commands.add_parser("run-binskim")
    run.add_argument("tool")
    run.add_argument("binary")
    args = parser.parse_args(argv)
    if args.command == "pe":
        texts = [Path(getattr(args, name)).read_text(encoding="utf-8-sig") for name in ("headers", "dependents", "exports")]
        require_release_pe(args.architecture, *texts)
    elif args.command == "binskim":
        require_clean_binskim(json.loads(Path(args.report).read_text(encoding="utf-8-sig")))
    else:
        _run_binskim(Path(args.tool), Path(args.binary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

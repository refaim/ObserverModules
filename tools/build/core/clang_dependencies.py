"""Resolve clang-cl translation-unit inputs through ``clang-scan-deps``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import os
from pathlib import Path
import subprocess
from typing import Any


class ClangDependencyError(RuntimeError):
    pass


def _file(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ClangDependencyError(f"invalid clang dependency {label}: {path}") from error
    if not resolved.is_file():
        raise ClangDependencyError(f"invalid clang dependency {label}: {path}")
    return resolved


def load_compile_command(command: Path, source: Path, compiler: Path) -> dict[str, Any]:
    """Load one clang ``-MJ`` fragment and remove its capture-only argument."""

    expected_source, expected_compiler = _file(source, "source"), _file(compiler, "compiler")
    try:
        content = command.read_text(encoding="utf-8-sig").rstrip()
        document = json.loads(content[:-1] if content.endswith(",") else content)
    except (OSError, json.JSONDecodeError) as error:
        raise ClangDependencyError(f"invalid clang compilation-command JSON: {command}") from error
    if not isinstance(document, dict):
        raise ClangDependencyError("invalid clang compilation-command JSON object")
    try:
        directory = Path(document["directory"]).resolve(strict=True)
    except (KeyError, OSError, TypeError) as error:
        raise ClangDependencyError("invalid clang compilation-command directory") from error
    if not directory.is_dir():
        raise ClangDependencyError("invalid clang compilation-command directory")
    try:
        reported_source = Path(document["file"])
        reported_source = (directory / reported_source).resolve(strict=True)
    except (KeyError, OSError, TypeError) as error:
        raise ClangDependencyError("invalid clang compilation-command source") from error
    if reported_source != expected_source:
        raise ClangDependencyError("clang compilation-command source mismatch")
    arguments = document.get("arguments")
    if not isinstance(arguments, list) or not arguments or not all(
        isinstance(item, str) for item in arguments
    ):
        raise ClangDependencyError("invalid clang compilation-command arguments")
    try:
        reported_compiler = Path(arguments[0]).resolve(strict=True)
    except OSError as error:
        raise ClangDependencyError("invalid clang compilation-command compiler") from error
    if reported_compiler != expected_compiler:
        raise ClangDependencyError("clang compilation-command compiler mismatch")
    return {
        **document,
        "directory": str(directory),
        "file": str(expected_source),
        "arguments": [
            str(expected_compiler),
            *(item for item in arguments[1:] if not item.casefold().startswith("/clang:-mj")),
        ],
    }


def dependency_manifest(source: Path, document: object) -> dict[str, object]:
    """Convert experimental-full scanner JSON to the existing canonical manifest."""

    expected_source = _file(source, "source")
    try:
        if not isinstance(document, dict):
            raise TypeError
        units = document["translation-units"]
        if not isinstance(units, list) or not units:
            raise TypeError
        dependencies: list[Path] = []
        for unit in units:
            commands = unit["commands"]
            if not isinstance(commands, list) or not commands:
                raise TypeError
            for command in commands:
                file_dependencies = command["file-deps"]
                if not isinstance(file_dependencies, list) or not all(
                    isinstance(item, str) for item in file_dependencies
                ):
                    raise TypeError
                for item in file_dependencies:
                    path = Path(item)
                    if not path.is_absolute():
                        raise TypeError
                    resolved = path.resolve(strict=True)
                    if not resolved.is_file():
                        raise OSError
                    dependencies.append(resolved)
    except (KeyError, OSError, TypeError) as error:
        raise ClangDependencyError("invalid clang-scan-deps scan output") from error
    if expected_source not in dependencies:
        raise ClangDependencyError("invalid clang-scan-deps scan output: source is absent")
    unique = {
        os.path.normcase(str(path)): str(path)
        for path in dependencies
        if path != expected_source
    }
    return {
        "Data": {
            "Source": str(expected_source),
            "Includes": sorted(unique.values(), key=str.casefold),
        }
    }


def scan_dependencies(
    source: Path, command: Path, scanner: Path, compiler: Path, build_directory: Path
) -> dict[str, object]:
    """Run the exact scanner over the exact clang-cl command captured by MSBuild."""

    scanner = _file(scanner, "scanner")
    compilation = load_compile_command(command, source, compiler)
    try:
        build_directory = build_directory.resolve(strict=True)
    except OSError as error:
        raise ClangDependencyError(
            f"invalid clang dependency build directory: {build_directory}"
        ) from error
    if not build_directory.is_dir():
        raise ClangDependencyError(
            f"invalid clang dependency build directory: {build_directory}"
        )
    database = build_directory / "compile_commands.json"
    try:
        database.write_text(
            json.dumps([compilation], ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        argv = [
            str(scanner),
            "-format=experimental-full",
            f"-compilation-database={database}",
            "-j",
            "1",
        ]
        result = subprocess.run(
            argv,
            cwd=compilation["directory"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise ClangDependencyError(f"failed to launch clang-scan-deps: {scanner}") from error
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no diagnostic"
        raise ClangDependencyError(
            f"clang-scan-deps failed ({result.returncode}): {detail[:400]}"
        )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ClangDependencyError("invalid clang-scan-deps JSON output") from error
    return dependency_manifest(source, document)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan")
    scan.add_argument("source")
    scan.add_argument("command_file")
    scan.add_argument("scanner")
    scan.add_argument("compiler")
    args = parser.parse_args(argv)
    raw_output = os.environ.get("OBSERVER_OUT_DIR")
    if not raw_output or not (output := Path(raw_output)).is_dir():
        raise ClangDependencyError("OBSERVER_OUT_DIR must be an existing directory")
    raw_build = os.environ.get("OBSERVER_BUILD_DIR")
    if not raw_build or not (build := Path(raw_build)).is_dir():
        raise ClangDependencyError("OBSERVER_BUILD_DIR must be an existing directory")
    document = scan_dependencies(
        Path(args.source),
        Path(args.command_file),
        Path(args.scanner),
        Path(args.compiler),
        build,
    )
    (output / "dependencies.json").write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

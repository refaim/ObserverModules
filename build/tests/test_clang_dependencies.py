from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.clang_dependencies import (  # noqa: E402
    ClangDependencyError,
    dependency_manifest,
    load_compile_command,
    main,
    scan_dependencies,
)


class ClangDependencyTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        source = root / "source file.cpp"
        compiler = root / "clang-cl.exe"
        scanner = root / "clang-scan-deps.exe"
        command = root / "compile-command.json"
        for path in (source, compiler, scanner):
            path.touch()
        command.write_text(
            json.dumps(
                {
                    "directory": str(root),
                    "file": str(source),
                    "output": "source.obj",
                    "arguments": [
                        str(compiler),
                        "-xc++",
                        str(source),
                        "-o",
                        "source.obj",
                        "/clang:-MJcapture.json",
                    ],
                }
            )
            + ",\n",
            encoding="utf-8",
        )
        return source, compiler, scanner, command

    @staticmethod
    def scan_document(source: Path, *includes: Path) -> dict[str, object]:
        return {
            "modules": [],
            "translation-units": [
                {
                    "commands": [
                        {
                            "input-file": str(source),
                            "file-deps": [str(source), *(str(path) for path in includes)],
                        }
                    ]
                }
            ],
        }

    def test_load_command_validates_exact_compiler_source_and_removes_only_capture_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, compiler, _scanner, command = self.fixture(root)
            other_source = root / "other.cpp"
            other_compiler = root / "other-clang.exe"
            other_source.touch()
            other_compiler.touch()

            loaded = load_compile_command(command, source, compiler)

            self.assertEqual(loaded["directory"], str(root.resolve()))
            self.assertEqual(loaded["file"], str(source.resolve()))
            self.assertEqual(loaded["arguments"][0], str(compiler.resolve()))
            self.assertNotIn("/clang:-MJcapture.json", loaded["arguments"])
            self.assertIn("source.obj", loaded["arguments"])

            for field, value, message in (
                ("directory", str(root / "missing"), "directory"),
                ("directory", str(source), "directory"),
                ("file", str(root / "missing.cpp"), "source"),
                ("file", str(other_source), "source"),
                ("arguments", [str(root / "missing-clang.exe")], "compiler"),
                ("arguments", [str(other_compiler)], "compiler"),
                ("arguments", "not-an-array", "arguments"),
                ("arguments", [], "arguments"),
                ("arguments", [str(compiler), 7], "arguments"),
            ):
                document = json.loads(command.read_text(encoding="utf-8").rstrip("\n,"))
                document[field] = value
                command.write_text(json.dumps(document) + ",\n", encoding="utf-8")
                with self.subTest(field=field), self.assertRaisesRegex(
                    ClangDependencyError, message
                ):
                    load_compile_command(command, source, compiler)
                self.fixture(root)

            command.write_text("not json,\n", encoding="utf-8")
            with self.assertRaisesRegex(ClangDependencyError, "JSON"):
                load_compile_command(command, source, compiler)
            command.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ClangDependencyError, "JSON object"):
                load_compile_command(command, source, compiler)
            self.fixture(root)
            command.write_text(
                command.read_text(encoding="utf-8").rstrip("\n,"), encoding="utf-8"
            )
            self.assertEqual(load_compile_command(command, source, compiler)["file"], str(source))

    def test_scan_output_becomes_deterministic_absolute_deduplicated_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, first, second = root / "source.cpp", root / "first.h", root / "second.h"
            for path in (source, first, second):
                path.touch()

            manifest = dependency_manifest(
                source,
                self.scan_document(source, second, first, second),
            )

        self.assertEqual(
            manifest,
            {
                "Data": {
                    "Source": str(source.resolve()),
                    "Includes": [str(first.resolve()), str(second.resolve())],
                }
            },
        )

    def test_scan_output_rejects_missing_source_and_invalid_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.cpp"
            source.touch()
            missing = root / "missing.h"
            other = root / "other.cpp"
            other.touch()
            directory = root / "directory"
            directory.mkdir()
            invalid_documents = (
                "bad",
                {},
                {"translation-units": "bad"},
                {"translation-units": []},
                {"translation-units": [{}]},
                {"translation-units": [{"commands": []}]},
                {"translation-units": [{"commands": [{}]}]},
                {"translation-units": [{"commands": [{"file-deps": "bad"}]}]},
                {"translation-units": [{"commands": [{"file-deps": [str(source), 7]}]}]},
                {"translation-units": [{"commands": [{"file-deps": ["relative.h"]}]}]},
                self.scan_document(source, directory),
                self.scan_document(source, missing),
                self.scan_document(other),
            )
            for document in invalid_documents:
                with self.subTest(document=document), self.assertRaisesRegex(
                    ClangDependencyError, "scan output"
                ):
                    dependency_manifest(source, document)
            with self.assertRaisesRegex(ClangDependencyError, "source"):
                dependency_manifest(root / "missing.cpp", self.scan_document(source))
            with self.assertRaisesRegex(ClangDependencyError, "source"):
                dependency_manifest(root, self.scan_document(source))

    def test_scan_runs_exact_tool_with_captured_compilation_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, compiler, scanner, command = self.fixture(root)
            header = root / "header.h"
            header.touch()
            build = root / "build"
            build.mkdir()
            captured: dict[str, object] = {}

            def run(argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
                database = Path(next(item.split("=", 1)[1] for item in argv if item.startswith("-compilation-database=")))
                captured["argv"] = argv
                captured["options"] = options
                captured["database"] = json.loads(database.read_text(encoding="utf-8"))
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(self.scan_document(source, header)), ""
                )

            with mock.patch("core.clang_dependencies.subprocess.run", side_effect=run):
                manifest = scan_dependencies(source, command, scanner, compiler, build)

        self.assertEqual(manifest["Data"]["Includes"], [str(header.resolve())])
        self.assertEqual(captured["argv"][0], str(scanner.resolve()))
        self.assertIn("-format=experimental-full", captured["argv"])
        self.assertEqual(captured["options"]["cwd"], str(root.resolve()))
        self.assertEqual(len(captured["database"]), 1)
        self.assertTrue(Path(captured["argv"][2].split("=", 1)[1]).is_relative_to(build))

    def test_scan_failure_and_invalid_json_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, compiler, scanner, command = self.fixture(root)
            build = root / "build"
            build.mkdir()
            cases = (
                (subprocess.CompletedProcess([], 2, "", "bad flags"), "failed"),
                (subprocess.CompletedProcess([], 2, "", ""), "no diagnostic"),
                (subprocess.CompletedProcess([], 0, "not json", ""), "JSON"),
            )
            for result, message in cases:
                with (
                    self.subTest(message=message),
                    mock.patch("core.clang_dependencies.subprocess.run", return_value=result),
                    self.assertRaisesRegex(ClangDependencyError, message),
                ):
                    scan_dependencies(source, command, scanner, compiler, build)
            with (
                mock.patch("core.clang_dependencies.subprocess.run", side_effect=OSError("boom")),
                self.assertRaisesRegex(ClangDependencyError, "launch"),
            ):
                scan_dependencies(source, command, scanner, compiler, build)
            for invalid_build in (root / "missing", source):
                with self.assertRaisesRegex(ClangDependencyError, "build directory"):
                    scan_dependencies(
                        source, command, scanner, compiler, invalid_build
                    )

    def test_cli_writes_canonical_json_only_below_observer_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, compiler, scanner, command = self.fixture(root)
            output = root / "out"
            output.mkdir()
            build = root / "build"
            build.mkdir()
            expected = {"Data": {"Source": str(source.resolve()), "Includes": []}}
            arguments = ("scan", str(source), str(command), str(scanner), str(compiler))
            with (
                mock.patch("core.clang_dependencies.scan_dependencies", return_value=expected),
                mock.patch.dict(
                    os.environ,
                    {"OBSERVER_OUT_DIR": str(output), "OBSERVER_BUILD_DIR": str(build)},
                ),
            ):
                self.assertEqual(main(arguments), 0)
            self.assertEqual(
                (output / "dependencies.json").read_text(encoding="utf-8"),
                json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n",
            )

            for environment, message in (
                ({}, "OBSERVER_OUT_DIR"),
                ({"OBSERVER_OUT_DIR": str(source)}, "OBSERVER_OUT_DIR"),
                ({"OBSERVER_OUT_DIR": str(output)}, "OBSERVER_BUILD_DIR"),
                (
                    {
                        "OBSERVER_OUT_DIR": str(output),
                        "OBSERVER_BUILD_DIR": str(source),
                    },
                    "OBSERVER_BUILD_DIR",
                ),
                (
                    {
                        "OBSERVER_OUT_DIR": str(output),
                        "OBSERVER_BUILD_DIR": str(root / "missing"),
                    },
                    "OBSERVER_BUILD_DIR",
                ),
            ):
                with (
                    self.subTest(environment=environment),
                    mock.patch.dict(os.environ, environment, clear=True),
                    self.assertRaisesRegex(ClangDependencyError, message),
                ):
                    main(arguments)
            with (
                mock.patch.object(sys, "argv", ["clang_dependencies.py", *arguments]),
                mock.patch.dict(
                    os.environ,
                    {"OBSERVER_OUT_DIR": str(output), "OBSERVER_BUILD_DIR": str(build)},
                ),
                mock.patch(
                    "subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps(self.scan_document(source)), ""
                    ),
                ),
                self.assertWarnsRegex(RuntimeWarning, "core.clang_dependencies"),
                self.assertRaises(SystemExit) as raised,
            ):
                runpy.run_module("core.clang_dependencies", run_name="__main__")
            self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

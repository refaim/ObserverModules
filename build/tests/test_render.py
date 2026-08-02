from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jinja2 import UndefinedError


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.render import TemplateRenderer, ps_quote  # noqa: E402


class TemplateRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.templates = BUILD_ROOT / "templates"
        self.renderer = TemplateRenderer(self.templates)

    def variables(self) -> dict[str, object]:
        return {
            "name": "build-renpy-x64",
            "pool": "slot",
            "inputs": ["src/modules/renpy/renpy.vcxproj"],
            "results": [],
            "pwsh": "pwsh.exe",
            "msbuild": r"C:\Program Files\O'Brien Tools\MSBuild.exe",
            "project": r"C:\repo\RPG's\renpy.vcxproj",
            "target": "Build",
            "configuration": "Release",
            "platform": "x64",
            "msbuild_args": ["/p:WarningsAsErrors=true"],
        }

    def test_msbuild_leaf_inherits_complete_json_recipe(self) -> None:
        rendered = self.renderer.render("msbuild.ps1", self.variables())
        recipe = json.loads(rendered)

        self.assertEqual(recipe["name"], "build-renpy-x64")
        self.assertEqual(recipe["pool"], "slot")
        self.assertNotIn("outputs", recipe)
        self.assertEqual(recipe["results"], [])
        self.assertEqual(
            recipe["script"]["exec"],
            [
                "pwsh.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference = 'Stop'; "
                "& ([ScriptBlock]::Create([Console]::In.ReadToEnd()))",
            ],
        )
        self.assertNotIn("shell", recipe["script"])
        self.assertIn(
            "'C:\\Program Files\\O''Brien Tools\\MSBuild.exe'",
            recipe["script"]["data"],
        )
        self.assertIn("'C:\\repo\\RPG''s\\renpy.vcxproj'", recipe["script"]["data"])
        self.assertIn("'/p:Configuration=Release'", recipe["script"]["data"])
        self.assertIn("'/p:WarningsAsErrors=true'", recipe["script"]["data"])
        self.assertIn("$env:OBSERVER_OUT_DIR", recipe["script"]["data"])
        self.assertIn("$env:OBSERVER_BUILD_DIR", recipe["script"]["data"])
        self.assertNotIn(r"C:\repo\out\cas", recipe["script"]["data"])
        self.assertIn("${LASTEXITCODE}: $FilePath", recipe["script"]["data"])

    def test_powershell_stdin_transport_reports_parser_errors(self) -> None:
        variables = self.variables()
        variables["pwsh"] = shutil.which("pwsh")
        argv = json.loads(self.renderer.render("msbuild.ps1", variables))["script"][
            "exec"
        ]
        result = subprocess.run(
            argv,
            input=b"$invalid:\n",
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_missing_variable_fails_before_a_recipe_is_created(self) -> None:
        variables = self.variables()
        del variables["platform"]

        with self.assertRaisesRegex(UndefinedError, "platform.*undefined"):
            self.renderer.render("msbuild.ps1", variables)

    def test_inherited_recipe_renders_declared_typed_results(self) -> None:
        variables = self.variables()
        variables["results"] = [
            {
                "id": "binary/renpy/x64",
                "kind": "module",
                "media_type": "application/vnd.microsoft.portable-executable",
                "path": "bin/renpy.dll",
            }
        ]

        recipe = json.loads(self.renderer.render("msbuild.ps1", variables))

        self.assertEqual(recipe["results"], variables["results"])

    def test_power_shell_quote_is_a_single_literal(self) -> None:
        self.assertEqual(ps_quote("plain"), "'plain'")
        self.assertEqual(ps_quote("O'Brien"), "'O''Brien'")
        self.assertEqual(ps_quote(Path(r"C:\A B\file.txt")), r"'C:\A B\file.txt'")

    def test_msbuild_family_template_stays_reviewable(self) -> None:
        meaningful_lines = [
            line
            for line in (self.templates / "msbuild.ps1").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        self.assertLessEqual(len(meaningful_lines), 18)

    def test_output_postconditions_share_one_strict_template_macro(self) -> None:
        helper = self.templates / "_output.ps1"
        leaves = (
            "catch2-test.ps1",
            "clang-command.ps1",
            "clang-tidy.ps1",
            "fuzz-build.ps1",
            "msvc-analyze.ps1",
            "sanitizer-test.ps1",
            "source-dependencies.ps1",
            "vcpkg.ps1",
        )

        self.assertTrue(helper.is_file())
        self.assertIn("macro require_output", helper.read_text(encoding="utf-8"))
        for name in leaves:
            with self.subTest(template=name):
                content = (self.templates / name).read_text(encoding="utf-8")
                self.assertIn('from "_output.ps1" import require_output', content)
                self.assertNotIn("if (-not (Test-Path", content)

        variables = self.variables() | {
            "llvm_dir": "llvm",
            "source": "unit.cpp",
            "vcpkg_installed": "installed",
            "vcpkg_root": "vcpkg",
        }
        with self.assertRaisesRegex(UndefinedError, "project_name.*undefined"):
            self.renderer.render("msvc-analyze.ps1", variables)

    def test_catch2_shards_share_a_short_base_without_changing_rendered_bytes(self) -> None:
        base = self.templates / "catch2-test.ps1"
        self.assertTrue(base.is_file(), "Catch2 shard recipes must share one inherited base")

        variables: dict[str, object] = {
            "name": "fixture",
            "pool": "slot",
            "inputs": ["build-tests"],
            "results": [],
            "pwsh": "pwsh.exe",
            "artifacts": [
                {"name": "tests.exe", "source": "C:/cas/tests.exe"},
                {"name": "renpy.so", "source": "C:/cas/renpy.so"},
            ],
            "shard_count": 4,
            "shard_index": 2,
        }
        cases = {
            "native-test.ps1": (
                variables,
                "7c5777763ff4cd93f343b773511bec55c61acc717128c9bfff8d6735bcb96f83",
            ),
            "native-corpus-test.ps1": (
                variables,
                "85e7f28d606dac5a70d01be851b5e17ed47a650a8287c32163881dbce21a6033",
            ),
            "coverage-test.ps1": (
                variables,
                "2ea01c93809ed7985d11147e0a2a060b945c7b2f6645c5d0c7375c023ecaeacc",
            ),
            "sanitizer-test.ps1": (
                variables
                | {
                    "runtime": {
                        "name": "clang_rt.asan_dynamic-x86_64.dll",
                        "source": "C:/llvm/asan.dll",
                    },
                    "options_name": "ASAN_OPTIONS",
                    "options_value": "halt_on_error=1",
                },
                "261dc649182fe2353fcb0393a3eeb2a70ff9f17879adb654048305d6ae4289f1",
            ),
            "sanitizer-test.ps1:ubsan": (
                variables
                | {
                    "runtime": None,
                    "options_name": "UBSAN_OPTIONS",
                    "options_value": "halt_on_error=1",
                },
                "47ddf49be4a5df8279eb7a6bf5229550da255d3256395493e30fad45d8726800",
            ),
        }
        for case, (values, expected) in cases.items():
            template = case.partition(":")[0]
            with self.subTest(case=case):
                rendered = self.renderer.render(template, values).encode()
                self.assertEqual(hashlib.sha256(rendered).hexdigest(), expected)

        for name in ("native-test.ps1", "coverage-test.ps1", "sanitizer-test.ps1"):
            with self.subTest(template=name):
                content = (self.templates / name).read_text(encoding="utf-8")
                self.assertIn('{% extends "catch2-test.ps1" %}', content)
                self.assertLessEqual(len(content.splitlines()), 10)

    def test_template_root_must_be_an_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / "template.txt"
            file_path.write_text("content", encoding="utf-8")

            with self.assertRaises(NotADirectoryError):
                TemplateRenderer(file_path)
            with self.assertRaises(FileNotFoundError):
                TemplateRenderer(Path(directory) / "missing")


if __name__ == "__main__":
    unittest.main()

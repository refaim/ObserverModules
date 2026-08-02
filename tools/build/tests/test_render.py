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

    def test_catch2_shards_share_a_short_base_without_changing_rendered_bytes(self) -> None:
        base = self.templates / "catch2-test.ps1"
        self.assertTrue(base.is_file(), "Catch2 shard recipes must share one inherited base")

        variables: dict[str, object] = {
            "name": "fixture",
            "pool": "slot",
            "inputs": ["build-tests"],
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
                "8e8be23fd290ead07b8a09d6ded0dcf6cd24c56cee6dc8b5f532bb06ad8581d4",
            ),
            "native-corpus-test.ps1": (
                variables,
                "525b016aaf8d444d342412bc5ac81221587d562f65ad895eba88891a2c80685a",
            ),
            "coverage-test.ps1": (
                variables,
                "65e2c48ac05288cf82e4691e3bd3eb5b745fef71dd2ff6212992a706012868d2",
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
                "6fac2869ac98cb3c3f3fb65f9b1e0856f41a2fabcd3e018f347ddb8e28d1455b",
            ),
            "sanitizer-test.ps1:ubsan": (
                variables
                | {
                    "runtime": None,
                    "options_name": "UBSAN_OPTIONS",
                    "options_value": "halt_on_error=1",
                },
                "c885f92dd881d55c2bd017bed98ded28e409cb2c3db1f386869ecfe789618ca6",
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

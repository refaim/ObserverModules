"""The clang-tidy gate keeps its exit verdict while publishing only real diagnostics."""

from __future__ import annotations

from pathlib import Path
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parents[2]
WORKFLOW = REPOSITORY / ".github" / "workflows" / "ci-check.yml"


def step_block(workflow: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    return marker + workflow.split(marker, 1)[1].split("\n      - name: ", 1)[0]


class ClangReportWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_capture_step_preserves_gate_failure_and_a_complete_log(self) -> None:
        step = step_block(self.workflow, "Verify clang-tidy and capture its report")
        body = textwrap.dedent(step.split("        run: |\n", 1)[1])
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "build.ps1").write_text(
                    f"Write-Output 'intentional fixture output'\nexit {exit_code}\n",
                    encoding="utf-8",
                )
                command = body.replace("${{ inputs.arch }}", "x64")
                completed = subprocess.run(
                    ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
                    cwd=root, capture_output=True, text=True, encoding="utf-8", check=False,
                )
                self.assertEqual(completed.returncode, exit_code)
                log = root / "out" / "native" / "analysis" / "clang-tidy-x64.log"
                self.assertTrue(log.is_file())
                self.assertIn("intentional fixture output", log.read_text(encoding="utf-8"))

    def test_report_toolchain_has_pinned_mature_tools_and_cache_validation(self) -> None:
        workflow = self.workflow
        self.assertIn("2d666fd9333e69a0c48b5d31a64bd360d5984c20", workflow)
        self.assertIn("2da736ff05ef065cb2894ac6892e47b5eac2c3c0", workflow)
        self.assertIn("Python 3.14.8", workflow)
        self.assertIn("lxml==6.1.3", workflow)
        self.assertIn("PyYAML==6.0.3", workflow)
        self.assertIn("sarif-tools==3.0.5", workflow)
        self.assertIn("jsonpath-ng==1.10.1", workflow)
        self.assertIn("Sarif.Multitool --version 5.8.0", workflow)
        self.assertIn("actions/cache/restore@", workflow)
        self.assertIn("actions/cache/save@", workflow)
        self.assertIn("Verify the cached clang report tools", workflow)
        self.assertIn("globber.py", workflow)
        self.assertIn("clang-report-cache-v3-", workflow)
        self.assertIn("other/src/fixture.cpp", workflow)
        self.assertLess(
            workflow.index("- name: Prepare the pinned SARIF merger"),
            workflow.index("- name: Verify the cached clang report tools"),
        )

    def test_pinned_source_fetch_extracts_only_the_report_tools(self) -> None:
        step = step_block(self.workflow, "Provision the pinned clang report tools")
        script = textwrap.dedent(step.split("          @'\n", 1)[1].split("          '@", 1)[0])
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            for name in ("snapshot/tools/report-converter/setup.py", "snapshot/unrelated.txt"):
                payload = b"fixture"
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))
        cache_archive = io.BytesIO()
        with tarfile.open(fileobj=cache_archive, mode="w:gz") as bundle:
            for name in ("ctcache/pyproject.toml", "ctcache/src/ctcache/clang_tidy_cache.py"):
                payload = b"fixture"
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))
        calls: list[list[str]] = []

        def fake_gh(argv: list[str], *, stdout: object, check: bool) -> None:
            self.assertTrue(check)
            calls.append(argv)
            if "ctcache/tarball" in argv[-1]:
                stdout.write(cache_archive.getvalue())
            else:
                stdout.write(archive.getvalue() if "tarball" in argv[-1] else b"# pinned filter\n")

        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"UNPACKRAT_REPORT_TOOLS": temp}), patch(
                "subprocess.run", fake_gh
            ):
                exec(compile(script, "<pinned-report-source>", "exec"), {})
            root = Path(temp)
            self.assertEqual(len(calls), 4)
            self.assertIn("2d666fd9333e69a0c48b5d31a64bd360d5984c20", calls[0][-1])
            self.assertIn("2da736ff05ef065cb2894ac6892e47b5eac2c3c0", calls[1][-1])
            self.assertIn("2da736ff05ef065cb2894ac6892e47b5eac2c3c0", calls[2][-1])
            self.assertIn("857b14271d3ded5511c2e6a60f2dc20c1e4b5680", calls[3][-1])
            self.assertIn("Accept: application/vnd.github.raw+json", calls[1])
            self.assertTrue((root / "globber.py").is_file())
            self.assertTrue(Path((root / "package-path.txt").read_text(encoding="utf-8")).is_dir())
            self.assertTrue(Path((root / "ctcache-package-path.txt").read_text(encoding="utf-8")).is_dir())
            self.assertFalse(list((root / "source").rglob("unrelated.txt")))

        invalid = io.BytesIO()
        with tarfile.open(fileobj=invalid, mode="w:gz") as bundle:
            info = tarfile.TarInfo("snapshot/unrelated.txt")
            info.size = 0
            bundle.addfile(info, io.BytesIO())

        def fake_missing_source(argv: list[str], *, stdout: object, check: bool) -> None:
            stdout.write(invalid.getvalue() if "tarball" in argv[-1] else b"# pinned filter\n")

        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"UNPACKRAT_REPORT_TOOLS": temp}), patch(
                "subprocess.run", fake_missing_source
            ), self.assertRaisesRegex(ValueError, "source is incomplete"):
                exec(compile(script, "<pinned-report-source>", "exec"), {})

        def fake_missing_cache(argv: list[str], *, stdout: object, check: bool) -> None:
            if "ctcache/tarball" in argv[-1]:
                stdout.write(invalid.getvalue())
            else:
                stdout.write(archive.getvalue() if "tarball" in argv[-1] else b"# pinned filter\n")

        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"UNPACKRAT_REPORT_TOOLS": temp}), patch(
                "subprocess.run", fake_missing_cache
            ), self.assertRaisesRegex(ValueError, "ctcache source is incomplete"):
                exec(compile(script, "<pinned-report-source>", "exec"), {})

    def test_cached_serializer_rejects_wrong_pins_and_malformed_clean_runs(self) -> None:
        step = step_block(self.workflow, "Verify the cached clang report tools")
        script = textwrap.dedent(step.split("          @'\n", 1)[1].split("          '@", 1)[0])
        names = (
            "codechecker_report_converter",
            "codechecker_report_converter.report",
            "codechecker_report_converter.report.parser",
            "codechecker_report_converter.report.parser.sarif",
        )
        modules = {name: types.ModuleType(name) for name in names}
        for module in modules.values():
            module.__path__ = []
        sarif = modules[names[-1]]
        for bad_pin, bad_document in ((False, False), (True, False), (False, True)):
            with self.subTest(bad_pin=bad_pin, bad_document=bad_document):
                sarif.Parser = lambda: types.SimpleNamespace(convert=lambda _: {
                    "version": "2.1.0", "runs": [{"results": ["unexpected"] if bad_document else []}]
                })
                versions = {"lxml": "6.1.3", "PyYAML": "6.0.3", "sarif-tools": "3.0.5",
                            "jsonpath-ng": "1.10.1", "ctcache": "1.2.0+g857b142"}
                if bad_pin:
                    versions["sarif-tools"] = "0.0.0"
                with patch.dict(sys.modules, modules), patch(
                    "importlib.metadata.version", side_effect=versions.__getitem__
                ):
                    observed_error = None
                    try:
                        exec(compile(script, "<cached-report-validation>", "exec"), {})
                    except ValueError as error:
                        observed_error = error
                    self.assertEqual(observed_error is not None, bad_pin or bad_document)

    def test_clean_serializer_uses_the_official_api(self) -> None:
        step = step_block(self.workflow, "Convert the clang-tidy diagnostics to Code Scanning SARIF")
        script = textwrap.dedent(step.split("            @'\n", 1)[1].split("          '@", 1)[0])
        names = (
            "codechecker_report_converter",
            "codechecker_report_converter.report",
            "codechecker_report_converter.report.parser",
            "codechecker_report_converter.report.parser.sarif",
        )
        modules = {name: types.ModuleType(name) for name in names}
        for module in modules.values():
            module.__path__ = []
        modules[names[-1]].Parser = lambda: types.SimpleNamespace(
            convert=lambda findings: {"version": "2.1.0", "runs": [{"results": findings}]}
        )
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "clean.sarif"
            with patch.dict(sys.modules, modules), patch.dict(
                os.environ, {"UNPACKRAT_EMPTY_SARIF": str(report)}
            ):
                exec(compile(script, "<clean-report-serialization>", "exec"), {})
            self.assertEqual(report.read_text(encoding="utf-8"),
                             '{"version": "2.1.0", "runs": [{"results": []}]}')

    def test_clean_and_failed_logs_never_create_false_clean_baselines(self) -> None:
        workflow = self.workflow
        self.assertIn("& $converter -t clang-tidy -e sarif", workflow)
        self.assertIn("Parser().convert([])", workflow)
        self.assertIn("steps.tidy-gate.outcome", workflow)
        self.assertIn("--merge-empty-logs", workflow)
        self.assertIn("--normalize-for-ghas", workflow)
        self.assertIn("--base-path-token SRCROOT", workflow)
        self.assertIn("'-**:-warnings-as-errors'", workflow)
        self.assertIn("--split-lines", workflow)
        self.assertIn("category: clang-tidy-${{ inputs.arch }}", workflow)
        self.assertIn("sarif_file: out/native/analysis/clang-tidy-${{ inputs.arch }}.sarif", workflow)
        self.assertIn("hashFiles(format('out/native/analysis/clang-tidy-{0}.complete', inputs.arch))", workflow)
        self.assertNotIn("continue-on-error:", workflow)

        step = step_block(workflow, "Convert the clang-tidy diagnostics to Code Scanning SARIF")
        body = textwrap.dedent(step.split("        run: |\n", 1)[1])
        body = body.replace("& $converter", "& FakeConverter")
        body = body.replace("& $sarif", "& FakeSarif")
        body = body.replace("& $python $filter", "& FakeFilter")
        body = body.replace("& $python -", "& FakePython -")
        fakes = textwrap.dedent("""\
            function FakeConverter {
              $global:LASTEXITCODE = 0
              if ($env:FAKE_HAS_FINDING -eq 'true') {
                Set-Content -LiteralPath 'out/native/analysis/clang-tidy-x64-raw/diagnostic.sarif' -Value '{}'
              }
            }
            function FakePython {
              $global:LASTEXITCODE = 0
              Set-Content -LiteralPath $env:UNPACKRAT_EMPTY_SARIF -Value '{}'
              Set-Content -LiteralPath 'serializer-invoked' -Value 'yes'
            }
            function FakeSarif {
              $global:LASTEXITCODE = 0
              if ($args[0] -eq 'merge') {
                Set-Content -LiteralPath 'out/native/analysis/clang-tidy-x64-merged.sarif' -Value '{}'
              } elseif ($args[0] -eq 'rewrite') {
                Set-Content -LiteralPath 'out/native/analysis/clang-tidy-x64-github.sarif' -Value '{}'
              }
            }
            function FakeFilter {
              $global:LASTEXITCODE = 0
              Set-Content -LiteralPath 'out/native/analysis/clang-tidy-x64.sarif' -Value '{}'
            }
            """)
        for gate, finding, expected in (("success", False, 0), ("failure", False, 1),
                                        ("failure", True, 0)):
            with self.subTest(gate=gate, finding=finding), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                analysis = root / "out" / "native" / "analysis"
                analysis.mkdir(parents=True)
                (analysis / "clang-tidy-x64.log").write_text("fixture", encoding="utf-8")
                command = fakes + body.replace("${{ inputs.arch }}", "x64").replace(
                    "${{ steps.tidy-gate.outcome }}", gate
                )
                with patch.dict(os.environ, {"RUNNER_TEMP": temp, "GITHUB_WORKSPACE": temp,
                                          "FAKE_HAS_FINDING": str(finding).lower()}):
                    completed = subprocess.run(
                        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command], cwd=root,
                        capture_output=True, text=True, encoding="utf-8", check=False,
                    )
                self.assertEqual(completed.returncode, expected, completed.stderr)
                self.assertEqual((root / "serializer-invoked").exists(), gate == "success")
                self.assertEqual((analysis / "clang-tidy-x64.sarif").exists(), expected == 0)

    def test_report_setup_and_conversion_blocks_are_valid_powershell(self) -> None:
        for name in (
            "Provision the pinned clang report tools",
            "Verify the cached clang report tools",
            "Convert the clang-tidy diagnostics to Code Scanning SARIF",
        ):
            with self.subTest(name=name):
                step = step_block(self.workflow, name)
                body = textwrap.dedent(step.split("        run: |\n", 1)[1])
                completed = subprocess.run(
                    ["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                     "[scriptblock]::Create([Console]::In.ReadToEnd()) | Out-Null"],
                    input=body, capture_output=True, text=True, encoding="utf-8", check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()

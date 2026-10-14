"""The optional clang-tidy cache cannot hide diagnostics or replace the real analysis gate."""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci-check.yml"


class ClangCacheWorkflowTests(unittest.TestCase):
    def test_pinned_llvm_subset_contains_real_clang_cl(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("'bin/clang-cl.exe'", workflow)
        self.assertIn("$clangCl = Join-Path $root 'bin/clang-cl.exe'", workflow)
        self.assertIn("& $clangCl --version", workflow)
        self.assertIn("llvm-23.1.3-windows-x64-parallel-cache-v2", workflow)

    def test_local_success_only_cache_is_scoped_to_clang_tidy(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("857b14271d3ded5511c2e6a60f2dc20c1e4b5680", workflow)
        self.assertIn("CTCACHE_KEEP_COMMENTS", workflow)
        self.assertIn("CTCACHE_DIR", workflow)
        self.assertIn("UNPACKRAT_CLANG_TIDY_CACHE", workflow)
        self.assertIn("@echo off", workflow)
        self.assertIn("%ERRORLEVEL%", workflow)
        self.assertNotIn("CTCACHE_SAVE_OUTPUT", workflow)
        self.assertNotIn("CTCACHE_SAVE_ALL", workflow)
        self.assertNotIn("CTCACHE_IGNORE_OUTPUT", workflow)
        self.assertIn("Restore reusable clang-tidy cache", workflow)
        self.assertIn("Save reusable clang-tidy cache", workflow)
        self.assertIn("if: success() && inputs.stage == 'clang-tidy'", workflow)

    def test_namespace_reuses_unchanged_units_across_source_commits(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("hashFiles('src/**'", workflow)
        self.assertIn("${{ github.sha }}", workflow)
        self.assertIn("steps.msvc-toolset.outputs.version", workflow)
        self.assertIn("sdk10.0.26100.0", workflow)
        self.assertIn("runner-image.outputs.identity", workflow)
        self.assertIn("/clang:-frewrite-includes", workflow)
        self.assertNotIn("snapshot-with-nolint", workflow)
        self.assertIn("#define BAD2", workflow)
        self.assertIn("#define BAD3", workflow)
        self.assertIn("$independent = Join-Path $fixture 'independent.cpp'", workflow)
        self.assertIn("$changedSource = Invoke-TidyProbe $source", workflow)
        self.assertIn("$unaffectedAfterSource = Invoke-TidyProbe $independent", workflow)
        self.assertIn("$unaffectedAfterHeader = Invoke-TidyProbe $independent", workflow)
        self.assertIn("$unaffectedAfterNolint = Invoke-TidyProbe $independent", workflow)

    def test_report_fixture_runs_native_context_through_the_production_bootstrap(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        marker = "          # Exercise native diagnostic newlines through the production runner entry point.\n"
        self.assertIn(marker, workflow)
        block = workflow.split(marker, 1)[1]
        code = textwrap.dedent(block.split("          @'\n", 1)[1].split("          '@", 1)[0])
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "space ü fixture"
            fixture.mkdir()
            source = fixture / "space ü" / "fixture.cpp"
            other = fixture / "other" / "fixture.cpp"
            completed = subprocess.run([sys.executable, "-c", code, str(fixture), str(source), str(other)],
                                       cwd=WORKFLOW.parents[2], text=True, capture_output=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            output = (fixture / "diagnostic.log").read_bytes()
            self.assertNotIn(b"\r\r\n", output)
            self.assertEqual(output.count(b": error:"), 2)
            self.assertEqual(output.count(b": note:"), 2)
            self.assertEqual(output.count(b"  return 0;\r\n  ^\r\n"), 4)
            self.assertIn(str(source).encode("utf-8"), output)
            self.assertIn(str(other).encode("utf-8"), output)
        self.assertIn("_CLANG_TIDY_RUNNER_BOOTSTRAP", code)
        self.assertIn("completed.returncode != 7", code)
        self.assertIn("$findings.Count -ne 2", block)
        self.assertIn("$first[0].locations[0].physicalLocation.region.startColumn -ne 3", block)
        self.assertIn("$second[0].locations[0].physicalLocation.region.startColumn -ne 5", block)

    def test_report_fixture_rejects_lost_verdict_or_changed_native_bytes(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        marker = "          # Exercise native diagnostic newlines through the production runner entry point.\n"
        block = workflow.split(marker, 1)[1]
        code = textwrap.dedent(block.split("          @'\n", 1)[1].split("          '@", 1)[0])
        imported = "from main import _CLANG_TIDY_RUNNER_BOOTSTRAP"
        for bootstrap in ("raise SystemExit(0)", "print('changed');raise SystemExit(7)"):
            with self.subTest(bootstrap=bootstrap), tempfile.TemporaryDirectory() as directory:
                instrumented = code.replace(imported, imported +
                                            f"\n_CLANG_TIDY_RUNNER_BOOTSTRAP = {bootstrap!r}")
                fixture = Path(directory)
                completed = subprocess.run([sys.executable, "-c", instrumented, directory,
                                            str(fixture / "first.cpp"), str(fixture / "second.cpp")],
                                           cwd=WORKFLOW.parents[2], text=True, capture_output=True,
                                           check=False)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("changed native diagnostic bytes or exit status", completed.stderr)
                self.assertFalse((fixture / "diagnostic.log").exists())

    def test_real_cache_smoke_runs_before_project_gate(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Verify pinned clang-tidy cache behavior", workflow)
        self.assertLess(workflow.index("- name: Verify pinned clang-tidy cache behavior"),
                        workflow.index("- name: Verify clang-tidy and capture its report"))
        self.assertIn("clang-tidy-cache-fixture", workflow)
        self.assertIn("header", workflow)
        self.assertIn("-warnings-as-errors=*", workflow)
        self.assertIn("-Qunused-arguments", workflow)
        self.assertIn("UNPACKRAT_CLANG_TIDY_CACHE=$bridge", workflow)

    def test_extracted_cache_smoke_rejects_a_reused_bad_finding(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        marker = "      - name: Verify pinned clang-tidy cache behavior\n"
        step = marker + workflow.split(marker, 1)[1].split("\n      - name: ", 1)[0]
        body = textwrap.dedent(step.split("        run: |\n", 1)[1])
        body = body.replace("& $cache", "& FakeCache").replace("& $bridge", "& FakeBridge")
        fakes = textwrap.dedent("""\
            function FakeCache {
              if ($args[0] -eq '--zero-stats') {
                $global:hit = 0; $global:miss = 0; $global:cached = 0; $global:calls = 0
              } else {
                @{ local = @{hit_count=$global:hit; miss_count=$global:miss;
                             cached_count=$global:cached} } | ConvertTo-Json -Compress
              }
              $global:LASTEXITCODE = 0
            }
            function FakeBridge {
              $global:calls++
              if ($args -contains '--' -or $args[0] -ne '-p' -or
                  $args[1] -ne $fixture -or $args.Count -ne 5 -or
                  $args[3] -ne "-config-file=$config" -or
                  $args[4] -ne '-warnings-as-errors=*') {
                throw 'Cache probe must use -p before the filename and only compile-database flags.'
              }
              $file = [string]$args[2]
              $requiredFilter = if ($global:calls -le 8) { "HeaderFilterRegex: '^$'" }
                                else { "HeaderFilterRegex: '.*'" }
              if ((Get-Content -LiteralPath $config) -notcontains $requiredFilter) {
                throw 'Config invalidation must change an explicit effective header filter.'
              }
              $database = @(Get-Content -LiteralPath (Join-Path $fixture 'compile_commands.json') -Raw |
                            ConvertFrom-Json)
              $compiler = Join-Path $env:RUNNER_TEMP 'unpackrat/tools/llvm/23.1.3/bin/clang-cl.exe'
              if ($database.Count -ne 3) { throw 'Expected all three fixture compile commands.' }
              foreach ($entry in $database) {
                $expected = '"{0}" /c /std:c++23 -Qunused-arguments /clang:-frewrite-includes /I"{1}" "{2}"' -f
                            $compiler, $fixture, $entry.file
                if ($entry.file -eq $source -and $global:calls -ge 11) { $expected += ' /DCHANGED=1' }
                if ($entry.directory -ne $fixture -or $entry.command -cne $expected -or
                    $entry.file -notin @($source, $independent, $bad) -or
                    -not [IO.Path]::IsPathFullyQualified($entry.file)) {
                  throw 'Compile database must use the pinned clang-cl and change only source A flags.'
                }
              }
              if ($global:calls -eq 1) { $global:dataDir = $env:CTCACHE_DIR }
              if ($env:CTCACHE_DIR -ne $global:dataDir) {
                $global:LASTEXITCODE = 91; return
              }
              if ($global:calls -in @(2,4,6,8,10,16) -and
                  (Split-Path -Leaf $file) -ne 'independent.cpp') {
                $global:LASTEXITCODE = 92; return
              }
              if ($global:calls -in @(14,15) -and
                  (Get-Content -LiteralPath (Join-Path (Split-Path $file) 'header.h') -Raw) -match 'NOLINT') {
                $global:LASTEXITCODE = 93; return
              }
              if ($global:calls -in @(3,4,6,8,16) -or
                  ($env:FAKE_CONFIG_FAILURE -eq 'true' -and $global:calls -eq 9) -or
                  ($env:FAKE_REUSE_BAD -eq 'true' -and $global:calls -eq 15)) {
                $global:hit++
              } else {
                $global:miss++
                if ($global:calls -in @(1,2,5,7,9,10,11)) { $global:cached++ }
              }
              if ($global:calls -in @(12,13,14,15) -and
                  -not ($env:FAKE_REUSE_BAD -eq 'true' -and $global:calls -eq 15)) {
                $global:LASTEXITCODE = 7
                if ($global:calls -in @(14,15)) {
                  Write-Output 'bugprone-macro-parentheses'
                } else {
                  Write-Output 'bugprone-sizeof-expression'
                }
              } else {
                $global:LASTEXITCODE = 0
                if ($env:FAKE_CONFIG_FAILURE -eq 'true' -and $global:calls -in @(9,10)) {
                  Write-Output "config-output-call-$($global:calls)"
                }
              }
            }
            """)
        for reuse_bad, config_failure, expected in ((False, False, 0), (True, False, 1),
                                                     (False, True, 1)):
            with self.subTest(reuse_bad=reuse_bad, config_failure=config_failure), tempfile.TemporaryDirectory() as temp:
                with patch.dict(os.environ, {"RUNNER_TEMP": temp,
                                          "FAKE_REUSE_BAD": str(reuse_bad).lower(),
                                          "FAKE_CONFIG_FAILURE": str(config_failure).lower()}):
                    completed = subprocess.run(
                        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", fakes + body],
                        cwd=temp, capture_output=True, text=True, encoding="utf-8", check=False,
                    )
                self.assertEqual(completed.returncode, expected, completed.stderr)
                if config_failure:
                    for detail in ("SourceCode=0", "IndependentCode=0", '"hit_count":5',
                                   '"miss_count":5', '"cached_count":5',
                                   "config-output-call-9", "config-output-call-10"):
                        self.assertIn(detail, completed.stderr)
                elif reuse_bad:
                    self.assertIn("ctcache reused a clean NOLINT result or cached a finding", completed.stderr)
                else:
                    self.assertIn("ctcache fixture:", completed.stdout)

    def test_nolint_edit_preserves_all_other_header_bytes(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        edit = workflow.split("          # Preserve the same cache while changing only an included header's NOLINT comments.\n", 1)[1]
        edit = textwrap.dedent(edit.split("          $unsuppressedFirst =", 1)[0])
        annotation = " // NOLINT(bugprone-macro-parentheses)"
        before = ("inline int value() { return 1; }\r\n"
                  f"#define BAD2 1+2{annotation}\r\n"
                  f"#define BAD3(A) (A+1){annotation}\r\n"
                  "// changed header comment — preserved\r\n").encode("utf-8")
        with tempfile.TemporaryDirectory() as temp:
            header = Path(temp) / "header.h"
            header.write_bytes(before)
            with patch.dict(os.environ, {"FIXTURE_HEADER": str(header)}):
                completed = subprocess.run(
                    ["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                     "$header = $env:FIXTURE_HEADER\n" + edit],
                    capture_output=True, text=True, encoding="utf-8", check=False,
                )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(header.read_bytes(), before.replace(annotation.encode("utf-8"), b""))


if __name__ == "__main__":
    unittest.main()

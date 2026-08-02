from __future__ import annotations

from pathlib import Path
import re
import unittest


REPOSITORY = Path(__file__).parents[2]


class WorkflowContractTests(unittest.TestCase):
    def test_main_ci_is_only_an_automatic_thin_public_verify_client(self) -> None:
        workflow = (REPOSITORY / ".github/workflows/main.yml").read_text(encoding="utf-8")

        self.assertIn("pull_request:", workflow)
        self.assertIn("push:", workflow)
        self.assertGreaterEqual(workflow.count("branches: [master]"), 2)
        self.assertNotIn("workflow_dispatch", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", workflow)

        self.assertIn("runs-on: windows-2022", workflow)
        self.assertIn("fail-fast: false", workflow)
        matrix_rows = re.findall(r"- job: ([^\n]+)\n\s+arch: ([^\n]+)", workflow)
        self.assertEqual(
            matrix_rows,
            [("source", "none"), ("x86", "x86"), ("x64", "x64"), ("arm64-cross", "arm64")],
        )
        self.assertEqual(workflow.count("./build.ps1 verify-source"), 1)
        self.assertEqual(workflow.count("./build.ps1 verify-arch"), 1)
        self.assertNotIn("./build.ps1 verify -Arch", workflow)
        self.assertNotIn("continue-on-error", workflow)

        self.assertIn("upload-artifact", workflow)
        self.assertIn("id: verify-source", workflow)
        self.assertIn("id: verify-arch", workflow)
        self.assertIn(
            "if: always() && (steps.verify-source.outcome != 'skipped' || "
            "steps.verify-arch.outcome != 'skipped')",
            workflow,
        )
        self.assertIn("/manifest.json", workflow)
        self.assertIn("/reports", workflow)
        self.assertIn("/logs", workflow)
        self.assertNotIn("!${{ runner.temp }}", workflow)
        self.assertIn("if-no-files-found: error", workflow)
        self.assertIn(
            "if: success() && github.event_name == 'push' && matrix.job != 'source'",
            workflow,
        )
        self.assertIn("/packages", workflow)
        self.assertNotIn("if-no-files-found: ignore", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertNotIn("out/cas", workflow)
        self.assertNotIn("out/work", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("download-artifact", workflow)
        self.assertNotIn("upload-sarif", workflow)
        self.assertNotIn("codeql-action", workflow)

        actions = dict(re.findall(r"uses:\s+([^@\s]+)@([^\s#]+)", workflow))
        self.assertEqual(
            actions,
            {
                "actions/checkout": "d23441a48e516b6c34aea4fa41551a30e30af803",
                "astral-sh/setup-uv": "08807647e7069bb48b6ef5acd8ec9567f424441b",
                "actions/cache": "caa296126883cff596d87d8935842f9db880ef25",
                "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            },
        )
        for action, revision in actions.items():
            with self.subTest(action=action):
                self.assertRegex(revision, r"^[0-9a-f]{40}$")
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn('version: "0.12.1"', workflow)
        self.assertIn("id: runner-image", workflow)
        self.assertIn("${{ steps.runner-image.outputs.identity }}", workflow)
        self.assertIn('"TEMP=$env:RUNNER_TEMP" | Add-Content -Path $env:GITHUB_ENV', workflow)
        self.assertIn('"TMP=$env:RUNNER_TEMP" | Add-Content -Path $env:GITHUB_ENV', workflow)
        self.assertIn(
            "$baseline = (Get-Content -Raw -LiteralPath 'vcpkg.json' | "
            "ConvertFrom-Json).'builtin-baseline'",
            workflow,
        )
        self.assertIn(
            "git -C $env:VCPKG_ROOT fetch --no-tags --depth=1 origin $baseline",
            workflow,
        )
        self.assertIn("checkout --detach --force $baseline", workflow)
        self.assertIn("bootstrap-vcpkg.bat", workflow)
        self.assertIn("C:\\Program Files\\Cppcheck", workflow)
        self.assertIn("nuget install Microsoft.CodeAnalysis.BinSkim", workflow)
        self.assertIn("tools\\net9.0\\win-x64\\BinSkim.exe", workflow)
        self.assertNotIn("dotnet tool install --global Microsoft.CodeAnalysis.BinSkim", workflow)
        self.assertIn("$env:GITHUB_PATH", workflow)
        self.assertIn("if: matrix.job == 'x64'", workflow)
        self.assertIn(
            "https://download.microsoft.com/download/e119c04b-71aa-4067-ac3c-360c2e13d209/"
            "windowssdk/Installers/X64%20Debuggers%20And%20Tools-x64_en-us.msi",
            workflow,
        )
        self.assertIn("354173D844D5C061050EE2638AA94FAFB4835AC3DE836E220F6A74A992849A3B", workflow)
        self.assertIn(
            '$process = Start-Process -FilePath "$env:SystemRoot\\System32\\msiexec.exe"',
            workflow,
        )
        self.assertNotIn("'/layout'", workflow)
        self.assertNotIn("'/installpath'", workflow)
        self.assertIn("'10.0.19041.'", workflow)
        self.assertIn("$gflags = Join-Path $debuggers 'gflags.exe'", workflow)
        self.assertIn('"OBSERVER_UMDH=$umdh" | Add-Content -Path $env:GITHUB_ENV', workflow)

        for duplicated_gate in (
            "./build.ps1 source-checks",
            "./build.ps1 compiler-analysis",
            "./build.ps1 test-coverage",
            "./build.ps1 test-asan",
            "./build.ps1 test-ubsan",
            "./build.ps1 test-leaks",
            "./build.ps1 fuzz",
            "./build.ps1 audit-binaries",
            "./build.ps1 package",
        ):
            self.assertNotIn(duplicated_gate, workflow)


if __name__ == "__main__":
    unittest.main()

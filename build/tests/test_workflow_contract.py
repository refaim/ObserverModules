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
        self.assertIn("if: always()", workflow)
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
        self.assertIn("C:\\Program Files\\Cppcheck", workflow)
        self.assertIn("$env:GITHUB_PATH", workflow)

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

from __future__ import annotations

from pathlib import Path
import unittest


REPOSITORY = Path(__file__).parents[2]


class WorkflowContractTests(unittest.TestCase):
    def test_main_ci_is_only_a_thin_public_verify_client(self) -> None:
        workflow = (REPOSITORY / ".github/workflows/main.yml").read_text(encoding="utf-8")

        self.assertEqual(workflow.count("runs-on:"), 1)
        self.assertEqual(workflow.count("./build.ps1 doctor"), 1)
        self.assertEqual(workflow.count("./build.ps1 verify"), 1)
        self.assertIn("./build.ps1 verify -Arch x64", workflow)
        self.assertNotIn("continue-on-error", workflow)
        for private_protocol in (
            "upload-artifact",
            "download-artifact",
            "upload-sarif",
            "test-reporter",
            "codeql-action",
            "action-gh-release",
            "contents: write",
        ):
            self.assertNotIn(private_protocol, workflow)
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

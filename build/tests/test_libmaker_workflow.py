"""Execute the shared CI LibMaker setup with fixture bytes and a fake gh command."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"
STEP = "Provision the pinned LibMaker tool"
SHA = "0aa2e9d937e9838d7d69c011897bf066a0cb44402eb562705179be74bf1c0fbd"
PAYLOAD = b"synthetic LibMaker fixture bytes"


class LibMakerWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (WORKFLOWS / "ci-check.yml").read_text(encoding="utf-8")
        self.assertTrue(f"- name: {STEP}" in self.workflow, "Shared LibMaker setup is missing")
        block = self.workflow.split(f"      - name: {STEP}\n", 1)[1].split("\n      - name: ", 1)[0]
        self.block = block
        self.script = textwrap.dedent(block.split("        run: |\n", 1)[1])

    def test_only_dependency_restoring_stages_get_the_shared_setup_before_gates(self):
        condition = re.search(r"if: (.+)", self.block).group(1)
        stages = json.loads(re.search(r"fromJSON\('([^']+)'\)", condition).group(1))
        self.assertEqual(set(stages), {"source", "native-debug", "native-release", "msvc-analysis",
                                       "clang-tidy", "coverage", "asan", "ubsan", "fuzz"})
        self.assertIn("inputs.stage", condition)
        self.assertIn("contains(", condition)
        self.assertEqual(self.workflow.count(f"- name: {STEP}"), 1)
        location = self.workflow.index(f"- name: {STEP}")
        self.assertLess(self.workflow.index("- name: Check out the repository"), location)
        for gate in ("Verify the repository sources", "Verify one stage for one architecture",
                     "Verify clang-tidy and capture its report"):
            self.assertLess(location, self.workflow.index(f"- name: {gate}"))
        for name in ("main.yml", "ci-instrumentation.yml"):
            self.assertNotIn("LibMaker", (WORKFLOWS / name).read_text(encoding="utf-8"))

    def test_release_bytes_are_pinned_and_verified_before_path_exposure(self):
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.block)
        self.assertIn("release download v1.0.4 --repo Chuyu-Team/LibMaker", self.script)
        self.assertIn("--pattern 'LibMaker.exe'", self.script)
        self.assertIn("--clobber", self.script)
        self.assertIn(SHA, self.script.lower())
        self.assertIn("236544", self.script)
        self.assertIn("-Algorithm SHA256", self.script)
        self.assertLess(self.script.index("Get-FileHash"), self.script.index("$env:GITHUB_PATH"))
        self.assertNotIn("Start-Process", self.script)
        self.assertNotIn("C:\\Tools", self.script)

    def _execute(self, *, payload=PAYLOAD, code=0, stale=False, missing=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "fixture.bin"
            fixture.write_bytes(payload)
            output = root / "github-path.txt"
            output.touch()
            directory = root / "unpackrat/tools/libmaker/1.0.4"
            if stale:
                directory.mkdir(parents=True)
                (directory / "LibMaker.exe").write_bytes(PAYLOAD)
            # Only the expected fixture data differs; the actual workflow logic runs unchanged.
            script = self.script.replace(SHA, hashlib.sha256(PAYLOAD).hexdigest()).replace("236544", str(len(PAYLOAD)))
            fake = textwrap.dedent("""\
                function gh {
                  @($args) | ConvertTo-Json -Compress | Set-Content -LiteralPath $env:FIXTURE_ARGS
                  if ($env:FIXTURE_EXIT -eq '0' -and $env:FIXTURE_MISSING -ne '1') {
                    $position = [Array]::IndexOf($args, '--dir')
                    Copy-Item -LiteralPath $env:FIXTURE_BYTES -Destination (Join-Path $args[$position + 1] 'LibMaker.exe') -Force
                  }
                  $global:LASTEXITCODE = [int]$env:FIXTURE_EXIT
                }
                """)
            completed = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command", fake + script],
                env={**os.environ, "RUNNER_TEMP": str(root), "GITHUB_PATH": str(output),
                     "FIXTURE_ARGS": str(root / "args.json"), "FIXTURE_BYTES": str(fixture),
                     "FIXTURE_EXIT": str(code), "FIXTURE_MISSING": str(int(missing)),
                     "GH_TOKEN": "fixture-not-a-secret"},
                capture_output=True, text=True, check=False,
            )
            return completed, output.read_text(), json.loads((root / "args.json").read_text()), directory

    def test_verified_fresh_and_preexisting_tools_are_exposed_only_after_download(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                completed, path, args, directory = self._execute(stale=stale)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertEqual(Path(path.strip()), directory)
                self.assertEqual(args, ["release", "download", "v1.0.4", "--repo", "Chuyu-Team/LibMaker",
                                        "--pattern", "LibMaker.exe", "--dir", str(directory), "--clobber"])

    def test_download_failures_cannot_accept_a_preexisting_verified_file(self):
        for code in (7, -1073741819):
            with self.subTest(code=code):
                completed, path, _, _ = self._execute(code=code, stale=True)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(path, "")
                self.assertIn("Unable to download the pinned LibMaker tool", completed.stdout + completed.stderr)

    def test_wrong_size_and_same_size_wrong_hash_are_rejected(self):
        for payload, diagnostic in ((PAYLOAD + b"!", "size"), (b"X" * len(PAYLOAD), "SHA-256")):
            with self.subTest(diagnostic=diagnostic):
                completed, path, _, _ = self._execute(payload=payload)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(path, "")
                self.assertIn(diagnostic, completed.stdout + completed.stderr)

    def test_successful_download_without_an_asset_fails_closed(self):
        completed, path, _, _ = self._execute(missing=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(path, "")


if __name__ == "__main__":
    unittest.main()

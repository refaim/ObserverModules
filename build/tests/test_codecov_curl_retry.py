"""The pinned Codecov action's retry is extended to transient curl connection failures.

curl with the Schannel backend aborted the CLI binary download with ``curl: (56)
connection closed abruptly`` before an upload began.  The pinned official
``codecov/codecov-action`` retries only the error classes its own wrapper covers, which
omits ``retry-all-errors``.  A CI-owned curl config under the runner temp tree adds that
single clause without touching the pinned action, its SHA pin, its GPG verification or its
``fail_ci_if_error`` policy, and it reaches only the two upload steps through a step-scoped
``CURL_HOME``.  These tests pin that contract and execute the harmless config fragment in a
fresh pwsh under a synthetic ``RUNNER_TEMP`` that only writes the temp config, never a
network call.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
CHECK = REPOSITORY / ".github" / "workflows" / "ci-check.yml"

CONFIG_STEP = "Prepare the Codecov curl retry configuration"
PYTHON_UPLOAD = "Upload the python coverage to Codecov"
NATIVE_UPLOAD = "Upload the native coverage to Codecov"
ACTION_PIN = "codecov/codecov-action@303a32d7a59b442fa8d48b6a1cc6825c09c847a5"


def step_block(workflow: str, name: str) -> str:
    """Select one step using the workflow's fixed six-space step indentation."""

    marker = f"      - name: {name}\n"
    return marker + workflow.split(marker, 1)[1].split("\n      - name: ", 1)[0]


def run_block(step: str) -> list[str]:
    """Dedent the known config step's literal PowerShell body."""

    return textwrap.dedent(step.split("        run: |\n", 1)[1]).splitlines()


class CodecovCurlRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.check = CHECK.read_text(encoding="utf-8")

    def test_the_workflow_owns_a_runner_temp_curl_config_with_the_retry_clause(self) -> None:
        step = step_block(self.check, CONFIG_STEP)
        # The config lives under the runner temp tree, never in the repository or a user profile.
        self.assertIn("$env:RUNNER_TEMP", step)
        self.assertIn("codecov-curl", step)
        self.assertIn(".curlrc", step)
        self.assertIn("retry-all-errors", step)

    def test_the_curl_config_step_runs_for_both_upload_stages_only(self) -> None:
        step = step_block(self.check, CONFIG_STEP)
        # Both coverage-producing stages publish a report, so both must see the config; the
        # build, analysis and instrumentation stages never upload to Codecov.
        self.assertIn("inputs.stage == 'coverage'", step)
        self.assertIn("inputs.stage == 'source'", step)
        for stage in (
            "native-debug", "native-release", "msvc-analysis", "clang-tidy",
            "asan", "ubsan", "fuzz", "leaks",
        ):
            with self.subTest(stage=stage):
                self.assertNotIn(stage, step)

    def test_both_uploads_scope_curl_home_to_the_config_directory(self) -> None:
        expected = "          CURL_HOME: ${{ runner.temp }}/codecov-curl"
        for name in (PYTHON_UPLOAD, NATIVE_UPLOAD):
            with self.subTest(step=name):
                self.assertIn(expected, step_block(self.check, name))
        # CURL_HOME is a step-scoped variable of exactly the two uploads: it never appears at
        # workflow scope and never at the job scope, where it would leak into every step.
        scoped = [line for line in self.check.splitlines() if "CURL_HOME" in line]
        self.assertEqual(scoped, [expected, expected])

    def test_the_pinned_action_and_its_integrity_policy_are_unchanged(self) -> None:
        # The wrapper keeps the pinned action, its hash pin and the fail-closed upload policy.
        self.assertEqual(self.check.count(ACTION_PIN), 2)
        self.assertEqual(self.check.count("disable_search: true"), 2)
        self.assertEqual(self.check.count("fail_ci_if_error: true"), 2)
        self.assertEqual(self.check.count("use_oidc:"), 2)
        # No weakened verification and no alternate package source anywhere in the workflow.
        for banned in ("skip_validation", "use_pypi"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, self.check)
        # The rest of CI legitimately fetches the signed Windows SDK payload with
        # Invoke-WebRequest, so the no-hand-rolled-network rule is scoped to the Codecov config
        # and the two upload steps instead of the whole workflow.
        codecov_steps = "\n".join(
            step_block(self.check, name) for name in (CONFIG_STEP, PYTHON_UPLOAD, NATIVE_UPLOAD)
        )
        for banned in ("Invoke-WebRequest", "Invoke-RestMethod", "curl.exe", "--retry"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, codecov_steps)

    def test_the_curl_config_step_precedes_every_coverage_gate_and_upload(self) -> None:
        config = self.check.index(CONFIG_STEP)
        for later in (
            "Verify the repository sources",
            "Verify one stage for one architecture",
            PYTHON_UPLOAD,
            NATIVE_UPLOAD,
        ):
            with self.subTest(later=later):
                self.assertGreater(self.check.index(later), config)

    def test_the_curl_config_fragment_writes_the_single_clause_under_runner_temp(self) -> None:
        block = run_block(step_block(self.check, CONFIG_STEP))
        with tempfile.TemporaryDirectory() as temp:
            quoted = temp.replace("'", "''")
            script = "\n".join(
                [
                    "$ErrorActionPreference = 'Stop'",
                    f"$env:RUNNER_TEMP = '{quoted}'",
                    *block,
                ]
            )
            subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            curlrc = Path(temp) / "codecov-curl" / ".curlrc"
            self.assertTrue(curlrc.is_file(), "the fragment did not write .curlrc")
            directives = curlrc.read_text(encoding="utf-8").splitlines()
            self.assertEqual(directives, ["retry-all-errors"])


if __name__ == "__main__":
    unittest.main()

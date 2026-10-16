"""The verified UBSan overflow probe must not fail the Verify step on its expected nonzero exit.

``probe.exe overflow`` deliberately overflows a signed 32-bit integer under
``-fno-sanitize-recover=all``, so the probe exits 1 and prints a UBSan runtime report.
``Assert-UbsanRuntimeDiagnosis`` accepts exactly that combination and returns normally, but the
residual ``$LASTEXITCODE`` 1 is still standing when GitHub's appended ``pwsh`` trailer runs
``exit $LASTEXITCODE``, so the step fails even though the diagnosis was validated.

These tests run the real Verify-step tail - the overflow invocation through to the end of the
step - under an emulated GitHub trailer.  The child exit code comes from a harmless
``cmd.exe /d /c exit N`` (no file-system work, no compiler), so the native ``$LASTEXITCODE`` the
tail observes is genuine and the constant diagnosis is written after it, leaving that exit code
standing.  The accepted nonzero exit must become a step success, and every exit the diagnosis
guard rejects must still fail before any success reset.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parent))

# Import the shared helpers and the step/function names, never the neighbouring TestCase
# classes, so ``unittest`` discovery does not collect that suite a second time.
from test_ubsan_runtime_provisioning import (  # noqa: E402
    DIAGNOSIS_FUNCTION,
    PREP_WORKFLOW,
    VERIFY_STEP,
    function_block,
    run_powershell,
    step_block,
)


# The exact first line of the Verify-step tail: the probe invocation the fix must follow.
OVERFLOW_INVOCATION = "$overflow = & $probeExe overflow"
# The trailer GitHub appends to every ``pwsh`` step, which turns the residual exit code into the
# step's own exit status.
GITHUB_TRAILER = "if (Test-Path -LiteralPath variable:\\LASTEXITCODE) { exit $LASTEXITCODE }"
VALID_DIAGNOSIS = "runtime error: signed integer overflow: 2147483647 + 1 cannot be represented"


def verify_tail(workflow: str) -> str:
    """Return the real Verify-step tail from the probe invocation to the end of the step."""

    lines = step_block(workflow, VERIFY_STEP).splitlines()
    for index, line in enumerate(lines):
        if OVERFLOW_INVOCATION in line:
            return "\n".join(lines[index:])
    raise AssertionError(f"the overflow probe invocation is missing from: {VERIFY_STEP}")


def _literal(text: str) -> str:
    """Render ``text`` as a single-quoted PowerShell literal."""

    return "'" + text.replace("'", "''") + "'"


def run_tail(
    diagnosis: str, tail: str, *, exit_code: int, output: str
) -> subprocess.CompletedProcess[str]:
    """Run the shipped tail under an emulated GitHub trailer with a controlled child exit.

    ``$probeExe`` is a harmless script block whose only actions are a native ``cmd.exe`` exit -
    which leaves a genuine ``$LASTEXITCODE`` - and a constant diagnosis write, so the tail runs
    exactly as the workflow ships it while the child exit code stays under test control.
    """

    script = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            diagnosis,
            "$probeExe = {",
            "  param([string] $mode)",
            f"  & cmd.exe /d /c exit {exit_code}",
            f"  Write-Output {_literal(output)}",
            "}",
            tail,
            GITHUB_TRAILER,
        ]
    )
    return run_powershell(script)


class ProbeExitTailTests(unittest.TestCase):
    """The shipped tail plus the emulated GitHub trailer decide the step's exit status."""

    # exit code, output, whether the diagnosis guard accepts it, expected stderr fragment.
    CASES = (
        (1, VALID_DIAGNOSIS, True, ""),
        (0, VALID_DIAGNOSIS, False, "exited successfully"),
        (1, "probe crashed", False, "without a UBSan diagnostic"),
        (1, "runtime error: load of misaligned address", False, "not a signed-overflow report"),
    )

    def setUp(self) -> None:
        self.workflow = PREP_WORKFLOW.read_text(encoding="utf-8")
        self.tail = verify_tail(self.workflow)
        self.diagnosis = function_block(self.workflow, DIAGNOSIS_FUNCTION)

    def _run(self, exit_code: int, output: str) -> subprocess.CompletedProcess[str]:
        return run_tail(self.diagnosis, self.tail, exit_code=exit_code, output=output)

    def test_an_accepted_nonzero_overflow_exit_ends_the_step_successfully(self) -> None:
        # The regression: the accepted probe exit 1 must become a step success rather than the
        # step failure the residual $LASTEXITCODE the diagnosis guard left behind would cause.
        result = self._run(1, VALID_DIAGNOSIS)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_rejected_exit_still_fails_before_any_success_reset(self) -> None:
        for exit_code, output, accepted, fragment in self.CASES:
            with self.subTest(exit_code=exit_code, output=output):
                result = self._run(exit_code, output)
                if accepted:
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0, "the step must fail: " + output)
                    self.assertIn(fragment, result.stderr)


class TailExtractionTests(unittest.TestCase):
    """The helper must hand back the shipped tail, and fail loudly when it is absent."""

    def setUp(self) -> None:
        self.workflow = PREP_WORKFLOW.read_text(encoding="utf-8")

    def test_the_tail_starts_at_the_overflow_invocation_and_ends_the_step(self) -> None:
        tail = verify_tail(self.workflow)
        self.assertTrue(tail.lstrip().startswith(OVERFLOW_INVOCATION))
        self.assertIn(f"{DIAGNOSIS_FUNCTION} -ExitCode $LASTEXITCODE -Output $overflow", tail)
        # The tail stops at the end of the Verify step and never reaches a later step.
        self.assertNotIn("- name: ", tail)

    def test_a_workflow_without_the_overflow_invocation_fails_loudly(self) -> None:
        stub = "jobs:\n  x:\n    steps:\n      - name: " + VERIFY_STEP + "\n        run: echo hi\n"
        with self.assertRaises(AssertionError):
            verify_tail(stub)

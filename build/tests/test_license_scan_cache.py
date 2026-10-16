"""Composition contracts for advertising a reusable native ScanCode cache.

The reusable Linux license-audit job builds a native ScanCode scanner cache as a side
effect of the ``scan`` stage, and only the scanner invocation emits the readiness flag:
the audit writes ``scan-cache-ready=true`` to the step's own ``GITHUB_OUTPUT`` file only
after that stage exits zero *after* ``require_result`` has confirmed it wrote its output,
so an analyze, evaluate or reporter failure never advertises the scanner cache.  A later
policy failure (the evaluator or the reporter) does not remove a scanner cache that was
already produced and validated, so the cache-save step keys on that exact flag with
``always()`` instead of the coarse job ``success()``.

These checks are composition contracts over the workflow's own declarations, reusing
``step_block`` and ``step_condition`` from ``test_license_audit_workflow``: they read the
real YAML and check the declared step id, the readiness write's placement after the
validated exit and its scan-only guard, and the exact save condition.  Nothing here
re-implements GitHub's expression evaluator or a Bash parser.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from build.tests.test_license_audit_workflow import step_block, step_condition  # noqa: E402


AUDIT = REPOSITORY / ".github" / "workflows" / "license-audit.yml"

AUDIT_STEP = "Run the pinned ORT audit"
SAVE_STEP = "Save the ORT native file cache"

# The one line the audit uses to advertise a validated scan to later steps.  Quote and
# brace forms are tolerated so the contract is about the emitted value, not cosmetics.
READY_WRITE = re.compile(
    r"""^\s*echo\s+["']scan-cache-ready=true["']\s*>>\s*["']?\$\{?GITHUB_OUTPUT\}?["']?\s*$"""
)
SCAN_STAGE = re.compile(r"\bstage\s+scan\b")
REQUIRE_CALL = re.compile(r'require_result\s+"\$\{name\}"')
BRANCH_OPEN = re.compile(r"^\s*if\b")
BRANCH_CLOSE = re.compile(r"^\s*fi\s*$")


def _first_index(lines, pattern):
    for index, line in enumerate(lines):
        if pattern.search(line):
            return index
    return None


class ScanCacheReadinessOutput(unittest.TestCase):
    """The audit advertises a validated scan and the save step consumes it exactly."""

    def setUp(self) -> None:
        self.audit = AUDIT.read_text(encoding="utf-8")
        self.step = step_block(self.audit, AUDIT_STEP)
        self.lines = self.step.splitlines()

    def test_the_audit_step_declares_the_ort_audit_id(self) -> None:
        self.assertRegex(self.step, r"(?m)^\s*id:\s*ort-audit\s*$")

    def test_the_scan_stage_emits_the_readiness_output_after_its_validated_exit(self) -> None:
        scan_stage = _first_index(self.lines, SCAN_STAGE)
        self.assertIsNotNone(scan_stage, "the scan stage invocation must be declared")
        require = _first_index(self.lines, REQUIRE_CALL)
        self.assertIsNotNone(require, "the validated-exit helper call must be declared")
        ready = _first_index(self.lines, READY_WRITE)
        self.assertIsNotNone(ready, "the scan-cache-ready output write must be declared")
        self.assertEqual(
            sum(1 for line in self.lines if READY_WRITE.search(line)),
            1,
            "the readiness flag must be emitted exactly once",
        )
        self.assertGreater(ready, require)

    def test_the_readiness_output_is_inside_the_scan_only_branch(self) -> None:
        ready = _first_index(self.lines, READY_WRITE)
        self.assertIsNotNone(ready, "the scan-cache-ready output write must be declared")
        guard = None
        for index in range(ready - 1, -1, -1):
            if BRANCH_OPEN.search(self.lines[index]):
                guard = index
                break
        self.assertIsNotNone(guard, "the readiness write must sit inside an if branch")
        # The guard must be the exact scan-only, validated-exit branch: only the scanner
        # invocation, and only after require_result confirmed a zero exit, may advertise.
        # Pinning the whole literal rejects any weaker condition, such as a bare zero
        # check or a non-scan name test, that would advertise the cache unsafely.
        self.assertEqual(
            self.lines[guard].strip(),
            'if [ "${name}" = scan ] && [ "${code}" -eq 0 ]; then',
        )
        self.assertTrue(
            any(BRANCH_CLOSE.search(line) for line in self.lines[ready + 1:]),
            "the scan-only branch must be closed after the readiness write",
        )

    def test_the_cache_save_step_consumes_the_exact_readiness_output(self) -> None:
        save = step_block(self.audit, SAVE_STEP)
        self.assertEqual(
            step_condition(save),
            "if: always() && steps.ort-audit.outputs.scan-cache-ready == 'true'",
        )


if __name__ == "__main__":
    unittest.main()

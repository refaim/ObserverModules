"""The two monolithic native jobs are split into eight real required stage checks.

A native build no longer bundles the Debug suite, the Release suite and its binary
audits, the MSVC static analyzer and clang-tidy into one ``native-<arch>`` job.  Each
(stage, architecture) slice is its own required check whose real driver exit code is the
verdict: ``native-debug-*`` builds and runs the full Debug suite plus the fatal cleanup
probe, ``native-release-*`` builds, tests and audits the Release output (and, for x64,
builds the leak probe), ``msvc-analysis-*`` runs the MSVC static analyzer and
``clang-tidy-*`` runs the pinned clang-tidy.  The four stages are independent; only the
leak measurement consumes a native Release artifact, and packaging fans every real check
back in.

These are composition contracts over the workflows' own declarations -- the job set, the
reusable-call inputs, the ``needs`` edges and the stage gates on provisioning steps --
with no reimplementation of GitHub's expression evaluator.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
WORKFLOWS = REPOSITORY / ".github" / "workflows"
MAIN = WORKFLOWS / "main.yml"
CHECK = WORKFLOWS / "ci-check.yml"

# name -> (stage, architecture)
NEW_JOBS = {
    "native-debug-x86": ("native-debug", "x86"),
    "native-debug-x64": ("native-debug", "x64"),
    "native-release-x86": ("native-release", "x86"),
    "native-release-x64": ("native-release", "x64"),
    "msvc-analysis-x86": ("msvc-analysis", "x86"),
    "msvc-analysis-x64": ("msvc-analysis", "x64"),
    "clang-tidy-x86": ("clang-tidy", "x86"),
    "clang-tidy-x64": ("clang-tidy", "x64"),
}
BUILD_STAGES = {"native-debug", "native-release"}
ANALYSIS_STAGES = {"msvc-analysis", "clang-tidy"}

REUSABLE_CALL = "uses: ./.github/workflows/ci-check.yml"


def job_blocks(workflow: str) -> dict[str, list[str]]:
    _, _, jobs = workflow.partition("\njobs:\n")
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in jobs.splitlines():
        job = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if job:
            current = job.group(1)
            blocks[current] = []
            continue
        if current is not None:
            blocks[current].append(line)
    return blocks


def job_needs(block: list[str]) -> list[str]:
    for line in block:
        needs = re.match(r"^    needs:\s*(.+?)\s*$", line)
        if needs:
            value = needs.group(1)
            if value.startswith("["):
                return [name.strip() for name in value.strip("[]").split(",") if name.strip()]
            return [value]
    return []


def step_block(workflow: str, name: str) -> str:
    """Return the line block of one ``- name:`` step, leading indentation included."""

    lines = workflow.splitlines()
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped != f"- name: {name}":
            continue
        indent = line[: len(line) - len(stripped)]
        block = [line]
        for follow in lines[index + 1:]:
            if follow.startswith(f"{indent}- name: "):
                break
            block.append(follow)
        return "\n".join(block)
    raise AssertionError(f"workflow step not found: {name}")


def stage_selector(step: str) -> set[str]:
    """The stage set of a ``contains(fromJSON('[...]'), inputs.stage)`` gate."""

    match = re.search(r"contains\(fromJSON\('(\[[^\]]*\])'\),\s*inputs\.stage\)", step)
    if match is None:
        raise AssertionError(f"no stage selector in step:\n{step}")
    return set(json.loads(match.group(1)))


def step_condition(step: str) -> str:
    return "\n".join(line.strip() for line in step.splitlines() if line.strip().startswith("if:"))


class NativeJobDecompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.main = MAIN.read_text(encoding="utf-8")
        self.check = CHECK.read_text(encoding="utf-8")
        self.blocks = job_blocks(self.main)

    def test_the_two_native_jobs_are_replaced_by_eight_real_required_jobs(self) -> None:
        for name in NEW_JOBS:
            with self.subTest(job=name):
                self.assertIn(name, self.blocks)
        for removed in ("native", "native-x86", "native-x64"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, self.blocks)

    def test_every_new_job_forwards_its_stage_and_architecture_to_the_reusable_workflow(self) -> None:
        for name, (stage, arch) in NEW_JOBS.items():
            with self.subTest(job=name):
                block = "\n".join(self.blocks[name])
                self.assertIn(REUSABLE_CALL, block)
                self.assertIn(f"      stage: {stage}", block)
                self.assertIn(f"      arch: {arch}", block)

    def test_the_four_new_stages_are_independent_real_checks(self) -> None:
        for name in NEW_JOBS:
            with self.subTest(job=name):
                self.assertEqual(job_needs(self.blocks[name]), [])
        # No aggregate or always-success wrapper re-scores the real stage verdicts.
        self.assertNotIn("if: always()", self.main)
        self.assertNotIn("success()", self.main)
        self.assertNotIn("needs.*.result", self.main)

    def test_packaging_waits_on_all_eight_new_jobs(self) -> None:
        needs = job_needs(self.blocks["packaging"])
        for name in NEW_JOBS:
            with self.subTest(job=name):
                self.assertIn(name, needs)

    def test_the_msvc_and_spectre_preflight_covers_every_new_stage(self) -> None:
        step = step_block(self.check, "Verify the legacy MSVC toolset and Spectre libraries")
        selected = stage_selector(step)
        for stage in (*BUILD_STAGES, *ANALYSIS_STAGES):
            with self.subTest(stage=stage):
                self.assertIn(stage, selected)
        self.assertNotIn("leaks", selected)

    def test_only_source_and_clang_tidy_provision_the_llvm_23_analysis_tools(self) -> None:
        for name in (
            "Restore the pinned LLVM tools",
            "Provision the pinned LLVM 23.1.3 tools",
            "Verify pinned clang-tidy and system-header skipping",
            "Save the pinned LLVM tools",
        ):
            with self.subTest(step=name):
                condition = step_condition(step_block(self.check, name))
                self.assertIn("'clang-tidy'", condition)
                self.assertIn("'source'", condition)
                for stage in (*BUILD_STAGES, "msvc-analysis"):
                    self.assertNotIn(stage, condition)

    def test_binary_audit_tools_are_provisioned_only_for_native_release(self) -> None:
        condition = step_condition(step_block(self.check, "Provision binary-analysis tools"))
        self.assertIn("native-release", condition)
        for stage in ("native-debug", "msvc-analysis", "clang-tidy"):
            with self.subTest(stage=stage):
                self.assertNotIn(stage, condition)

    def test_the_llvm_19_instrumentation_stages_are_unchanged(self) -> None:
        download = stage_selector(step_block(self.check, "Collect the instrumentation toolchain"))
        self.assertEqual(download, {"coverage", "ubsan", "fuzz"})

    def test_the_native_release_artifact_feeds_the_leak_probe_and_packaging(self) -> None:
        upload = step_block(self.check, "Upload the Release binaries")
        self.assertEqual(step_condition(upload), "if: inputs.stage == 'native-release'")
        self.assertIn("name: release-${{ inputs.arch }}", upload)
        self.assertEqual(job_needs(self.blocks["leaks-x64"]), ["native-release-x64"])
        collect = step_block(self.check, "Collect the verified x64 Release binaries")
        self.assertIn("name: release-x64", collect)
        self.assertIn("path: out/native/bin/x64/Release", collect)


if __name__ == "__main__":
    unittest.main()

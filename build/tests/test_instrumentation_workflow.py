"""One real provisioning job publishes the matched LLVM 19.1.5 instrumentation toolchain.

The VS2026 image bundles LLVM 22.1.3, which ships no i386 runtime and no static ASan
libraries, so it cannot drive the coverage, UBSan or fuzz stages.  The ASan slices are
unaffected: they are the MSVC 14.44 dynamic AddressSanitizer build and use no LLVM toolchain.
A single real ``instrumentation-tools`` job downloads the exact official Microsoft payloads,
hash-verifies and extracts them, verifies the 19.1.5 versions and the runtime libraries for
both architectures, and publishes the result as a same-run artifact.  The instrumentation
slices download that same-run artifact and select it through
``UNPACKRAT_LLVM_INSTRUMENTATION``; nothing reaches a cross-run stash, and no aggregate gate
re-scores the results.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
WORKFLOWS = REPOSITORY / ".github" / "workflows"
PREP_WORKFLOW = WORKFLOWS / "ci-instrumentation.yml"
ARTIFACT = "llvm-instrumentation-19.1.5"

BASE_URL = (
    "https://download.visualstudio.microsoft.com/download/pr/"
    "58af20f6-56e8-48a0-86d3-115fb110097b/"
    "6a70f2b5677302799b5220afd03d062af72ad8ca869e4537465f8a687810cfa2/payload.vsix"
)
BASE_SHA = "6a70f2b5677302799b5220afd03d062af72ad8ca869e4537465f8a687810cfa2"
BASE_SIZE = "62004420"
CLANG_URL = (
    "https://download.visualstudio.microsoft.com/download/pr/"
    "e98d75fa-91b1-47a1-9cb7-b6556de592c5/"
    "3f4c2578467784756f3706848d8ebec3f0b1f6705c062b2be769f4138534115f/payload.vsix"
)
CLANG_SHA = "3f4c2578467784756f3706848d8ebec3f0b1f6705c062b2be769f4138534115f"
CLANG_SIZE = "2224434987"

VERIFY_STEP = "Verify the pinned LLVM 19.1.5 toolchain"
# The verify step must keep its step-local compiler path out of PowerShell's read-only
# automatic ``$HOME`` (variable names are case-insensitive, so ``$home`` is ``$HOME``), and it
# uses this distinct, step-local name instead.
COMPILER_HOME_VARIABLE = "$llvmCompilerHome"

INSTRUMENTATION_JOBS = (
    "coverage-x86", "coverage-x64", "ubsan-x64", "fuzz-x86", "fuzz-x64",
)
# The MSVC 14.44 dynamic ASan slices build without the LLVM 19 instrumentation toolchain.
LLVM_FREE_JOBS = ("asan-x86", "asan-x64")
PREP_JOB = "instrumentation-tools"


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


def verify_setup_lines(step_block: str) -> list[str]:
    """The side-effect-free ``$name = ...`` prefix of a pwsh ``run: |`` step body.

    Only that harmless prefix is extracted, so a regression can execute the step's real
    variable setup without ever downloading, extracting or installing a tool.
    """

    lines = step_block.splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == "run: |")
    setup: list[str] = []
    for line in lines[start + 1:]:
        if re.match(r"^\s*\$[A-Za-z_][A-Za-z0-9_]*\s*=", line):
            setup.append(line.strip())
        else:
            break
    return setup


def run_verify_setup(
    setup: list[str], root: str, *, env_var: str = "LLVM_ROOT"
) -> dict[str, object]:
    """Run a verify-step variable-setup prefix in a fresh pwsh and report the outcome.

    ``root`` is exposed to the prefix through the ``env_var`` environment variable, so both the
    preparation step (``LLVM_ROOT``) and the consumer step (``RUNNER_TEMP``) can drive their real
    setup.  The prefix is wrapped in a ``try`` so an assignment PowerShell rejects (the read-only
    ``$HOME``) surfaces as ``HomeRejected`` rather than an opaque non-zero exit.  No payload is
    downloaded, extracted or installed; only the harmless assignments are executed.
    """

    script = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$env:{env_var} = '{root}'",
            "$homeBefore = $HOME",
            "$homeRejected = $false",
            "try {",
            *(f"  {line}" for line in setup),
            "} catch {",
            "  $homeRejected = $true",
            "}",
            "[pscustomobject]@{ Path = $llvmCompilerHome; HomeRejected = $homeRejected; "
            "HomeBefore = $homeBefore; HomeAfter = $HOME } | ConvertTo-Json -Compress",
        ]
    )
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(completed.stdout.strip().lstrip("\ufeff"))


class InstrumentationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prep = PREP_WORKFLOW.read_text(encoding="utf-8")
        self.check = (WORKFLOWS / "ci-check.yml").read_text(encoding="utf-8")
        self.main = (WORKFLOWS / "main.yml").read_text(encoding="utf-8")

    def _step(self, workflow: str, name: str) -> str:
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
        self.fail(f"workflow step not found: {name}")

    def test_a_single_real_prep_job_publishes_the_toolchain_artifact(self) -> None:
        self.assertTrue(PREP_WORKFLOW.is_file(), "ci-instrumentation.yml is missing")
        self.assertIn("workflow_call", self.prep)
        blocks = job_blocks(self.prep)
        self.assertEqual(sorted(blocks), [PREP_JOB])
        self.assertIn(f"    name: {PREP_JOB}\n", self.prep)
        self.assertIn(f"          name: {ARTIFACT}\n", self.prep)

    def test_the_prep_job_downloads_and_hash_verifies_the_official_payloads(self) -> None:
        self.assertIn(BASE_URL, self.prep)
        self.assertIn(CLANG_URL, self.prep)
        self.assertIn(BASE_SHA, self.prep)
        self.assertIn(CLANG_SHA, self.prep)
        self.assertIn(BASE_SIZE, self.prep)
        self.assertIn(CLANG_SIZE, self.prep)
        # The official Microsoft URLs are not GitHub release assets, so the download is a plain
        # web request; every payload still passes a size and SHA-256 check.
        self.assertIn("Invoke-WebRequest", self.prep)
        self.assertIn("Get-FileHash", self.prep)
        self.assertIn("SHA256", self.prep)

    def test_the_payload_size_pins_are_the_verified_official_byte_counts(self) -> None:
        # 62004420 / 2224434987 are the independently rehashed sizes of the two official payload
        # files behind the pinned URLs; a trimmed or re-cut payload changes them, so both the
        # exact pins and the loud mismatch guard must stay.
        self.assertIn(f'LLVM_BASE_SIZE: "{BASE_SIZE}"', self.prep)
        self.assertIn(f'LLVM_CLANG_SIZE: "{CLANG_SIZE}"', self.prep)
        self.assertIn("Unexpected $($payload.Name) VSIX size", self.prep)

    def test_the_toolchain_cache_uses_the_standard_action_keyed_on_the_payloads(self) -> None:
        self.assertIn("actions/cache/restore@", self.prep)
        self.assertIn("actions/cache/save@", self.prep)
        self.assertIn(f"{BASE_SHA}-{CLANG_SHA}", self.prep)

    def test_the_prep_job_extracts_the_vendor_layout_with_the_stdlib_zipfile(self) -> None:
        self.assertIn("zipfile", self.prep)
        self.assertIn("Contents/VC/Tools/Llvm/", self.prep)
        # The extraction keeps the compiler/coverage tools, the whole x64 ``lib/clang/19`` tree
        # (headers and libraries) and the sibling 32-bit runtime tree, through prefix matches the
        # workflow really uses rather than a literal header path.
        for member in (
            "x64/bin/clang-cl.exe",
            "x64/bin/llvm-cov.exe",
            "x64/bin/llvm-profdata.exe",
            "x64/lib/clang/19/",
            "lib/clang/19/lib/windows",
        ):
            with self.subTest(member=member):
                self.assertIn(member, self.prep)

    def test_the_prep_job_verifies_the_versions_and_runtime_libraries(self) -> None:
        for tool in ("clang-cl", "llvm-cov", "llvm-profdata"):
            with self.subTest(tool=tool):
                self.assertIn(tool, self.prep)
        self.assertIn("19\\.1\\.5", self.prep)
        for arch in ("x86_64", "i386"):
            for runtime in ("profile", "fuzzer", "asan", "asan_cxx"):
                with self.subTest(arch=arch, runtime=runtime):
                    self.assertIn(f"clang_rt.{runtime}-{arch}.lib", self.prep)
        self.assertIn("clang_rt.ubsan_standalone-x86_64.lib", self.prep)
        self.assertIn("clang_rt.ubsan_standalone_cxx-x86_64.lib", self.prep)
        self.assertIn("stddef.h", self.prep)

    def test_the_verify_step_is_unconditional_and_uses_an_exact_version_boundary(self) -> None:
        step = self._step(self.prep, "Verify the pinned LLVM 19.1.5 toolchain")
        # A restored cache HIT must be re-verified before it is trusted, so the step never
        # depends on the cache-hit output and carries no condition of its own.
        self.assertNotIn("cache-hit", step)
        self.assertNotIn("if:", step)
        # The version boundary is exact, so a longer token such as 19.1.50 cannot satisfy it.
        self.assertIn(r"19\.1\.5(?!\d)", step)

    def test_the_verify_step_uses_a_step_local_path_variable_that_leaves_home_alone(self) -> None:
        step = self._step(self.prep, VERIFY_STEP)
        # ``$home`` is PowerShell's read-only automatic ``$HOME``, and variable names are
        # case-insensitive, so ``$home = ...`` silently fails to bind and the later tool and
        # library checks would look under the real user profile instead of the toolchain root.
        self.assertIn(COMPILER_HOME_VARIABLE, step)
        self.assertNotIn("$home", step)
        setup = verify_setup_lines(step)
        self.assertIn(f"{COMPILER_HOME_VARIABLE} = Join-Path $root 'x64'", setup)

        root = r"C:\a\_temp\unpackrat\tools\llvm\19.1.5"
        fixed = run_verify_setup(setup, root)
        self.assertEqual(fixed["Path"], os.path.join(root, "x64"))
        self.assertFalse(fixed["HomeRejected"])
        self.assertEqual(fixed["HomeAfter"], fixed["HomeBefore"])

        # The same prefix under the colliding name reproduces the pre-fix failure: PowerShell
        # rejects the read-only assignment, so the step-local variable never receives the path.
        collided = [line.replace(COMPILER_HOME_VARIABLE, "$home") for line in setup]
        self.assertNotEqual(collided, setup)
        broken = run_verify_setup(collided, root)
        self.assertIsNone(broken["Path"])
        self.assertTrue(broken["HomeRejected"])
        self.assertEqual(broken["HomeAfter"], broken["HomeBefore"])

    def test_the_consumer_step_uses_a_step_local_path_variable_that_leaves_home_alone(self) -> None:
        step = self._step(self.check, "Select the instrumentation toolchain")
        # The consumer derives its root from RUNNER_TEMP, so it must use the same distinct,
        # step-local name for the compiler home; ``$home`` would collide with the read-only
        # automatic ``$HOME`` and silently fail to bind, sending the bin and include checks to
        # the real user profile instead of the toolchain root.
        setup = verify_setup_lines(step)

        # Run the step's real variable-setup prefix first, so the regression fails on the broken
        # behavior (a rejected read-only ``$HOME``) rather than on a string comparison.
        runner_temp = r"C:\a\_temp"
        fixed = run_verify_setup(setup, runner_temp, env_var="RUNNER_TEMP")
        self.assertFalse(fixed["HomeRejected"])
        self.assertEqual(
            fixed["Path"],
            os.path.join(runner_temp, "unpackrat", "tools", "llvm", "19.1.5", "x64"),
        )
        self.assertEqual(fixed["HomeAfter"], fixed["HomeBefore"])

        # The step-local name is the only compiler-home binding the step may carry.
        self.assertIn(COMPILER_HOME_VARIABLE, step)
        self.assertNotIn("$home", step)
        self.assertIn(f"{COMPILER_HOME_VARIABLE} = Join-Path $root 'x64'", setup)

    def test_the_cached_toolchain_is_saved_before_the_artifact_upload(self) -> None:
        save = self.prep.index("actions/cache/save@")
        upload = self.prep.index("actions/upload-artifact@")
        self.assertLess(save, upload)

    def test_the_instrumentation_slices_depend_on_the_prep_job(self) -> None:
        blocks = job_blocks(self.main)
        self.assertIn(PREP_JOB, blocks)
        self.assertIn(
            "uses: ./.github/workflows/ci-instrumentation.yml",
            [line.strip() for line in blocks[PREP_JOB]],
        )
        for job in INSTRUMENTATION_JOBS:
            with self.subTest(job=job):
                self.assertEqual(job_needs(blocks[job]), [PREP_JOB])
        # The native, analysis, source and leak stages never wait on the instrumentation
        # toolchain, and neither do the MSVC 14.44 dynamic ASan slices.
        llvm_free_native_jobs = (
            "source-checks",
            "native-debug-x86", "native-debug-x64", "native-release-x86", "native-release-x64",
            "msvc-analysis-x86", "msvc-analysis-x64", "clang-tidy-x86", "clang-tidy-x64",
            "leaks-x64",
        )
        for job in (*llvm_free_native_jobs, *LLVM_FREE_JOBS):
            with self.subTest(job=job):
                self.assertNotIn(PREP_JOB, job_needs(blocks[job]))

    def test_the_reusable_check_workflow_downloads_the_same_run_artifact(self) -> None:
        download = next(
            line.strip()
            for line in self.check.splitlines()
            if line.strip().startswith("name: ") and line.strip().endswith(ARTIFACT)
        )
        self.assertEqual(download, f"name: {ARTIFACT}")
        self.assertIn("actions/download-artifact@", self.check)
        # The toolchain is selected through the dedicated environment variable, never by
        # overriding the standalone clang-tidy/clang-format pin.
        self.assertIn("UNPACKRAT_LLVM_INSTRUMENTATION", self.check)
        selection = self.check.split("UNPACKRAT_LLVM_INSTRUMENTATION", 1)[1]
        self.assertIn("GITHUB_ENV", selection[:400])
        # The instrumentation stages fetch this run's artifact only: no head SHA and no stash.
        self.assertNotIn("head.sha", self.check)
        self.assertNotIn("head_sha", self.check)
        self.assertNotIn("stash", self.check)

    def test_only_the_instrumentation_stages_download_the_toolchain(self) -> None:
        # Bind the selector to the real artifact-download step, not to the first stage-gated
        # step in the file (the MSVC toolset check gates the native stage too).
        step = self._step(self.check, "Collect the instrumentation toolchain")
        self.assertIn("actions/download-artifact@", step)
        self.assertIn(ARTIFACT, step)
        stages = re.search(
            r"if:\s*contains\(fromJSON\('\[([^\]]*)\]'\), inputs\.stage\)",
            step,
        )
        self.assertIsNotNone(stages, "instrumentation stages selector is missing")
        selected = {stage.strip().strip('"') for stage in stages.group(1).split(",")}
        self.assertEqual(
            selected,
            {"coverage", "ubsan", "fuzz"},
        )

    def test_job_needs_helper_reads_list_single_and_absent_dependencies(self) -> None:
        # The helper backs the dependency assertions above, so pin its three shapes directly.
        self.assertEqual(job_needs(["    needs: [one, two]\n"]), ["one", "two"])
        self.assertEqual(job_needs(["    needs: only\n", "    runs-on: x\n"]), ["only"])
        self.assertEqual(job_needs(["    runs-on: windows-2025-vs2026\n"]), [])

    def test_step_helper_locates_the_final_block_and_reports_a_missing_step(self) -> None:
        # The final step of the prep workflow has no following ``- name:`` line, so the block must
        # run to the end of the file; a name that is absent must fail loudly instead of returning
        # an empty block.
        final = self._step(self.prep, "Upload the LLVM 19.1.5 instrumentation toolchain")
        self.assertIn("actions/upload-artifact@", final)
        # Only the step's own header mentions a name; no following step was captured.
        self.assertEqual(final.count("- name:"), 1)
        with self.assertRaises(AssertionError):
            self._step(self.prep, "a step that does not exist")

    def test_verify_setup_lines_take_only_the_leading_assignments(self) -> None:
        # The helper feeds the pwsh regression above, so pin both of its shapes: it stops at the
        # first non-assignment line, and a block of nothing but assignments has no terminator, so
        # the scan completes and every assignment is returned.
        block = (
            "        run: |\n"
            "          $root = $env:LLVM_ROOT\n"
            "          $llvmCompilerHome = Join-Path $root 'x64'\n"
            "          foreach ($tool in @('clang-cl')) { }\n"
        )
        self.assertEqual(
            verify_setup_lines(block),
            ["$root = $env:LLVM_ROOT", "$llvmCompilerHome = Join-Path $root 'x64'"],
        )
        self.assertEqual(verify_setup_lines("run: |\n  $only = 'one'"), ["$only = 'one'"])


if __name__ == "__main__":
    unittest.main()

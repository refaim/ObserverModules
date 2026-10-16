"""The Release slices feed one real native Linux license audit built from pinned official binaries.

These are composition contracts over the workflows' own declarations: the ``license-inputs`` step the
reusable check pipeline gains for ``native-release``, the reusable Linux job that consumes those
same-run artifacts, the pinned upstream policy it fetches, the checksum-verified official ORT and
ScanCode releases it downloads and configures, the loopback source server, the failure-preserving
stage sequencing, the two cache contracts and the packaging gate.  Nothing here re-implements
GitHub's expression evaluator; each assertion reads a real declaration of the workflow.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
WORKFLOWS = REPOSITORY / ".github" / "workflows"
MAIN = WORKFLOWS / "main.yml"
CHECK = WORKFLOWS / "ci-check.yml"
AUDIT = WORKFLOWS / "license-audit.yml"

UPLOAD = "cf430e030ddbb5b0abf93d22962f4752f3646cd9"
DOWNLOAD = "9000827ccba6bdab643e8b6fd33ac0654aef8333"
CACHE = "55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
ORT_CONFIG_REPOSITORY = "oss-review-toolkit/ort-config"
ORT_CONFIG_REVISION = "5075f4308109384c98a6a2fab53246f7b60d59f4"

# The pinned official tools: release asset names and their hard-coded SHA-256 checksums.
ORT_VERSION = "97.0.0"
ORT_ASSET = "ort-97.0.0.tgz"
ORT_SHA256 = "7b9b1cc9dd7af027e91402f739f2e01dfb73367ca9c1d0cbef3b74a14bce77ae"
SCANCODE_VERSION = "32.5.0"
SCANCODE_ASSET = "scancode-toolkit-v32.5.0_py3.14-linux.tar.gz"
SCANCODE_SHA256 = "02be93341e2f9775f88b4abd03cdd74f2e4de91941a12a1d8cd150eeb72a0945"


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


def step_condition(step: str) -> str:
    return "\n".join(line.strip() for line in step.splitlines() if line.strip().startswith("if:"))


def job_env_block(workflow: str) -> str:
    """Return the single job's ``env:`` block, exactly as declared, without any step declarations.

    GitHub Actions evaluates a job-level ``env:`` block with a restricted context set and rejects a
    reusable workflow whose job scope reads a context that is not available there, so the raw block
    text is what the contract must inspect.
    """

    _, _, rest = workflow.partition("\n    env:\n")
    env, _, _ = rest.partition("\n    steps:\n")
    return env


class LicenseInputsStepTests(unittest.TestCase):
    """The reusable native Release slice emits the exact license inputs ORT audits."""

    def setUp(self) -> None:
        self.check = CHECK.read_text(encoding="utf-8")

    def test_native_release_emits_the_license_inputs_through_the_driver(self) -> None:
        step = step_block(self.check, "Produce the license inputs")
        self.assertEqual(step_condition(step), "if: inputs.stage == 'native-release'")
        self.assertIn("license-inputs -Arch '${{ inputs.arch }}'", step)
        self.assertIn("-Output out/license-inputs/${{ inputs.arch }}", step)
        # The inputs are derived from the tree the same step's ci-arch gate just built.
        self.assertLess(
            self.check.index("      - name: Verify one stage for one architecture\n"),
            self.check.index("      - name: Produce the license inputs\n"),
        )

    def test_the_license_inputs_are_uploaded_and_a_missing_one_is_an_error(self) -> None:
        step = step_block(self.check, "Upload the license inputs")
        self.assertEqual(step_condition(step), "if: success() && inputs.stage == 'native-release'")
        self.assertIn("actions/upload-artifact@" + UPLOAD, step)
        self.assertIn("name: license-inputs-${{ inputs.arch }}", step)
        self.assertIn("path: out/license-inputs/${{ inputs.arch }}", step)
        self.assertIn("if-no-files-found: error", step)

    def test_partial_license_inputs_survive_a_failed_stage(self) -> None:
        step = step_block(self.check, "Retain the partial license inputs")
        self.assertEqual(step_condition(step), "if: failure() && inputs.stage == 'native-release'")
        self.assertIn("actions/upload-artifact@" + UPLOAD, step)
        self.assertIn("if-no-files-found: ignore", step)
        self.assertIn("path: out/license-inputs/${{ inputs.arch }}", step)

    def test_the_license_inputs_reuse_the_prepared_environment_without_a_new_restore(self) -> None:
        step = step_block(self.check, "Produce the license inputs")
        self.assertNotIn("vcpkg", step)
        self.assertNotIn("uv sync", step)
        # The one pinned Python environment preparation the job already owns is unchanged.
        self.assertEqual(self.check.count("uv sync --project build --frozen"), 1)


class LicenseAuditJobTests(unittest.TestCase):
    """The native Linux ORT job consumes the same-run inputs and applies repository data policy."""

    def setUp(self) -> None:
        self.main = MAIN.read_text(encoding="utf-8")
        self.audit = AUDIT.read_text(encoding="utf-8")
        self.blocks = job_blocks(self.main)

    def test_main_declares_one_license_audit_job_calling_the_reusable_workflow(self) -> None:
        self.assertIn("license-audit", self.blocks)
        self.assertEqual(
            sorted(job_needs(self.blocks["license-audit"])),
            ["native-release-x64", "native-release-x86"],
        )
        body = "\n".join(self.blocks["license-audit"])
        self.assertIn("uses: ./.github/workflows/license-audit.yml", body)
        self.assertIn("      contents: read", body)

    def test_packaging_gates_on_the_license_audit(self) -> None:
        self.assertIn("license-audit", job_needs(self.blocks["packaging"]))

    def test_the_reusable_workflow_is_a_pinned_native_linux_job(self) -> None:
        self.assertIn("workflow_call:", self.audit)
        self.assertIn("runs-on: ubuntu-24.04", self.audit)
        self.assertNotIn("ubuntu-latest", self.audit)
        self.assertNotIn("continue-on-error", self.audit)
        self.assertNotIn("secrets.", self.audit)

    def test_no_container_runtime_or_image_is_referenced(self) -> None:
        # Roma rejected pulling the multi-gigabyte image for CI, so the job must run native tools.
        lowered = self.audit.lower()
        self.assertNotIn("docker", lowered)
        self.assertNotIn("ghcr.io", lowered)
        self.assertNotIn("--network host", self.audit)

    def test_the_audit_fetches_the_pinned_upstream_policy_instead_of_vendoring_it(self) -> None:
        step = step_block(self.audit, "Check out the pinned upstream ORT configuration")
        self.assertIn("actions/checkout@", step)
        self.assertIn(f"repository: {ORT_CONFIG_REPOSITORY}", step)
        self.assertIn(f"ref: {ORT_CONFIG_REVISION}", step)
        self.assertIn("path: out/ort-config", step)
        self.assertIn("persist-credentials: false", step)

    def test_the_audit_downloads_the_same_run_inputs_and_prepares_them(self) -> None:
        collect_x86 = step_block(self.audit, "Collect the x86 license inputs")
        collect_x64 = step_block(self.audit, "Collect the x64 license inputs")
        self.assertIn("actions/download-artifact@" + DOWNLOAD, collect_x86)
        self.assertIn("name: license-inputs-x86", collect_x86)
        self.assertIn("path: out/license-inputs/x86", collect_x86)
        self.assertIn("name: license-inputs-x64", collect_x64)
        self.assertIn("path: out/license-inputs/x64", collect_x64)
        prepare = step_block(self.audit, "Prepare the SPDX project and curations")
        # A Linux shell cannot run the PowerShell entry point; the driver is invoked directly.
        self.assertNotIn("build.ps1", prepare)
        self.assertIn("uv run", prepare)
        self.assertIn("python build/main.py license-prepare", prepare)
        self.assertIn("-Inputs out/license-inputs -Output out/license-ready", prepare)

    def test_the_official_tools_are_downloaded_with_gh_and_checksum_verified_before_extract(self) -> None:
        step = step_block(self.audit, "Install the pinned ORT and ScanCode releases on a cache miss")
        self.assertIn("gh release download", step)
        self.assertIn("--repo oss-review-toolkit/ort", step)
        self.assertIn("--repo aboutcode-org/scancode-toolkit", step)
        self.assertIn('"${ORT_VERSION}"', step)
        self.assertIn('"v${SCANCODE_VERSION}"', step)
        self.assertIn('--pattern "ort-${ORT_VERSION}.tgz"', step)
        self.assertIn('--pattern "${SCANCODE_ASSET}"', step)
        # The pinned checksums for both assets are verified BEFORE either archive is extracted.
        self.assertIn('"${ORT_SHA256}  ${downloads}/ort-${ORT_VERSION}.tgz" | sha256sum -c -', step)
        self.assertIn('"${SCANCODE_SHA256}  ${downloads}/${SCANCODE_ASSET}" | sha256sum -c -', step)
        self.assertEqual(step.count("sha256sum -c -"), 2)
        self.assertLess(step.index("sha256sum -c -"), step.index("tar -xzf"))
        self.assertIn("tar -xzf", step)

    def test_the_env_pins_the_exact_official_assets_and_checksums(self) -> None:
        # The asset names and checksums are repository data, not guessed at run time.
        self.assertIn(f'ORT_VERSION: "{ORT_VERSION}"', self.audit)
        self.assertIn(f'ORT_SHA256: "{ORT_SHA256}"', self.audit)
        self.assertIn(f'SCANCODE_VERSION: "{SCANCODE_VERSION}"', self.audit)
        self.assertIn(f'SCANCODE_ASSET: "{SCANCODE_ASSET}"', self.audit)
        self.assertIn(f'SCANCODE_SHA256: "{SCANCODE_SHA256}"', self.audit)

    def test_the_job_env_never_reads_the_runner_context(self) -> None:
        # GitHub Actions evaluates a job-level env block with only github/needs/strategy/matrix/
        # vars/secrets/inputs, so a reusable workflow that bound a tool path to runner.temp at job
        # scope is rejected before any job runs.  The runner-dependent tool paths must not be pinned
        # there; only the static pins and checksums live in the job env.
        env = job_env_block(self.audit)
        self.assertNotIn("runner.", env)
        self.assertNotIn("LICENSE_TOOLS:", env)
        self.assertNotIn("SCANCODE_CACHE:", env)

    def test_the_tool_paths_are_initialized_early_through_github_env(self) -> None:
        # The two runner-dependent tool locations are exported once, at step level where RUNNER_TEMP
        # is available, before any step that consumes them.  The exported values are the exact
        # effective paths: the tool root and its ScanCode cache beneath it.
        step = step_block(self.audit, "Initialize the pinned tool paths")
        self.assertIn("RUNNER_TEMP", step)
        self.assertIn("GITHUB_ENV", step)
        self.assertIn(
            'echo "LICENSE_TOOLS=${RUNNER_TEMP}/license-tools" >> "${GITHUB_ENV}"', step
        )
        self.assertIn(
            'echo "SCANCODE_CACHE=${RUNNER_TEMP}/license-tools/scancode-cache" >> "${GITHUB_ENV}"',
            step,
        )
        # It precedes every consumer of the tool root: the cache restore/save, the cold install, the
        # ScanCode PATH export and the ORT run.
        initialized = self.audit.index("- name: Initialize the pinned tool paths")
        for consumer in (
            "Restore the pinned tool cache",
            "Install the pinned ORT and ScanCode releases on a cache miss",
            "Expose the configured ScanCode on the job PATH",
            "Run the pinned ORT audit",
        ):
            with self.subTest(consumer=consumer):
                self.assertLess(initialized, self.audit.index(consumer))

    def test_the_scan_code_venv_is_configured_offline_with_the_pinned_python(self) -> None:
        step = step_block(self.audit, "Install the pinned ORT and ScanCode releases on a cache miss")
        self.assertIn("scancode-toolkit-v${SCANCODE_VERSION}", step)
        self.assertIn("./configure", step)
        # The interpreter is the pinned, already-prepared uv environment, used by absolute path.
        self.assertIn(
            'PYTHON_EXECUTABLE="${GITHUB_WORKSPACE}/build/.venv/bin/python"', step
        )
        # configure is invoked from the extracted toolkit root so the bundled thirdparty wheels and
        # the bundled virtualenv.pyz drive an offline install; no index is fetched by this workflow.
        self.assertIn('( cd "${scancode_root}"', step)
        self.assertNotIn("pip install", step)
        self.assertNotIn("pip download", step)

    def test_the_configured_scan_code_wrapper_is_exposed_on_the_job_path(self) -> None:
        step = step_block(self.audit, "Expose the configured ScanCode on the job PATH")
        self.assertIn("scancode-toolkit-v${SCANCODE_VERSION}/venv/bin", step)
        self.assertIn("GITHUB_PATH", step)
        self.assertIn("--version", step)
        # A missing wrapper fails the job instead of silently running an unpinned scanner.
        self.assertIn("exit 1", step)

    def test_the_installation_wheels_are_removed_after_configure_before_the_cache_save(self) -> None:
        install = step_block(self.audit, "Install the pinned ORT and ScanCode releases on a cache miss")
        # The root ScanCode wheel and every bundled thirdparty wheel are consumed by the offline
        # configure and are then dead duplicate weight, so they are deleted before the cache save.
        self.assertIn("scancode_toolkit-*.whl", install)
        self.assertIn("thirdparty", install)
        self.assertIn("-name '*.whl' -delete", install)
        # The deletion must follow the successful configure so the wheels are still present when
        # the venv is built, and must not touch the installed environment that the cache preserves.
        self.assertLess(install.index("./configure"), install.index("*.whl"))
        self.assertNotIn("rm -rf \"${scancode_root}/venv\"", install)
        # The official archive already ships the bundled package index, so no reindex step exists.
        self.assertNotIn("reindex", self.audit.lower())
        # The cache save is a separate later step, so the wheels are gone before it runs.
        self.assertLess(
            self.audit.index("Install the pinned ORT and ScanCode releases on a cache miss"),
            self.audit.index("Save the pinned tool cache"),
        )

    def test_the_java_25_runtime_comes_from_the_runner_image_and_fails_closed(self) -> None:
        step = step_block(self.audit, "Configure the pinned Java 25 runtime")
        self.assertIn("JAVA_HOME_25_X64", step)
        self.assertIn("GITHUB_ENV", step)
        self.assertIn("GITHUB_PATH", step)
        self.assertIn("exit 1", step)
        # The job never installs a JDK; it only wires the image-provided Java 25.
        self.assertNotIn("setup-java", self.audit)

    def test_the_tool_cache_key_binds_pin_and_platform_and_never_restores_a_partial_prefix(self) -> None:
        key = step_block(self.audit, "Compute the pinned tool cache key")
        self.assertIn("license-tools-", key)
        self.assertIn("GITHUB_OUTPUT", key)
        for token in (
            "ORT_VERSION", "ORT_SHA256", "SCANCODE_VERSION", "SCANCODE_SHA256",
            "${{ runner.os }}", "${{ runner.arch }}", "python_full", "python_base",
        ):
            with self.subTest(token=token):
                self.assertIn(token, key)
        # The restored ScanCode virtualenv is built from an absolute uv interpreter, so the key
        # must bind the FULL interpreter version and the resolved base interpreter prefix; the
        # minor version alone would let a different patch-level or base interpreter reuse the cache.
        self.assertIn("sys.version_info[:3]", key)
        self.assertIn("sys.base_prefix", key)
        self.assertIn("-py${python_full}-base-", key)
        self.assertNotIn("sys.version_info.minor", key)

        restore = step_block(self.audit, "Restore the pinned tool cache")
        self.assertIn("actions/cache/restore@" + CACHE, restore)
        self.assertIn("${{ runner.temp }}/license-tools", restore)
        self.assertIn("${{ steps.tool-cache-key.outputs.key }}", restore)
        # Binary tools must be an exact match: no prefix restore may hand back a partial cache.
        self.assertNotIn("restore-keys", restore)

        save = step_block(self.audit, "Save the pinned tool cache")
        self.assertIn("actions/cache/save@" + CACHE, save)
        self.assertIn("${{ steps.tool-cache-key.outputs.key }}", save)
        self.assertEqual(
            step_condition(save), "if: steps.tool-cache.outputs.cache-hit != 'true'"
        )

    def test_the_prepared_sources_are_served_from_the_prepared_root_over_loopback(self) -> None:
        stage = step_block(self.audit, "Stage the ORT input and configuration outside the checkout")
        self.assertIn('root="${RUNNER_TEMP}/license-audit"', stage)
        # The generated download URLs are /sources/<artifact>, so the served directory is the
        # prepared root that contains sources/, not the sources/ leaf itself.
        self.assertIn('served="${root}/served"', stage)
        self.assertIn('cp -RT "${ready}/sources" "${served}/sources"', stage)
        self.assertIn('"${root}/input"', stage)
        self.assertNotIn("${root}/input/sources", stage)

        run = step_block(self.audit, "Run the pinned ORT audit")
        self.assertIn('python3 -m http.server 8765 --bind 127.0.0.1 --directory "${served}"', run)
        self.assertIn("trap ", run)
        self.assertIn("kill ", run)
        self.assertNotIn('--directory "${root}/sources"', run)
        self.assertNotIn("file://", run)
        self.assertNotIn("cas-server", run)

    def test_the_generated_repository_configuration_is_staged_not_the_base_file_alone(self) -> None:
        stage = step_block(self.audit, "Stage the ORT input and configuration outside the checkout")
        # The prepared .ort.yml (base resolutions merged with the exact package license choices) is
        # the repository configuration ORT reads.  A missing generated file must fail the staging
        # step instead of silently dropping the reviewed choices by falling back to the base file.
        self.assertIn('"${ready}/.ort.yml"', stage)
        self.assertIn('if [ ! -f "${ready}/.ort.yml" ]', stage)
        self.assertIn("exit 1", stage)
        self.assertNotIn("cp .ort.yml ", stage)
        self.assertNotIn("repository_license_choices", stage)

    def test_the_global_configuration_is_rewritten_from_the_container_prefix_to_the_native_home(self) -> None:
        stage = step_block(self.audit, "Stage the ORT input and configuration outside the checkout")
        # The tracked global configuration roots its owned paths at the fixed /ort/home prefix; the
        # one narrow staging rewrite maps that literal prefix to the real native audit home.
        self.assertIn('sed "s#/ort/home#${root}/home#g" .ort/config/config.yml > "${cfg}/config.yml"', stage)

    def test_the_upstream_rules_and_classifications_come_from_the_fetched_pin(self) -> None:
        stage = step_block(self.audit, "Stage the ORT input and configuration outside the checkout")
        self.assertIn("out/ort-config/evaluator.rules.kts", stage)
        self.assertIn("out/ort-config/license-classifications.yml", stage)
        # Our reviewed global configuration is staged as data.
        self.assertIn(".ort/config/config.yml", stage)
        # The generated per-package source curations become the global curations at the prepared home.
        self.assertIn("curations.yml", stage)
        self.assertIn('"${ready}/package-curations.yml"', stage)

    def test_the_native_ort_binary_runs_the_four_explicit_stages(self) -> None:
        run = step_block(self.audit, "Run the pinned ORT audit")
        self.assertIn('ort="${LICENSE_TOOLS}/ort-${ORT_VERSION}/bin/ort"', run)
        self.assertIn("ort_run()", run)
        for command in ("stage analyze ", "stage scan ", "stage evaluate ", "stage report "):
            with self.subTest(command=command):
                self.assertIn(command, run)

    def test_every_cli_argument_is_a_native_host_path(self) -> None:
        run = step_block(self.audit, "Run the pinned ORT audit")
        # The global configuration is passed explicitly and the analyzer has no curation flag: its
        # curations come from the explicit File provider at the prepared home.
        self.assertIn('-c "${cfg}/config.yml"', run)
        analyze = run.split("stage analyze ", 1)[1].split("stage scan ", 1)[0]
        self.assertNotIn("--package-curations-file", analyze)
        self.assertIn('-i "${root}/input"', analyze)
        self.assertIn("stage scan ", run)
        scan = run.split("stage scan ", 1)[1].split("stage evaluate ", 1)[0]
        self.assertIn("--scanners ScanCode", scan)
        self.assertIn("--package-types=PACKAGE", scan)
        # No leftover container-only mount or input/result paths survive in a native run.
        self.assertNotIn("/ort/input", run)
        self.assertNotIn("/ort/results", run)

    def test_the_evaluator_and_reporter_use_the_repository_data_files(self) -> None:
        run = step_block(self.audit, "Run the pinned ORT audit")
        evaluate = run.split("stage evaluate ", 1)[1].split("stage report ", 1)[0]
        self.assertIn('--rules-file "${cfg}/evaluator.rules.kts"', evaluate)
        self.assertIn('--license-classifications-file "${cfg}/license-classifications.yml"', evaluate)
        self.assertIn('--package-configurations-dir "${cfg}/package-configurations"', evaluate)
        self.assertIn("--repository-configuration-file", evaluate)
        report = run.split("stage report ", 1)[1]
        # The default PlainTextTemplate output is the NOTICE report; no first-party generator exists.
        self.assertIn("CycloneDX,SpdxDocument,WebApp,PlainTextTemplate", report)

    def test_the_reporter_runs_from_the_latest_available_result_and_the_worst_exit_code_survives(self) -> None:
        run = step_block(self.audit, "Run the pinned ORT audit")
        # No early abort: the job must continue past a failing stage so the reporter still runs.
        self.assertNotIn("set -euo pipefail", run)
        self.assertIn("set -uo pipefail", run)
        self.assertIn("set +e", run)
        # The first non-zero stage exit code is retained and re-emitted at the end.
        self.assertIn("stage_status", run)
        self.assertIn('exit "${stage_status}"', run)
        # The reporter consumes whichever result file exists, newest stage first.
        for candidate in ("evaluation-result.json", "scan-result.json", "analyzer-result.json"):
            with self.subTest(candidate=candidate):
                self.assertIn(candidate, run)

    def test_the_reporter_probes_the_native_host_paths(self) -> None:
        run = step_block(self.audit, "Run the pinned ORT audit")
        self.assertIn(
            "for candidate in evaluator/evaluation-result.json scanner/scan-result.json "
            "analyzer/analyzer-result.json",
            run,
        )
        self.assertIn('"${results}/${candidate}"', run)
        self.assertIn('-i "${result}"', run)
        self.assertNotIn('if [ -s "/ort/results/', run)

    def test_each_stage_output_is_retained_as_a_log_without_losing_pipestatus(self) -> None:
        run = step_block(self.audit, "Run the pinned ORT audit")
        self.assertIn('mkdir -p "${results}" "${results}/logs"', run)
        self.assertIn('log="${results}/logs/${name}.log"', run)
        self.assertIn('| tee "${log}"', run)
        # The tool's own exit code, not tee's, is the verdict.
        self.assertIn("PIPESTATUS", run)

    def test_a_stage_that_produces_no_expected_result_fails_closed(self) -> None:
        run = step_block(self.audit, "Run the pinned ORT audit")
        self.assertIn("require_result", run)
        self.assertIn("failing closed", run)
        # The recorded verdict is the tool exit code, which a missing result must not mask.
        self.assertIn("stage_status", run)

    def test_the_audit_emits_cold_and_warm_timing_evidence(self) -> None:
        install = step_block(self.audit, "Install the pinned ORT and ScanCode releases on a cache miss")
        self.assertIn("SECONDS", install)
        self.assertIn("tool-setup=", install)
        self.assertIn("GITHUB_STEP_SUMMARY", install)
        run = step_block(self.audit, "Run the pinned ORT audit")
        self.assertIn("SECONDS", run)
        self.assertIn("GITHUB_STEP_SUMMARY", run)
        for stage in ("analyze", "scan", "evaluate", "report"):
            with self.subTest(stage=stage):
                self.assertIn(f"stage {stage} ", run)

    def test_the_audit_does_not_treat_a_nonempty_artifact_as_proof_of_success(self) -> None:
        # The verdict is each stage's own exit code, not the mere presence of files.
        self.assertNotIn("Require the completed ORT artifacts", self.audit)

    def test_the_ort_native_file_cache_allows_new_entries_when_inputs_change(self) -> None:
        key = step_block(self.audit, "Compute the ORT native file cache key")
        # The key must bind the pinned ORT version and a hash of the effective configuration, rules
        # and scanner options, not the version alone, so a changed policy invalidates the cache.
        self.assertIn("ORT_VERSION", key)
        self.assertIn("sha256sum", key)
        self.assertIn("GITHUB_OUTPUT", key)
        for name in (
            "config.yml",
            "evaluator.rules.kts",
            "license-classifications.yml",
            "repository-configuration.yml",
        ):
            with self.subTest(name=name):
                self.assertIn(name, key)

        restore = step_block(self.audit, "Restore the ORT native file cache")
        self.assertIn("actions/cache/restore@" + CACHE, restore)
        self.assertIn("${{ steps.ort-cache-key.outputs.key }}", restore)
        # A unique per-run suffix plus a restore prefix lets a re-scan add a fresh entry when a
        # dependency changes instead of pretending the immutable key was updated.
        self.assertIn("restore-keys:", restore)
        save = step_block(self.audit, "Save the ORT native file cache")
        self.assertIn("actions/cache/save@" + CACHE, save)
        self.assertIn("${{ steps.ort-cache-key.outputs.key }}", save)

    def test_the_ort_native_cache_restore_prefix_carries_the_config_digest(self) -> None:
        # ORT's scanner applies the detected-license mapping *before* it stores results, so a
        # tightened mapping must never restore a cache populated under a different configuration:
        # the restore prefix carries the same configuration digest as the primary key.
        key = step_block(self.audit, "Compute the ORT native file cache key")
        self.assertIn('echo "digest=${digest}"', key)
        restore = step_block(self.audit, "Restore the ORT native file cache")
        self.assertIn(
            "ort-native-${{ env.ORT_VERSION }}-${{ steps.ort-cache-key.outputs.digest }}-",
            restore,
        )

    def test_all_audit_evidence_is_uploaded_even_on_failure(self) -> None:
        step = step_block(self.audit, "Upload the license audit evidence")
        self.assertIn("if: always()", step_condition(step))
        self.assertIn("actions/upload-artifact@" + UPLOAD, step)
        self.assertIn("name: license-audit-results", step)
        self.assertIn("license-audit/results", step)
        self.assertIn("license-audit/home/config", step)
        self.assertIn("out/license-ready", step)
        self.assertIn("out/license-inputs", step)


if __name__ == "__main__":
    unittest.main()

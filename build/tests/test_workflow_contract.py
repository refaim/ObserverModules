from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import tomllib
import unittest


REPOSITORY = Path(__file__).parents[2]


class WorkflowContractTests(unittest.TestCase):
    def main_workflow(self) -> str:
        return (REPOSITORY / ".github/workflows/main.yml").read_text(encoding="utf-8")

    def check_workflow(self) -> str:
        return (REPOSITORY / ".github/workflows/ci-check.yml").read_text(encoding="utf-8")

    def all_workflows(self) -> str:
        return "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((REPOSITORY / ".github/workflows").glob("*.yml"))
        )

    def job_permissions(self, workflow: str) -> dict[str, dict[str, str]]:
        """Map each top-level job in a workflow's ``jobs`` section to its permission grants."""
        _, _, jobs = workflow.partition("\njobs:\n")
        grants: dict[str, dict[str, str]] = {}
        current: str | None = None
        collecting = False
        for line in jobs.splitlines():
            job = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
            if job:
                current = job.group(1)
                grants[current] = {}
                collecting = False
                continue
            if current is None:
                continue
            if re.match(r"^    permissions:\s*$", line):
                collecting = True
                continue
            if collecting:
                grant = re.match(r"^      ([A-Za-z0-9_-]+):\s*(\S+)\s*$", line)
                if grant:
                    grants[current][grant.group(1)] = grant.group(2)
                else:
                    collecting = False
        return grants

    def job_blocks(self, workflow: str) -> dict[str, list[str]]:
        """Split a workflow's ``jobs`` section into the line block of each top-level job."""
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

    def job_needs(self, block: list[str]) -> list[str]:
        """Read a job block's declared ``needs`` as the ordered list of dependencies."""
        for line in block:
            needs = re.match(r"^    needs:\s*(.+?)\s*$", line)
            if needs:
                value = needs.group(1)
                if value.startswith("["):
                    return [name.strip() for name in value.strip("[]").split(",") if name.strip()]
                return [value]
        return []

    def test_check_workflow_inherits_the_caller_permission_grant(self) -> None:
        check = self.check_workflow()
        caller = self.main_workflow()

        # GitHub's reusable-workflow reference: a called workflow can only keep or lower the
        # caller's token permissions; it can never widen them. The maintained check workflow must
        # therefore declare no permissions block of its own and inherit the caller grant, while
        # the caller stays least-privilege and mints id-token only for the three Codecov uploads.
        self.assertNotIn("permissions:", check)

        grants = self.job_permissions(caller)
        oidc = sorted(name for name, entries in grants.items() if "id-token" in entries)
        self.assertEqual(oidc, ["coverage-x64", "coverage-x86", "source-checks"])
        callers = (
            "source-checks",
            "native-debug-x86", "native-debug-x64", "native-release-x86", "native-release-x64",
            "msvc-analysis-x86", "msvc-analysis-x64", "clang-tidy-x86", "clang-tidy-x64",
            "coverage-x86", "coverage-x64", "asan-x86", "asan-x64", "ubsan-x64",
            "fuzz-x86", "fuzz-x64", "leaks-x64", "license-audit",
        )
        for job in callers:
            expected = {"contents": "read", "id-token": "write"} if job in oidc else {"contents": "read"}
            if job in {"source-checks", "msvc-analysis-x86", "msvc-analysis-x64",
                       "clang-tidy-x86", "clang-tidy-x64"}:
                expected["security-events"] = "write"
            with self.subTest(job=job):
                self.assertEqual(grants[job], expected)

    def test_vcpkg_cache_key_normalizes_the_source_architecture(self) -> None:
        check = self.check_workflow()

        # The source stage is dispatched with arch "none" yet restores the x64 Debug dependency
        # cache, so the key's architecture field must normalize "none" to "x64" through a GitHub
        # expression instead of embedding the raw caller input. The runner image, the
        # standard/asan/fuzz selector and the dependency hash stay part of the same key. Fuzz
        # gets its own cache because its standalone static-CRT dependency tree must not share an
        # entry with the ASan dynamic-CRT tree.
        self.assertIn("      arch: none\n      stage: source\n", self.main_workflow())
        key = next(
            line.strip()
            for line in check.splitlines()
            if line.strip().startswith("key: vcpkg-")
        )
        self.assertIn("${{ steps.runner-image.outputs.identity }}", key)
        self.assertIn("${{ inputs.arch == 'none' && 'x64' || inputs.arch }}", key)
        self.assertIn(
            "${{ inputs.stage == 'ubsan' && 'ubsan' || inputs.stage == 'asan' && 'asan' || inputs.stage == 'fuzz' && 'fuzz' || 'standard' }}",
            key,
        )
        # A changed overlay recipe must get a new Actions cache so its new ABI binaries can be saved.
        self.assertIn(
            "${{ hashFiles('vcpkg.json', 'vcpkg-configuration.json', 'build/vcpkg/ports/**', 'build/vcpkg/triplets/*.cmake') }}",
            key,
        )

    def test_triggers_and_concurrency_are_preserved(self) -> None:
        workflow = self.main_workflow()

        self.assertIn("pull_request:", workflow)
        self.assertIn("push:", workflow)
        self.assertGreaterEqual(workflow.count("branches: [master]"), 2)
        self.assertNotIn("workflow_dispatch", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", workflow)
        self.assertIn("runs-on: windows-2025-vs2026", workflow)
        self.assertNotIn("continue-on-error", workflow)

    def test_no_aggregate_gate_jobs_or_bash_result_scripts_remain(self) -> None:
        workflow = self.main_workflow()

        # The three aggregate `source`/`x86`/`x64` jobs are removed: the required check names
        # become the real per-stage jobs directly, with no job left re-scoring their results in a
        # shell script. Nothing here keeps an old aggregate gate or its decision table alive.
        blocks = self.job_blocks(workflow)
        for name in ("source", "x86", "x64"):
            with self.subTest(name=name):
                self.assertNotIn(name, blocks)
        self.assertNotIn("if: always()", workflow)
        self.assertNotIn("toJSON(needs.*.result)", workflow)
        self.assertNotIn('grep -Eq \'"(failure|cancelled|skipped)"\'', workflow)
        self.assertNotIn("runs-on: ubuntu-latest", workflow)

    def test_slices_are_independent_reusable_calls(self) -> None:
        workflow = self.main_workflow()

        self.assertEqual(workflow.count("uses: ./.github/workflows/ci-check.yml"), 17)
        jobs = (
            "source-checks",
            "native-debug-x86", "native-debug-x64", "native-release-x86", "native-release-x64",
            "msvc-analysis-x86", "msvc-analysis-x64", "clang-tidy-x86", "clang-tidy-x64",
            "coverage-x86", "coverage-x64", "asan-x86", "asan-x64", "ubsan-x64",
            "fuzz-x86", "fuzz-x64", "leaks-x64",
        )
        blocks = self.job_blocks(workflow)
        # The whole job set is exactly the eleven real reusable-workflow calls plus the one real
        # instrumentation-tools provisioning job and the tag-gated packaging and release jobs; no
        # aggregate gate survives.
        self.assertEqual(
            sorted(blocks),
            sorted([*jobs, "instrumentation-tools", "license-audit", "packaging", "release"]),
        )
        for job in jobs:
            with self.subTest(job=job):
                self.assertIn(f"\n  {job}:\n", workflow)
        # The instrumentation-tools job provisions the shared toolchain; it is a provisioning
        # step, not an aggregate verdict over the other jobs.
        self.assertIn("uses: ./.github/workflows/ci-instrumentation.yml", workflow)
        self.assertEqual(self.job_needs(blocks["instrumentation-tools"]), [])
        # The leak measurement depends only on the x64 Release slice that built the tree it
        # measures, never on a whole matrix of both architectures.
        self.assertEqual(self.job_needs(blocks["leaks-x64"]), ["native-release-x64"])
        # The Linux license audit consumes both Release slices and waits on nothing else.
        self.assertEqual(
            sorted(self.job_needs(blocks["license-audit"])),
            ["native-release-x64", "native-release-x86"],
        )
        self.assertNotIn("matrix:", workflow)
        # No verification slice waits on another except the instrumentation stages, which wait on
        # the shared toolchain provisioning, and the leak measurement. Only packaging fans the
        # real gates back in.
        instrumentation = {
            "coverage-x86", "coverage-x64", "ubsan-x64", "fuzz-x86", "fuzz-x64",
        }
        for job in jobs:
            with self.subTest(job=job):
                if job in instrumentation:
                    self.assertEqual(self.job_needs(blocks[job]), ["instrumentation-tools"])
                elif job == "leaks-x64":
                    self.assertEqual(self.job_needs(blocks[job]), ["native-release-x64"])
                else:
                    # The MSVC 14.44 dynamic ASan slices build without the LLVM 19 toolchain.
                    self.assertEqual(self.job_needs(blocks[job]), [])

    def test_release_publishes_three_selected_archives_from_verified_outputs(self) -> None:
        workflow = self.main_workflow()

        self.assertIn("tags: ['renpy/v*', 'rpgmaker/v*', 'zanzarah/v*', 'godot/v*']", workflow)
        self.assertIn("  packaging:\n", workflow)
        # Packaging waits on every real verification job directly, so a failed, skipped or
        # cancelled slice fails closed through GitHub's own `needs` instead of a bash gate.
        self.assertEqual(
            sorted(self.job_needs(self.job_blocks(workflow)["packaging"])),
            sorted(
                [
                    "source-checks",
                    "native-debug-x86", "native-debug-x64", "native-release-x86", "native-release-x64",
                    "msvc-analysis-x86", "msvc-analysis-x64", "clang-tidy-x86", "clang-tidy-x64",
                    "coverage-x86", "coverage-x64", "asan-x86", "asan-x64", "ubsan-x64",
                    "fuzz-x86", "fuzz-x64", "leaks-x64", "license-audit",
                ]
            ),
        )
        self.assertIn("  release:\n", workflow)
        self.assertIn("needs: packaging", workflow)
        self.assertIn("if: startsWith(github.ref, 'refs/tags/renpy/v')", workflow)
        self.assertIn("startsWith(github.ref, 'refs/tags/godot/v')", workflow)
        self.assertEqual(workflow.count("contents: write"), 1)

        # Packaging consumes each verified per-architecture Release directory without rebuilding.
        self.assertIn("name: release-x86", workflow)
        self.assertIn("name: release-x64", workflow)
        self.assertIn("path: out/native/bin/x86/Release", workflow)
        self.assertIn("path: out/native/bin/x64/Release", workflow)
        self.assertIn("./build.ps1 package -Output out/packages", workflow)
        self.assertNotIn("core.release", workflow)
        self.assertNotIn("packages.json", workflow)
        self.assertNotIn("release.json", workflow)

        # The release names exactly the three archives of the tagged module as literal paths and
        # publishes them in one `gh release create` with the human change log notes. There is no
        # glob, no count and no self-validation: a missing path is left to gh's own error.
        self.assertIn(
            'gh release create $env:GITHUB_REF_NAME --verify-tag --title "$module $version" --notes-file $notes @archives',
            workflow,
        )
        self.assertNotIn("--generate-notes", workflow)
        self.assertIn("GH_TOKEN: ${{ github.token }}", workflow)
        self.assertEqual(workflow.count("GH_TOKEN: ${{ github.token }}"), 1)
        self.assertIn("$env:GITHUB_REF_NAME.Split('/')[0]", workflow)
        self.assertIn("$env:GITHUB_REF_NAME.Split('/')[1]", workflow)
        self.assertIn("ChangeLog.en.txt", workflow)
        self.assertIn("ChangeLog.ru.txt", workflow)
        self.assertIn('"packages/$module-$version-bin-x64.zip"', workflow)
        self.assertIn('"packages/$module-$version-bin-x86.zip"', workflow)
        self.assertIn('"packages/$module-$version-pdb.zip"', workflow)
        self.assertNotIn("Get-ChildItem", workflow)
        self.assertNotIn("$archives.Count", workflow)
        self.assertNotIn("VERSION", workflow)

    def test_reusable_check_workflow_gates_tools_by_stage(self) -> None:
        check = self.check_workflow()

        # One maintained reusable workflow, driven only by the arch/stage inputs.
        self.assertIn("workflow_call", check)
        self.assertIn("./build.ps1 ci-source", check)
        self.assertIn("./build.ps1 ci-arch", check)
        self.assertIn("-Stage '${{ inputs.stage }}'", check)
        self.assertEqual(check.count("./build.ps1 ci-source"), 1)
        self.assertEqual(check.count("./build.ps1 ci-arch"), 2)

        # The build machinery (vcpkg baseline, uv, MSBuild side) is prepared only for a build or
        # source stage; leaks runs only the Python environment and the measurement tools.
        self.assertEqual(check.count("if: inputs.stage != 'leaks'"), 3)
        # vcpkg itself is a pinned prebuilt binary; the old in-tree bootstrap that would
        # rebuild and overwrite it must not run.
        self.assertNotIn("bootstrap-vcpkg.bat", check)
        self.assertIn("2a4ae1467b9e797964c60c919d057b9e5dc1ec03f4b9632c1150026db25201f2", check.lower())
        self.assertIn("uv sync --project build --frozen", check)
        self.assertIn("Cache vcpkg binaries", check)
        self.assertIn("uses: actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9", check)
        self.assertIn("id: runner-image", check)
        self.assertIn("${{ steps.runner-image.outputs.identity }}", check)

        # The source tools are installed only for the source stage.
        self.assertIn("if: inputs.stage == 'source'", check)
        self.assertIn("2.22", check)
        self.assertIn("3929162bab7e71d1b9b18708ec054fb0188bfc8ea1660979ed95837f5a9f547b", check.lower())
        self.assertIn("Cppcheck", check)
        self.assertIn("Install-Module PSScriptAnalyzer -RequiredVersion 1.25.0", check)

        # BinSkim is installed only for the native Release stage, the one stage that audits the
        # shipped binaries.
        self.assertIn("if: inputs.stage == 'native-release'", check)
        self.assertIn("Microsoft.CodeAnalysis.BinSkim -Version 4.4.9.11", check)

        # UMDH is provisioned only for the leak stage, and the stage downloads this run's tree.
        self.assertIn("if: inputs.stage == 'leaks'", check)
        self.assertIn("OptionId.WindowsDesktopDebuggers", check)
        self.assertIn(
            "https://download.microsoft.com/download/e119c04b-71aa-4067-ac3c-360c2e13d209/"
            "windowssdk/Installers/X64%20Debuggers%20And%20Tools-x64_en-us.msi",
            check,
        )
        self.assertIn("354173D844D5C061050EE2638AA94FAFB4835AC3DE836E220F6A74A992849A3B", check)
        self.assertIn("'10.0.19041.'", check)
        self.assertIn('"UNPACKRAT_UMDH=$umdh" | Add-Content -Path $env:GITHUB_ENV', check)
        self.assertIn("name: release-x64", check)
        self.assertIn("path: out/native/bin/x64/Release", check)
        # No cross-run or cached binaries: a plain same-run artifact download only.
        # The tool caches are restored early and saved before the build gates, so a failed
        # gate still leaves the verified tools cached for the next run.
        self.assertIn("actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9", check)
        self.assertIn("actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9", check)
        self.assertNotIn("out/cas", check)
        self.assertEqual(check.count("github/codeql-action/upload-sarif@"), 3)

    def test_code_scanning_publishes_existing_source_and_msvc_reports(self) -> None:
        check = self.check_workflow()
        self.assertIn("Sarif.Multitool --version 5.8.0", check)
        self.assertIn("inputs.stage == 'msvc-analysis'", check)
        self.assertIn("$sarif = Join-Path $env:RUNNER_TEMP 'sarif-tools/sarif.exe'", check)
        self.assertIn("& $sarif merge", check)
        self.assertIn("--recurse --merge-empty-logs", check)
        self.assertIn("out/native/obj-analysis/$arch/Release/*.sarif", check)
        self.assertIn('msvc-$arch.sarif', check)
        self.assertIn("category: msvc-analysis-${{ inputs.arch }}", check)
        self.assertIn("category: cppcheck-source", check)
        self.assertIn("sarif_file: out/native/analysis/cppcheck.sarif", check)
        self.assertIn("sarif_file: out/native/analysis/msvc-${{ inputs.arch }}.sarif", check)
        self.assertEqual(check.count("checkout_path: ${{ github.workspace }}"), 3)
        self.assertNotIn("continue-on-error:", check)
        self.assertNotIn("pull_request_target:", self.main_workflow())

    def test_failed_fuzz_stage_retains_existing_inputs_without_running_more_tools(self) -> None:
        check = self.check_workflow()
        marker = "      - name: Retain the fuzz failure inputs\n"
        self.assertEqual(check.count(marker), 1)
        step = check.split(marker, 1)[1].partition("\n      - name:")[0]
        self.assertIn("        if: failure() && inputs.stage == 'fuzz'\n", step)
        self.assertIn(
            "uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9", step)
        self.assertIn("name: fuzz-failure-${{ inputs.arch }}\n", step)
        self.assertIn(
            "          path: |\n"
            "            out/native/fuzz/${{ inputs.arch }}\n"
            "            crash-*\n"
            "            oom-*\n"
            "            timeout-*\n", step)
        self.assertIn("if-no-files-found: ignore\n", step)
        self.assertIn("retention-days: 14\n", step)
        self.assertNotIn("run:", step)
        self.assertNotIn("continue-on-error:", check)
        self.assertLess(check.index("      - name: Verify one stage for one architecture\n"),
                        check.index(marker))

    def test_only_approved_pinned_actions_are_used(self) -> None:
        workflow = self.all_workflows()

        actions = dict(re.findall(r"uses:\s+([^@\s]+)@([^\s#]+)", workflow))
        self.assertEqual(
            actions,
            {
                "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
                "astral-sh/setup-uv": "1c37ad07a6a961277cf70c0d37d6f313000f5884",
                "actions/cache/restore": "55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
                "actions/cache/save": "55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
                "actions/upload-artifact": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
                "actions/download-artifact": "9000827ccba6bdab643e8b6fd33ac0654aef8333",
                "codecov/codecov-action": "303a32d7a59b442fa8d48b6a1cc6825c09c847a5",
                "github/codeql-action/upload-sarif": "24c54180a607b1449ed407dd24f251e4e9147c8d",
            },
        )
        for action, revision in actions.items():
            with self.subTest(action=action):
                self.assertRegex(revision, r"^[0-9a-f]{40}$")
        self.assertIn("persist-credentials: false", workflow)
        self.assertEqual(workflow.count('version: "0.13.0"'), 3)
        pyproject = tomllib.loads((REPOSITORY / "build/pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["requires-python"], "==3.14.7")

    def test_release_display_name_is_the_module_and_version_for_every_module_tag(self) -> None:
        publish_step = self.main_workflow().split("      - name: Publish the module release\n", 1)[1]
        body = re.search(r"(?m)^        run: \|\n((?: {10}.*(?:\n|$))+)", publish_step)
        self.assertIsNotNone(body, "the publish step must contain a PowerShell run block")
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            "function gh {\n"
            "  ConvertTo-Json -InputObject @($args) -Compress | "
            "Set-Content -LiteralPath $env:GH_ARGS_FILE -Encoding utf8\n"
            "  $global:LASTEXITCODE = 0\n"
            "}\n"
            + textwrap.dedent(body[1])
        )
        for module in ("renpy", "rpgmaker", "zanzarah", "godot"):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as temporary:
                version = (REPOSITORY / "src/modules" / module / "VERSION").read_text(encoding="utf-8").strip()
                runner_temp = Path(temporary)
                script_path = runner_temp / "release.ps1"
                script_path.write_text(script, encoding="utf-8")
                args_path = runner_temp / "gh-args.json"
                completed = subprocess.run(
                    ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script_path)],
                    cwd=REPOSITORY,
                    env={
                        **os.environ,
                        "GITHUB_REF_NAME": f"{module}/v{version}",
                        "RUNNER_TEMP": temporary,
                        "GH_ARGS_FILE": str(args_path),
                    },
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
                recorded = json.loads(args_path.read_text(encoding="utf-8-sig"))
                self.assertEqual(
                    recorded,
                    [
                        "release", "create", f"{module}/v{version}", "--verify-tag",
                        "--title", f"{module} {version}", "--notes-file", str(runner_temp / "notes.md"),
                        f"packages/{module}-{version}-bin-x64.zip",
                        f"packages/{module}-{version}-bin-x86.zip",
                        f"packages/{module}-{version}-pdb.zip",
                    ],
                )

    def test_coverage_reports_upload_to_codecov_with_flags_and_oidc(self) -> None:
        check = self.check_workflow()

        # One reused upload step per report kind: the source stage publishes the python XML and
        # every coverage stage publishes its own native LCOV under a per-architecture flag.
        self.assertEqual(
            check.count("codecov/codecov-action@303a32d7a59b442fa8d48b6a1cc6825c09c847a5"),
            2,
        )
        self.assertIn("out/native/coverage/python/coverage.xml", check)
        self.assertIn("format('out/native/coverage/{0}/coverage.lcov', inputs.arch)", check)
        self.assertIn("flags: python", check)
        self.assertIn("flags: native-${{ inputs.arch }}", check)
        self.assertEqual(check.count("disable_search: true"), 2)
        self.assertEqual(check.count("fail_ci_if_error: true"), 2)
        # OIDC needs a job-scoped id-token permission on the caller, never on the reusable
        # workflow: the called workflow declares no permissions and inherits the caller grant,
        # while fork pull requests fall back to the tokenless path instead of assuming it.
        self.assertNotIn("permissions:", check)
        self.assertIn("id-token: write", self.main_workflow())
        self.assertEqual(check.count("use_oidc:"), 2)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", check)
        # Uploads still run after a coverage gate failure, but never from a missing report.
        self.assertIn("always()", check)
        self.assertIn("hashFiles(", check)
        # No custom comment bot and no secret-based fallback.
        self.assertNotIn("secrets.", self.all_workflows())
        self.assertNotIn("codecov-commenter", self.all_workflows())


class RepositoryMetadataTests(unittest.TestCase):
    """The repository's own manifests, readme and human ChangeLogs stay consistent."""

    MODULES = ("renpy", "rpgmaker", "zanzarah", "godot")
    UPSTREAM_PROJECT_URLS = (
        "https://github.com/nlohmann/json",
        "https://github.com/catchorg/Catch2",
        "https://github.com/lazyhamster/Observer",
        "https://github.com/Cyan4973/xxHash",
        "https://github.com/zlib-ng/zlib-ng",
        "https://github.com/luxrck/rgssad",
        "https://github.com/birkenfeld/serde-pickle",
        "https://github.com/Shizmob/rpatool",
        "https://github.com/pg83/ix",
    )

    def setUp(self) -> None:
        self.manifest = json.loads((REPOSITORY / "vcpkg.json").read_text(encoding="utf-8"))
        self.readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")

    def test_manifest_uses_the_lowercase_unpackrat_name_and_mit_license(self) -> None:
        self.assertEqual(self.manifest["name"], "unpackrat")
        self.assertEqual(self.manifest["license"], "MIT")

    def test_readme_declares_mit_and_the_observer_lgpl_api_header_exception(self) -> None:
        readme = self.readme
        self.assertTrue(readme.startswith("# unpackrat"), readme.splitlines()[0])
        self.assertIn("[![License: MIT](", readme)
        self.assertIn("(https://opensource.org/licenses/MIT)", readme)
        self.assertIn("src/api.h", readme)
        self.assertIn("LGPL-3.0", readme)
        self.assertIn("licenses/Observer.txt", readme)
        self.assertIn("licenses/GPL-3.0.txt", readme)

    def test_readme_omits_the_removed_format_references_section(self) -> None:
        self.assertNotIn("### Format references", self.readme)
        self.assertNotIn("zanzapak", self.readme.lower())

    def test_root_license_is_mit_and_the_gpl_is_the_companion_of_the_lgpl_header(self) -> None:
        license_text = (REPOSITORY / "LICENSE.txt").read_text(encoding="utf-8")
        self.assertTrue(license_text.startswith("MIT License"), license_text[:40])
        self.assertIn("Copyright (c) 2019-2026 Roman Kharitonov", license_text)
        self.assertIn("Permission is hereby granted, free of charge", license_text)
        gpl = (REPOSITORY / "licenses" / "GPL-3.0.txt").read_text(encoding="utf-8")
        self.assertIn("GNU GENERAL PUBLIC LICENSE", gpl)
        self.assertIn("Version 3, 29 June 2007", gpl)
        self.assertGreater(len(gpl.splitlines()), 500)
        observer = (REPOSITORY / "licenses" / "Observer.txt").read_text(encoding="utf-8")
        self.assertIn("GNU LESSER GENERAL PUBLIC LICENSE", observer)

    def test_readme_does_not_promise_a_7z_adapter_that_does_not_exist(self) -> None:
        self.assertNotIn("7z", self.readme)

    def test_readme_opens_with_a_lowercase_name_and_a_short_practical_intro(self) -> None:
        lines = self.readme.splitlines()
        self.assertEqual(lines[0], "# unpackrat", lines[0])
        first_heading = next(i for i, line in enumerate(lines) if line.startswith("## "))
        intro = [
            line for line in lines[1:first_heading]
            if line.strip() and not line.lstrip().startswith("[![")
        ]
        self.assertGreaterEqual(len(intro), 1)
        self.assertLessEqual(len(intro), 4)
        text = " ".join(intro).lower()
        self.assertIn("far manager", text)
        self.assertIn("self-contained", text)

    def test_readme_leads_with_download_before_license(self) -> None:
        readme = self.readme
        self.assertIn("## Download", readme)
        self.assertLess(readme.index("## Download"), readme.index("## License"))

    def test_readme_download_table_links_to_each_modules_published_releases(self) -> None:
        readme = self.readme
        for module in self.MODULES:
            with self.subTest(module=module):
                self.assertIn(f"https://github.com/refaim/unpackrat/releases?q={module})", readme)
        self.assertNotIn("refaim/ObserverModules", readme)
        self.assertIn("| Module | Formats | Release | ChangeLog |", readme)
        self.assertNotIn("| Version |", readme)
        self.assertNotIn("current sources build", readme)
        self.assertIn("newest published release", " ".join(readme.split()))

    def test_readme_ci_badge_and_link_use_the_canonical_repository(self) -> None:
        workflow_url = "https://github.com/refaim/unpackrat/actions/workflows/main.yml"
        self.assertIn(f"[![CI]({workflow_url}/badge.svg)]({workflow_url})", self.readme)

    def test_readme_documents_architecture_matched_install_and_browse_flow(self) -> None:
        readme = self.readme
        self.assertIn("%FARHOME%\\Plugins\\Observer\\modules", readme)
        self.assertIn("observer_user.ini", readme)
        self.assertIn("restart", readme.lower())
        for architecture in ("x86", "x64"):
            with self.subTest(architecture=architecture):
                self.assertIn(architecture, readme)

    def test_central_inventory_carries_every_upstream_identity_and_notice(self) -> None:
        # The reviewed component inventory, not a hand-written README table, is the single source
        # of upstream provenance and retained notices.
        inventory = json.loads(
            (REPOSITORY / "build" / "license-inventory.json").read_text(encoding="utf-8")
        )
        self.assertEqual(inventory["schema"], "unpackrat-license-inventory/3")
        self.assertIsInstance(inventory["components"], list)
        self.assertTrue(inventory["components"])
        urls = [
            str(component["source"].get("url"))
            for component in inventory["components"]
            if isinstance(component, dict) and isinstance(component.get("source"), dict)
        ]
        for project in self.UPSTREAM_PROJECT_URLS:
            with self.subTest(project=project):
                self.assertTrue(
                    any(url.startswith(project) for url in urls),
                    f"{project} is absent from the reviewed component inventory",
                )
        # Every retained notice named by the inventory must resolve to a real file.
        notices = {
            str(notice)
            for component in inventory["components"]
            if isinstance(component, dict)
            for notice in component.get("notices", [])
        } | {str(notice) for notice in inventory["rootPackage"].get("notices", [])}
        for notice in sorted(notices):
            with self.subTest(notice=notice):
                self.assertTrue((REPOSITORY / notice).is_file(), notice)

    def test_readme_states_the_install_prerequisites_and_needs_no_external_runtime_dlls(self) -> None:
        readme = self.readme
        self.assertIn("https://farmanager.com/download.php?l=en", readme)
        self.assertIn("https://github.com/lazyhamster/Observer/releases", readme)
        self.assertRegex(readme.lower(), r"no\s+external runtime dlls")

    def test_readme_scopes_the_zip_contents_to_each_module_zip(self) -> None:
        readme = self.readme
        self.assertIn("Each module ZIP", readme)
        self.assertNotIn("Every published archive", readme)

    def test_readme_matches_the_zip_architecture_to_the_far_observer_build(self) -> None:
        readme = self.readme
        download = " ".join(readme[readme.index("## Download"):readme.index("## Install")].split())
        self.assertIn("matches your FAR Manager and Observer build", download)
        self.assertNotIn("host architecture", readme)

    def test_build_documentation_states_the_toolchain_prerequisites(self) -> None:
        docs = (REPOSITORY / "docs" / "build-system.md").read_text(encoding="utf-8")
        requirements = (
            "Build Tools 2026", "v145", "LLVM 21.1.8", "Spectre", "Windows 11 SDK",
            "PowerShell 7.4", "uv", "vcpkg", "VCPKG_ROOT",
        )
        for requirement in requirements:
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, docs)

    def test_build_documentation_documents_the_coverage_reports_and_codecov(self) -> None:
        docs = (REPOSITORY / "docs" / "build-system.md").read_text(encoding="utf-8")
        for token in ("coverage.xml", "coverage.lcov", "codecov.yml", "Codecov", "OIDC", "flag"):
            with self.subTest(token=token):
                self.assertIn(token, docs)

    def test_readme_links_self_contained_outputs_to_the_build_documentation(self) -> None:
        readme = self.readme
        self.assertIn("docs/build-system.md", readme)
        self.assertIn("self-contained", readme.lower())

    def test_every_module_owns_bilingual_change_logs_matching_its_declared_version(self) -> None:
        readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        headers = {"en": "Version", "ru": "\u0412\u0435\u0440\u0441\u0438\u044f"}
        for module in self.MODULES:
            version = (REPOSITORY / "src" / "modules" / module / "VERSION").read_text(encoding="utf-8").strip()
            for language, header in headers.items():
                name = f"ChangeLog.{language}.txt"
                with self.subTest(module=module, language=language):
                    changelog = (REPOSITORY / "src" / "modules" / module / name).read_text(encoding="utf-8")
                    lines = changelog.splitlines()
                    self.assertEqual(lines[0], f"{header} {version}")
                    self.assertTrue(set(lines[1]) == {"-"})
                    self.assertTrue(any(line.lstrip().startswith(("*", "+")) for line in lines[2:]))
                    self.assertIn(f"{module}/{name}", readme)


class CodecovConfigTests(unittest.TestCase):
    """The Codecov policy keeps the local gates authoritative and shows the PR evidence."""

    def config(self) -> str:
        return (REPOSITORY / "codecov.yml").read_text(encoding="utf-8")

    def test_project_and_patch_targets_are_exact_100_with_zero_drift(self) -> None:
        text = self.config()
        self.assertEqual(text.count("target: 100%"), 2)
        self.assertEqual(text.count("threshold: 0%"), 2)
        self.assertIn("project:", text)
        self.assertIn("patch:", text)

    def test_comment_lists_diff_flags_and_files_for_base_less_prs(self) -> None:
        text = self.config()
        self.assertIn('layout: "diff, flags, files"', text)
        self.assertIn("require_changes: false", text)
        self.assertIn("require_base: false", text)
        self.assertIn("require_head: true", text)
        self.assertIn("hide_project_coverage: false", text)

    def test_flags_carry_no_stale_state_and_wait_for_all_three_uploads(self) -> None:
        text = self.config()
        self.assertNotIn("carryforward: true", text)
        for flag in ("python", "native-x86", "native-x64"):
            with self.subTest(flag=flag):
                self.assertIn(flag, text)
        self.assertIn("after_n_builds: 3", text)


if __name__ == "__main__":
    unittest.main()

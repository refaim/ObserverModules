from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest


REPOSITORY = Path(__file__).parents[2]
PYTHON = sys.executable
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def run_block(workflow: str, step_name: str) -> str:
    """Return the dedented ``run: |`` body of one named step, for a real execution fixture."""

    lines = workflow.splitlines()
    marker = f"- name: {step_name}"
    for index, line in enumerate(lines):
        if line.strip() == marker:
            break
    else:
        raise AssertionError(f"workflow step not found: {step_name}")
    for run in range(index + 1, len(lines)):
        head = lines[run]
        if head.strip() == "run: |":
            break
    else:
        raise AssertionError(f"workflow step has no run block: {step_name}")
    base = len(head) - len(head.lstrip(" "))
    body: list[str] = []
    for line in lines[run + 1:]:
        if line.strip() == "":
            body.append("")
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= base:
            break
        body.append(line[base + 2:])
    return "\n".join(body) + "\n"


def write_fake_tools(bindirectory: Path) -> None:
    """Install fake ``gh`` and ``uv`` shims so a workflow snippet can run with no network."""

    (bindirectory / "fake_gh.py").write_text(
        textwrap.dedent(
            """
            import json, os, sys

            argv = sys.argv[1:]
            with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as handle:
                handle.write(json.dumps(argv) + "\\n")
            joined = " ".join(argv)
            if "releases/tags/" in joined:
                sys.stdout.write(os.environ.get("FAKE_GH_PROBE_OUT", ""))
                sys.stderr.write(os.environ.get("FAKE_GH_PROBE_ERR", ""))
                raise SystemExit(int(os.environ.get("FAKE_GH_PROBE_EXIT", "0")))
            if "releases?" in joined:
                sys.stdout.write(os.environ.get("FAKE_GH_LIST_OUT", ""))
                raise SystemExit(int(os.environ.get("FAKE_GH_LIST_EXIT", "0")))
            if argv[:2] == ["release", "download"]:
                code = int(os.environ.get("FAKE_GH_DOWNLOAD_EXIT", "0"))
                files = json.loads(os.environ.get("FAKE_GH_DOWNLOAD_FILES", "{}"))
                if code == 0 and "--pattern" in argv:
                    pattern = argv[argv.index("--pattern") + 1]
                    if pattern in files:
                        directory = argv[argv.index("--dir") + 1]
                        os.makedirs(directory, exist_ok=True)
                        with open(os.path.join(directory, pattern), "w", encoding="utf-8") as handle:
                            handle.write(files[pattern])
                raise SystemExit(code)
            raise SystemExit(0)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (bindirectory / "fake_uv.py").write_text(
        textwrap.dedent(
            """
            import os, sys

            argv = sys.argv[1:]
            directory = argv[argv.index("--directory") + 1] if "--directory" in argv else "."
            rest = argv[argv.index("core.release") + 1:]
            os.chdir(os.path.abspath(directory))
            sys.path.insert(0, os.getcwd())
            from core.release import main

            raise SystemExit(main(rest))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (bindirectory / "gh.cmd").write_text(
        f'@echo off\r\n"{PYTHON}" "%~dp0fake_gh.py" %*\r\n', encoding="utf-8"
    )
    (bindirectory / "uv.cmd").write_text(
        f'@echo off\r\n"{PYTHON}" "%~dp0fake_uv.py" %*\r\n', encoding="utf-8"
    )


class WorkflowContractTests(unittest.TestCase):
    def workflow(self) -> str:
        return (REPOSITORY / ".github/workflows/main.yml").read_text(encoding="utf-8")

    def test_main_ci_verifies_sources_and_architectures_through_the_public_entry_point(self) -> None:
        workflow = self.workflow()

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
            [("source", "none"), ("x86", "x86"), ("x64", "x64"), ("arm64", "arm64")],
        )

        # The full doctor inventory demands every tool (including BinSkim and UMDH) and so cannot
        # run on the source or arm64 rows; each verify operation must discover what it needs.
        self.assertNotIn("./build.ps1 doctor", workflow)
        self.assertEqual(workflow.count("./build.ps1 verify-source"), 1)
        self.assertEqual(workflow.count("./build.ps1 verify-arch"), 1)
        # The local default de-prioritises the build for an interactive desktop; a hosted
        # runner has none to protect and must ask for the normal priority class explicitly.
        self.assertEqual(workflow.count("-Priority normal"), 2)
        self.assertNotIn("-PruneCas", workflow)
        self.assertNotIn("continue-on-error", workflow)

        # The content-addressed out/cas cache is gone; only the vcpkg binary cache remains.
        self.assertNotIn("path: out/cas", workflow)
        self.assertNotIn("actions/cache/restore@", workflow)
        self.assertNotIn("actions/cache/save@", workflow)
        self.assertNotIn("id: restore-build-cas", workflow)
        self.assertNotIn("cas-v1-", workflow)
        self.assertIn("Cache vcpkg binaries", workflow)
        self.assertIn("uses: actions/cache@", workflow)

        # Every quality gate stays behind the host-routing verify-arch command, never a
        # duplicated per-operation workflow step.
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

        self.assertIn("id: verify-source", workflow)
        self.assertIn("id: verify-arch", workflow)
        self.assertIn(
            "if: always() && (steps.verify-source.outcome != 'skipped' || "
            "steps.verify-arch.outcome != 'skipped')",
            workflow,
        )
        self.assertIn("/manifest.json", workflow)
        self.assertIn("/reports", workflow)
        self.assertIn("/logs", workflow)
        self.assertIn("if-no-files-found: error", workflow)
        self.assertNotIn("if-no-files-found: ignore", workflow)
        # GitHub's upload-artifact silently drops hidden files (including the coverage
        # data file reports/**/.coverage) unless the step opts in. Bind that opt-in to the
        # evidence upload step itself, so every declared report and log reaches the artifact.
        evidence_upload = next(
            block
            for block in workflow.split("- name: ")
            if block.startswith("Upload verification evidence")
        )
        self.assertIn("name: evidence-${{ matrix.job }}", evidence_upload)
        self.assertIn("include-hidden-files: true", evidence_upload)
        self.assertIn(
            "if: success() && github.event_name == 'push' && matrix.job != 'source'", workflow
        )
        self.assertIn("/packages", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)

        actions = dict(re.findall(r"uses:\s+([^@\s]+)@([^\s#]+)", workflow))
        self.assertEqual(
            actions,
            {
                "actions/checkout": "d23441a48e516b6c34aea4fa41551a30e30af803",
                "astral-sh/setup-uv": "08807647e7069bb48b6ef5acd8ec9567f424441b",
                "actions/cache": "caa296126883cff596d87d8935842f9db880ef25",
                "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
                "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
            },
        )
        for action, revision in actions.items():
            with self.subTest(action=action):
                self.assertRegex(revision, r"^[0-9a-f]{40}$")

        self.assertIn("persist-credentials: false", workflow)
        # Both CI jobs pin uv 0.13.0, a release verified to provision Python 3.14.7.
        self.assertEqual(workflow.count('version: "0.13.0"'), 2)
        pyproject = tomllib.loads(
            (REPOSITORY / "build/pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(pyproject["project"]["requires-python"], "==3.14.7")
        self.assertIn("id: runner-image", workflow)
        self.assertIn("${{ steps.runner-image.outputs.identity }}", workflow)
        self.assertIn('"TEMP=$env:RUNNER_TEMP" | Add-Content -Path $env:GITHUB_ENV', workflow)
        self.assertIn('"TMP=$env:RUNNER_TEMP" | Add-Content -Path $env:GITHUB_ENV', workflow)
        self.assertIn(
            "$baseline = (Get-Content -Raw -LiteralPath 'vcpkg.json' | "
            "ConvertFrom-Json).'builtin-baseline'",
            workflow,
        )
        self.assertIn("git -C $env:VCPKG_ROOT fetch --no-tags --depth=1 origin $baseline", workflow)
        self.assertIn("checkout --detach --force $baseline", workflow)
        self.assertIn("bootstrap-vcpkg.bat", workflow)

        # The source row installs only the source-check tools; the binary tools belong to the
        # architecture rows, and the debugging tools are provisioned on x64 alone.
        self.assertIn("choco install cppcheck --version=2.19.0", workflow)
        self.assertIn("C:\\Program Files\\Cppcheck", workflow)
        self.assertIn("Install-Module PSScriptAnalyzer -RequiredVersion 1.25.0", workflow)
        self.assertEqual(workflow.count("nuget install Microsoft.CodeAnalysis.BinSkim"), 1)
        self.assertIn("if: matrix.job != 'source'", workflow)
        self.assertIn("tools\\net9.0\\win-x64\\BinSkim.exe", workflow)
        self.assertNotIn("dotnet tool install --global Microsoft.CodeAnalysis.BinSkim", workflow)
        self.assertIn("$env:GITHUB_PATH", workflow)
        self.assertIn("if: matrix.job == 'x64'", workflow)
        self.assertIn("OptionId.WindowsDesktopDebuggers", workflow)
        self.assertIn(
            "https://download.microsoft.com/download/e119c04b-71aa-4067-ac3c-360c2e13d209/"
            "windowssdk/Installers/X64%20Debuggers%20And%20Tools-x64_en-us.msi",
            workflow,
        )
        self.assertIn("354173D844D5C061050EE2638AA94FAFB4835AC3DE836E220F6A74A992849A3B", workflow)
        self.assertIn(
            '$process = Start-Process -FilePath "$env:SystemRoot\\System32\\msiexec.exe"', workflow
        )
        self.assertIn("$arguments = @('/a', $msi, '/qn', '/norestart',", workflow)
        self.assertIn("'10.0.19041.'", workflow)
        self.assertIn("$gflags = Join-Path $debuggers 'gflags.exe'", workflow)
        self.assertIn('"UNPACKRAT_UMDH=$umdh" | Add-Content -Path $env:GITHUB_ENV', workflow)

        self.assertNotIn("upload-sarif", workflow)
        self.assertNotIn("codeql-action", workflow)

    def test_a_module_tag_release_promotes_verified_packages_behind_the_version_gate(self) -> None:
        workflow = self.workflow()

        # A module release is a manually pushed tag that names exactly one module lineage.
        self.assertIn("tags: ['renpy/v*', 'rpgmaker/v*', 'zanzarah/v*']", workflow)
        self.assertIn("  release:\n", workflow)
        self.assertIn("needs: verification", workflow)
        self.assertIn("if: startsWith(github.ref, 'refs/tags/renpy/v')", workflow)
        self.assertEqual(workflow.count("contents: write"), 1)
        self.assertEqual(workflow.count("uses: actions/download-artifact@"), 1)

        # The tag and the version gate are public build-tool commands, never workflow logic.
        self.assertEqual(workflow.count("python -m core.release tag"), 1)
        self.assertEqual(workflow.count("python -m core.release gate"), 1)
        self.assertIn("uv run --directory build --frozen --no-sync", workflow)
        self.assertIn("--module", workflow)
        self.assertIn("--tag", workflow)
        self.assertIn("--assets", workflow)
        self.assertIn("--previous", workflow)
        self.assertIn("packages.json", workflow)
        self.assertIn("release.json", workflow)
        self.assertIn("notes.md", workflow)
        # The published body is the gate's human notes, never auto-generated commit notes.
        self.assertIn("--notes-file", workflow)
        self.assertNotIn("--generate-notes", workflow)

        # `uv run --directory build` changes the working directory, so a relative `--repository .`
        # would validate build/src; every release command must pass an absolute workspace path.
        self.assertEqual(workflow.count('--repository "$env:GITHUB_WORKSPACE"'), 2)
        self.assertNotIn("--repository .", workflow)

        # A fresh publication of the same module must serialize before the lineage lookup, the
        # gate and the publish even when another module's release runs concurrently. A job-level
        # concurrency group cannot read step outputs, so it keys on the module in github.ref_name
        # and never cancels an already-running publication.
        self.assertEqual(workflow.count("cancel-in-progress: false"), 1)
        self.assertIn("group: release-${{ startsWith(github.ref_name, 'renpy/')", workflow)
        self.assertIn("startsWith(github.ref_name, 'rpgmaker/')", workflow)
        self.assertIn("|| 'zanzarah' }}", workflow)

        # The previous release is looked up by the same module lineage, never global latest, and
        # the search must page through every release instead of silently resetting older history.
        self.assertIn("python -m core.release previous", workflow)
        self.assertIn("gh api --paginate", workflow)
        self.assertNotIn("limit 200", workflow)
        # A rerun never reconstructs the lineage from the mutable history: it downloads the
        # immutable release.json published with the tag and reuses the predecessor recorded there,
        # while a new publication still lists and filters the published stable history.
        self.assertIn("--current-report", workflow)
        self.assertIn("--current-exists --current-report", workflow)
        self.assertIn("--pattern 'release.json'", workflow)
        self.assertIn("releases.jsonl", workflow)
        # Draft and prerelease entries are emitted and filtered by the lineage helper, so a
        # published stable lineage is never disturbed by an unpublished draft.
        self.assertIn(".draft", workflow)
        self.assertIn(".prerelease", workflow)
        self.assertIn("gh release download", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("steps.tag.outputs.module", workflow)
        self.assertIn("$env:GITHUB_REF_NAME", workflow)
        self.assertIn("GH_TOKEN: ${{ github.token }}", workflow)
        self.assertEqual(workflow.count("GH_TOKEN: ${{ github.token }}"), 2)
        # Every architecture publishes the same intermediate symbol bundle name, so only the
        # module archives are copied flat; the gate assembles the final bundle from the
        # per-architecture bundles left beside their own manifests. The published per-module
        # asset set is exactly three module archives and one symbol bundle.
        self.assertIn(
            "Where-Object { -not $_.Name.EndsWith('-pdb.zip', [StringComparison]::Ordinal) }",
            workflow,
        )
        self.assertIn("if ($packages.Count -ne 4) {", workflow)
        # Unknown listing/download failures must fail instead of looking like an absent release.
        self.assertIn("Unable to list", workflow)
        # The current tag's existence is queried exactly once, before lineage resolution; only a
        # confirmed 404 means it is absent, and the publish step reuses that recorded result.
        self.assertEqual(workflow.count("releases/tags/$tag"), 1)
        self.assertIn("HTTP 404", workflow)
        self.assertIn("steps.previous.outputs.exists", workflow)
        # Idempotent reruns compare published bytes instead of blindly replacing assets.
        self.assertIn("Get-FileHash", workflow)
        self.assertNotIn("VERSION", workflow)


def release_record(tag: str, *, draft: bool = False, prerelease: bool = False) -> str:
    return json.dumps({"tag": tag, "draft": draft, "prerelease": prerelease})


@unittest.skipUnless(POWERSHELL is not None, "PowerShell is required for the workflow fixture")
class WorkflowPreviousStepFixtureTests(unittest.TestCase):
    """Execute the real previous-release step with fake ``gh``/``uv`` and no network."""

    STEP_NAME = "Resolve the previous release of the same module"

    def setUp(self) -> None:
        self.workflow = (REPOSITORY / ".github/workflows/main.yml").read_text(encoding="utf-8")
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.bindirectory = self.root / "bin"
        self.bindirectory.mkdir()
        write_fake_tools(self.bindirectory)
        self.step = self.root / "previous-step.ps1"
        self.step.write_text(run_block(self.workflow, self.STEP_NAME), encoding="utf-8")
        self.log = self.root / "gh.log"
        self.output = self.root / "github-output.txt"
        self.output.write_text("", encoding="utf-8")
        (self.root / "runner").mkdir()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def run_previous_step(
        self,
        *,
        probe_exit: int,
        probe_out: str = "",
        probe_err: str = "",
        list_out: str = "",
        list_exit: int = 0,
        download_exit: int = 0,
        download_files: "dict[str, str] | None" = None,
        tag: str = "renpy/v3.1.0",
    ) -> "subprocess.CompletedProcess[str]":
        env = dict(os.environ)
        env["PATH"] = str(self.bindirectory) + os.pathsep + env["PATH"]
        env["FAKE_GH_LOG"] = str(self.log)
        env["FAKE_GH_PROBE_EXIT"] = str(probe_exit)
        env["FAKE_GH_PROBE_OUT"] = probe_out
        env["FAKE_GH_PROBE_ERR"] = probe_err
        env["FAKE_GH_LIST_OUT"] = list_out
        env["FAKE_GH_LIST_EXIT"] = str(list_exit)
        env["FAKE_GH_DOWNLOAD_EXIT"] = str(download_exit)
        env["FAKE_GH_DOWNLOAD_FILES"] = json.dumps(download_files or {})
        env["GITHUB_REF_NAME"] = tag
        env["GITHUB_OUTPUT"] = str(self.output)
        env["RUNNER_TEMP"] = str(self.root / "runner")
        env["GITHUB_WORKSPACE"] = str(REPOSITORY)
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(self.step)],
            cwd=str(REPOSITORY),
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )

    def gh_calls(self) -> list[list[str]]:
        if not self.log.is_file():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def downloads(self) -> list[str]:
        return [call[2] for call in self.gh_calls() if call[:2] == ["release", "download"]]

    def patterns(self) -> list[str]:
        return [
            call[call.index("--pattern") + 1]
            for call in self.gh_calls()
            if call[:2] == ["release", "download"] and "--pattern" in call
        ]

    def list_calls(self) -> list[list[str]]:
        return [call for call in self.gh_calls() if "releases?" in " ".join(call)]

    def outputs(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in self.output.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            values[key] = value
        return values

    @staticmethod
    def report(previous: str | None) -> str:
        return json.dumps(
            {
                "bootstrap": previous is None,
                "modules": [
                    {
                        "content": "a" * 64,
                        "module": "renpy",
                        "previous_version": previous,
                        "status": "bootstrap" if previous is None else "released",
                        "version": "3.1.0",
                    }
                ],
                "note": "",
                "title": "renpy 3.1.0",
            }
        )

    def test_a_new_release_lists_the_history_and_downloads_the_published_head(self) -> None:
        listing = (
            "\n".join(
                [
                    release_record("renpy/v3.2.0"),
                    release_record("renpy/v3.3.0", draft=True),
                    release_record("renpy/v3.1.0"),
                    release_record("renpy/v3.0.0"),
                ]
            )
            + "\n"
        )
        result = self.run_previous_step(
            probe_exit=1,
            probe_out='{\n  "message": "Not Found",\n  "status": "404"\n}\ngh: Not Found (HTTP 404)\n',
            list_out=listing,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        # A new publication resolves the latest published stable head and downloads its manifest;
        # the immutable report is only ever fetched for a rerun.
        self.assertEqual(self.downloads(), ["renpy/v3.2.0"])
        self.assertEqual(self.patterns(), ["packages.json"])
        self.assertTrue(self.list_calls())
        # The bare resolved tag is handed to gh; a field wrapper never leaks into the argument.
        arguments = " ".join(item for call in self.gh_calls() for item in call)
        self.assertNotIn("previous=", arguments)
        outputs = self.outputs()
        self.assertEqual(outputs["exists"], "false")
        self.assertTrue(outputs["manifest"].endswith("packages.json"))

    def test_a_rerun_downloads_the_immutable_report_and_its_recorded_predecessor(self) -> None:
        # The history would resolve a different predecessor; the recorded report must win and the
        # history must not even be listed.
        result = self.run_previous_step(
            probe_exit=0,
            probe_out="renpy/v3.1.0\n",
            list_out=release_record("renpy/v3.2.0") + "\n",
            download_files={"release.json": self.report("3.0.5"), "packages.json": "{}\n"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.downloads(), ["renpy/v3.1.0", "renpy/v3.0.5"])
        self.assertEqual(self.patterns(), ["release.json", "packages.json"])
        self.assertEqual(self.list_calls(), [])
        self.assertEqual(self.outputs()["exists"], "true")

    def test_a_bootstrap_rerun_records_no_predecessor_and_downloads_nothing_else(self) -> None:
        result = self.run_previous_step(
            probe_exit=0,
            probe_out="renpy/v3.1.0\n",
            download_files={"release.json": self.report(None)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.downloads(), ["renpy/v3.1.0"])
        outputs = self.outputs()
        self.assertEqual(outputs["exists"], "true")
        self.assertEqual(outputs["manifest"], "")

    def test_a_first_release_without_history_downloads_nothing(self) -> None:
        result = self.run_previous_step(
            probe_exit=1, probe_out="gh: Not Found (HTTP 404)\n", list_out=""
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.downloads(), [])
        outputs = self.outputs()
        self.assertEqual(outputs["exists"], "false")
        self.assertEqual(outputs["manifest"], "")

    def test_a_confirmed_404_with_a_json_body_is_absence(self) -> None:
        result = self.run_previous_step(
            probe_exit=1,
            probe_out='{"message": "Not Found"}\ngh: Not Found (HTTP 404)\n',
            list_out="",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.downloads(), [])

    def test_a_missing_published_report_is_fatal(self) -> None:
        result = self.run_previous_step(probe_exit=0, probe_out="renpy/v3.1.0\n")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unable to resolve the previous release", result.stderr)
        self.assertEqual(self.downloads(), ["renpy/v3.1.0"])

    def test_a_failed_report_download_is_fatal(self) -> None:
        result = self.run_previous_step(
            probe_exit=0, probe_out="renpy/v3.1.0\n", download_exit=1
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("readable release.json", result.stderr)
        self.assertEqual(self.downloads(), ["renpy/v3.1.0"])

    def test_a_failed_predecessor_manifest_download_is_fatal(self) -> None:
        result = self.run_previous_step(
            probe_exit=1,
            probe_out="gh: Not Found (HTTP 404)\n",
            list_out=release_record("renpy/v3.0.0") + "\n",
            download_exit=1,
        )

        self.assertNotEqual(result.returncode, 0)
        # Real PowerShell wraps and decorates the message between the two words, so the phrase
        # match is whitespace- and pipe-tolerant while the nonzero and lineage checks stay exact.
        self.assertRegex(result.stderr, r"readable\s+(?:\|\s*)?packages\.json")
        self.assertEqual(self.downloads(), ["renpy/v3.0.0"])

    def test_an_unknown_probe_failure_is_fatal(self) -> None:
        result = self.run_previous_step(
            probe_exit=1, probe_out="gh: Server Error (HTTP 500)\n", list_out=""
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unable to determine", result.stderr)
        self.assertEqual(self.downloads(), [])

    def test_a_listing_failure_is_fatal(self) -> None:
        result = self.run_previous_step(
            probe_exit=1, probe_out="gh: Not Found (HTTP 404)\n", list_exit=1
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unable to list", result.stderr)
        self.assertEqual(self.downloads(), [])


class RepositoryMetadataTests(unittest.TestCase):
    """The repository's own manifests, readme and human ChangeLogs stay consistent."""

    MODULES = ("renpy", "rpgmaker", "zanzarah")
    UPSTREAM_PROJECT_URLS = (
        "https://github.com/nlohmann/json",
        "https://github.com/catchorg/Catch2",
        "https://github.com/lazyhamster/Observer",
        "https://github.com/Cyan4973/xxHash",
        "https://zlib.net",
        "https://github.com/luxrck/rgssad",
        "https://github.com/birkenfeld/serde-pickle",
        "https://github.com/Shizmob/rpatool",
        "https://github.com/pg83/ix",
        "https://aluigi.altervista.org/papers.htm#others-file",
    )

    def setUp(self) -> None:
        self.manifest = json.loads((REPOSITORY / "vcpkg.json").read_text(encoding="utf-8"))
        self.readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")

    def test_manifest_uses_the_lowercase_unpackrat_name_and_mit_license(self) -> None:
        # The manifest declares the project's own MIT grant under its lowercase product name.
        self.assertEqual(self.manifest["name"], "unpackrat")
        self.assertEqual(self.manifest["license"], "MIT")

    def test_readme_declares_mit_and_the_observer_lgpl_api_header_exception(self) -> None:
        readme = self.readme
        self.assertTrue(readme.startswith("# unpackrat"), readme.splitlines()[0])
        self.assertIn("[![License: MIT](", readme)
        self.assertIn("(https://opensource.org/licenses/MIT)", readme)
        # The Observer API header stays a documented LGPLv3 exception to the project's MIT grant.
        self.assertIn("src/api.h", readme)
        self.assertIn("LGPL-3.0", readme)
        self.assertIn("licenses/Observer.txt", readme)
        self.assertIn("licenses/GPL-3.0.txt", readme)

    def test_readme_labels_zanzapak_as_a_format_reference_not_a_dependency(self) -> None:
        readme = self.readme
        self.assertIn("[GPL-2.0-or-later](licenses/zanzapak.txt)", readme)
        self.assertNotIn("[GPL-3.0](licenses/zanzapak.txt)", readme)
        # zanzapak is a format reference, not a shipped-code dependency of the project.
        self.assertIn("format reference", readme.lower())

    def test_root_license_is_mit_and_the_gpl_is_the_companion_of_the_lgpl_header(self) -> None:
        license_text = (REPOSITORY / "LICENSE.txt").read_text(encoding="utf-8")
        self.assertTrue(license_text.startswith("MIT License"), license_text[:40])
        self.assertIn("Copyright (c) 2019-2026 Roman Kharitonov", license_text)
        self.assertIn("Permission is hereby granted, free of charge", license_text)
        # The full GPLv3 stays alongside as the text the LGPL API header incorporates.
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
        # The intro before the first section is a few lines that state the plugin purpose and the
        # self-contained module binary, with the license/CI badges excluded from the prose count.
        first_heading = next(i for i, line in enumerate(lines) if line.startswith("## "))
        intro = [
            line
            for line in lines[1:first_heading]
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
        # A reader sees how to get the plugin before the license and build sections.
        self.assertLess(readme.index("## Download"), readme.index("## License"))

    def test_readme_download_table_names_declared_versions_and_release_lines(self) -> None:
        readme = self.readme
        for module, version in (("renpy", "3.1.0"), ("rpgmaker", "1.1.0"), ("zanzarah", "2.1.0")):
            with self.subTest(module=module):
                self.assertIn(version, readme)
                self.assertIn(
                    f"https://github.com/refaim/ObserverModules/releases?q={module}%2Fv",
                    readme,
                )

    def test_readme_documents_architecture_matched_install_and_browse_flow(self) -> None:
        readme = self.readme
        self.assertIn("%FARHOME%\\Plugins\\Observer\\modules", readme)
        self.assertIn("observer_user.ini", readme)
        self.assertIn("restart", readme.lower())
        for architecture in ("x86", "x64", "ARM64"):
            with self.subTest(architecture=architecture):
                self.assertIn(architecture, readme)

    def test_readme_keeps_every_upstream_notice_under_its_classification(self) -> None:
        readme = self.readme
        for project in self.UPSTREAM_PROJECT_URLS:
            with self.subTest(project=project):
                self.assertIn(project, readme)
        for heading in ("Third-party components", "Sources of inspiration", "Format references"):
            with self.subTest(heading=heading):
                self.assertIn(heading, readme)

    def test_readme_states_the_install_prerequisites_and_needs_no_external_runtime_dlls(self) -> None:
        readme = self.readme
        # The reader is pointed at the host to install first: FAR Manager and the Observer plugin host.
        self.assertIn("https://farmanager.com/download.php?l=en", readme)
        self.assertIn("https://github.com/lazyhamster/Observer/releases", readme)
        # The self-contained modules need no external runtime redistributable DLLs (wrap-tolerant).
        self.assertRegex(readme.lower(), r"no\s+external runtime dlls")

    def test_readme_scopes_the_zip_contents_to_each_module_zip(self) -> None:
        readme = self.readme
        # Only a module binary ZIP is described here; the sibling PDB bundles are not module archives.
        self.assertIn("Each module ZIP", readme)
        self.assertNotIn("Every published archive", readme)

    def test_readme_matches_the_zip_architecture_to_the_far_observer_build(self) -> None:
        readme = self.readme
        download = " ".join(
            readme[readme.index("## Download"):readme.index("## Install")].split()
        )
        # The download guidance ties the ZIP architecture to FAR Manager and Observer, not the host OS.
        self.assertIn("matches your FAR Manager and Observer build", download)
        self.assertNotIn("host architecture", readme)

    def test_build_documentation_states_the_toolchain_prerequisites(self) -> None:
        docs = (REPOSITORY / "docs" / "build-system.md").read_text(encoding="utf-8")
        requirements = (
            "Build Tools 2022",
            "v143",
            "ARM64",
            "Spectre",
            "Windows 11 SDK",
            "PowerShell 7.4",
            "uv",
            "vcpkg",
            "VCPKG_ROOT",
        )
        for requirement in requirements:
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, docs)

    def test_readme_links_self_contained_outputs_to_the_build_documentation(self) -> None:
        readme = self.readme
        self.assertIn("docs/build-system.md", readme)
        self.assertIn("self-contained", readme.lower())

    def test_every_module_owns_bilingual_change_logs_matching_its_declared_version(self) -> None:
        readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        headers = {"en": "Version", "ru": "\u0412\u0435\u0440\u0441\u0438\u044f"}
        for module in self.MODULES:
            version = (
                REPOSITORY / "src" / "modules" / module / "VERSION"
            ).read_text(encoding="utf-8").strip()
            for language, header in headers.items():
                name = f"ChangeLog.{language}.txt"
                with self.subTest(module=module, language=language):
                    changelog = (
                        REPOSITORY / "src" / "modules" / module / name
                    ).read_text(encoding="utf-8")
                    lines = changelog.splitlines()
                    self.assertEqual(lines[0], f"{header} {version}")
                    self.assertTrue(set(lines[1]) == {"-"})
                    self.assertTrue(any(line.lstrip().startswith(("*", "+")) for line in lines[2:]))
                    self.assertIn(f"{module}/{name}", readme)


if __name__ == "__main__":
    unittest.main()

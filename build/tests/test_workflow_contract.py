from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib
import unittest


REPOSITORY = Path(__file__).parents[2]


class WorkflowContractTests(unittest.TestCase):
    def workflow(self) -> str:
        return (REPOSITORY / ".github/workflows/main.yml").read_text(encoding="utf-8")

    def test_verification_matrix_preserves_the_required_check_names(self) -> None:
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
        self.assertEqual(matrix_rows, [("source", "none"), ("x86", "x86"), ("x64", "x64")])

        self.assertEqual(workflow.count("./build.ps1 ci-source"), 1)
        self.assertEqual(workflow.count("./build.ps1 ci-arch"), 1)
        self.assertNotIn("continue-on-error", workflow)

        # Only the vcpkg binary cache remains; there is no content-addressed build cache.
        self.assertNotIn("out/cas", workflow)
        self.assertNotIn("actions/cache/restore@", workflow)
        self.assertNotIn("actions/cache/save@", workflow)
        self.assertIn("Cache vcpkg binaries", workflow)
        self.assertIn("uses: actions/cache@", workflow)

        # The source row installs only the source tools; the x64 row provisions UMDH.
        self.assertIn("choco install cppcheck --version=2.19.0", workflow)
        self.assertIn("C:\\Program Files\\Cppcheck", workflow)
        self.assertIn("Install-Module PSScriptAnalyzer -RequiredVersion 1.25.0", workflow)
        self.assertIn("if: matrix.job == 'x64'", workflow)
        self.assertIn("OptionId.WindowsDesktopDebuggers", workflow)
        self.assertIn(
            "https://download.microsoft.com/download/e119c04b-71aa-4067-ac3c-360c2e13d209/"
            "windowssdk/Installers/X64%20Debuggers%20And%20Tools-x64_en-us.msi",
            workflow,
        )
        self.assertIn("354173D844D5C061050EE2638AA94FAFB4835AC3DE836E220F6A74A992849A3B", workflow)
        self.assertIn("'10.0.19041.'", workflow)
        self.assertIn('"UNPACKRAT_UMDH=$umdh" | Add-Content -Path $env:GITHUB_ENV', workflow)
        self.assertNotIn("upload-sarif", workflow)
        self.assertNotIn("codeql-action", workflow)

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
        self.assertEqual(workflow.count('version: "0.13.0"'), 2)
        pyproject = tomllib.loads((REPOSITORY / "build/pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["requires-python"], "==3.14.7")
        self.assertIn("id: runner-image", workflow)
        self.assertIn("${{ steps.runner-image.outputs.identity }}", workflow)
        self.assertIn("bootstrap-vcpkg.bat", workflow)

    def test_release_publishes_three_selected_archives_from_verified_outputs(self) -> None:
        workflow = self.workflow()

        self.assertIn("tags: ['renpy/v*', 'rpgmaker/v*', 'zanzarah/v*']", workflow)
        self.assertIn("  packaging:\n", workflow)
        self.assertIn("needs: verification", workflow)
        self.assertIn("  release:\n", workflow)
        self.assertIn("needs: packaging", workflow)
        self.assertIn("if: startsWith(github.ref, 'refs/tags/renpy/v')", workflow)
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
        self.assertIn("gh release create $env:GITHUB_REF_NAME --verify-tag --notes-file $notes @archives", workflow)
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

    def test_readme_labels_zanzapak_as_a_format_reference_not_a_dependency(self) -> None:
        readme = self.readme
        self.assertIn("[GPL-2.0-or-later](licenses/zanzapak.txt)", readme)
        self.assertNotIn("[GPL-3.0](licenses/zanzapak.txt)", readme)
        self.assertIn("format reference", readme.lower())

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

    def test_readme_download_table_names_declared_versions_and_release_lines(self) -> None:
        readme = self.readme
        for module, version in (("renpy", "3.1.0"), ("rpgmaker", "1.1.0"), ("zanzarah", "2.1.0")):
            with self.subTest(module=module):
                self.assertIn(version, readme)
                self.assertIn(f"https://github.com/refaim/ObserverModules/releases?q={module}%2Fv", readme)

    def test_readme_documents_architecture_matched_install_and_browse_flow(self) -> None:
        readme = self.readme
        self.assertIn("%FARHOME%\\Plugins\\Observer\\modules", readme)
        self.assertIn("observer_user.ini", readme)
        self.assertIn("restart", readme.lower())
        for architecture in ("x86", "x64"):
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
            "Build Tools 2022", "v143", "Spectre", "Windows 11 SDK",
            "PowerShell 7.4", "uv", "vcpkg", "VCPKG_ROOT",
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


if __name__ == "__main__":
    unittest.main()

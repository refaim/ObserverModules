from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest
import xml.etree.ElementTree as ET


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MSBUILD = "{http://schemas.microsoft.com/developer/msbuild/2003}"
sys.path.insert(0, str(REPOSITORY_ROOT / "build"))

from core.module_version import ModuleVersionError, parse_version  # noqa: E402


def _project(relative_path: str) -> ET.Element:
    return ET.parse(REPOSITORY_ROOT / relative_path).getroot()


class MSBuildContractsTests(unittest.TestCase):
    def test_direct_msbuild_fallback_stays_below_managed_work_root(self) -> None:
        project = _project("build/ObserverProject.props")

        artifacts_root = project.find(f".//{MSBUILD}ArtifactsRoot")
        self.assertIsNotNone(artifacts_root)
        self.assertEqual(
            artifacts_root.text,
            "$(RepositoryRoot)out\\work\\manual-msbuild\\",
        )

    def test_analysis_reports_have_stable_unique_project_paths(self) -> None:
        project = _project("build/ObserverProject.props")

        report_name = project.find(f".//{MSBUILD}ObserverAnalysisReportName")
        self.assertIsNotNone(report_name)
        self.assertEqual(report_name.text, "$(ProjectName)")
        self.assertEqual(
            report_name.get("Condition"), "'$(ObserverAnalysisReportName)' == ''"
        )

        report_logs = project.findall(f".//{MSBUILD}PREfastLog")
        self.assertIn(
            "$(ObserverAnalysisReportDirectory)\\$(PlatformMoniker)\\"
            "$(ObserverAnalysisReportName).sarif",
            (log.text for log in report_logs),
        )

    def test_coverage_links_the_architecture_specific_profile_runtime(self) -> None:
        project = _project("build/ObserverProject.props")

        names = {
            node.get("Condition"): node.text
            for node in project.findall(f".//{MSBUILD}ObserverProfileRuntime")
        }
        self.assertEqual(
            names,
            {
                "'$(Configuration)' == 'Coverage' And '$(Platform)' == 'Win32'":
                    "clang_rt.profile-i386.lib",
                "'$(Configuration)' == 'Coverage' And '$(Platform)' == 'x64'":
                    "clang_rt.profile-x86_64.lib",
            },
        )

        link = project.find(f".//{MSBUILD}ItemDefinitionGroup/{MSBUILD}Link")
        dependencies = [
            node.text for node in link.findall(f"{MSBUILD}AdditionalDependencies")
            if node.get("Condition") == "'$(ObserverProfileRuntime)' != ''"
        ]
        options = [
            node.text for node in link.findall(f"{MSBUILD}AdditionalOptions")
            if node.get("Condition") == "'$(ObserverProfileRuntime)' != ''"
        ]
        self.assertEqual(
            dependencies,
            ["$(LLVMRuntimeDir)\\$(ObserverProfileRuntime);%(AdditionalDependencies)"],
        )
        self.assertEqual(
            options,
            ["/NODEFAULTLIB:clang_rt.profile.lib %(AdditionalOptions)"],
        )

    def test_coverage_rejects_a_manual_build_without_a_usable_profile_runtime(self) -> None:
        project = _project("build/ObserverProject.props")
        validation = project.findall(
            f".//{MSBUILD}Target[@Name='ValidateObserverProfileRuntime']/{MSBUILD}Error"
        )

        self.assertEqual(len(validation), 2)
        self.assertEqual(
            validation[0].get("Condition"),
            "'$(ObserverCompileAnalysis)' != 'true' And "
            "'$(ObserverProfileRuntime)' != '' And '$(LLVMRuntimeDir)' == ''",
        )
        self.assertIn("build.ps1 test-coverage", validation[0].get("Text"))
        self.assertEqual(
            validation[1].get("Condition"),
            "'$(ObserverCompileAnalysis)' != 'true' And "
            "'$(Configuration)' == 'Coverage' And '$(ObserverProfileRuntime)' == ''",
        )
        self.assertIn("Win32 and x64 only", validation[1].get("Text"))

    def test_fuzz_links_the_architecture_specific_libfuzzer_runtime(self) -> None:
        project = _project("build/ObserverFuzz.props")

        suffixes = {
            node.get("Condition"): node.text
            for node in project.findall(f".//{MSBUILD}ObserverFuzzRuntimeSuffix")
        }
        self.assertEqual(
            suffixes,
            {
                "'$(Configuration)' == 'Fuzz' And '$(Platform)' == 'Win32'": "i386",
                "'$(Configuration)' == 'Fuzz' And '$(Platform)' == 'x64'": "x86_64",
            },
        )

        link = project.find(f".//{MSBUILD}ItemDefinitionGroup/{MSBUILD}Link")
        dependencies = link.find(f"{MSBUILD}AdditionalDependencies").text
        options = link.find(f"{MSBUILD}AdditionalOptions").text
        # No architecture may remain hard-coded now that Fuzz|Win32 links the same way.
        self.assertNotIn("x86_64", dependencies)
        self.assertNotIn("x86_64", options)
        for library in ("clang_rt.asan", "clang_rt.asan_cxx"):
            self.assertIn(
                f"$(LLVMRuntimeDir)\\{library}-$(ObserverFuzzRuntimeSuffix).lib;",
                dependencies,
            )
        self.assertTrue(dependencies.endswith(";%(AdditionalDependencies)"))
        for library in ("clang_rt.fuzzer", "clang_rt.asan", "clang_rt.asan_cxx"):
            self.assertIn(
                f'/WHOLEARCHIVE:"$(LLVMRuntimeDir)\\{library}-$(ObserverFuzzRuntimeSuffix).lib"',
                options,
            )
        self.assertIn("/INFERASANLIBS:NO", options)

    def test_fuzz_win32_keeps_msvc_exception_unwinding_under_asan(self) -> None:
        project = _project("build/ObserverFuzz.props")

        options = project.findall(
            f".//{MSBUILD}ItemDefinitionGroup/{MSBUILD}ClCompile/{MSBUILD}AdditionalOptions"
        )
        self.assertEqual(len(options), 1)
        self.assertEqual(
            options[0].get("Condition"),
            "'$(Configuration)' == 'Fuzz' And '$(Platform)' == 'Win32'",
        )
        self.assertEqual(
            options[0].text,
            "/clang:-mllvm /clang:-asan-stack-dynamic-alloca=0 %(AdditionalOptions)",
        )

    def test_fuzz_validation_allows_only_win32_and_x64_fuzz_or_compile_analysis(self) -> None:
        project = _project("build/ObserverFuzz.props")
        validation = project.findall(
            f".//{MSBUILD}Target[@Name='ValidateFuzzConfiguration']/{MSBUILD}Error"
        )

        self.assertEqual(len(validation), 2)
        self.assertEqual(
            validation[0].get("Condition"),
            "'$(ObserverCompileAnalysis)' != 'true' And "
            "'$(ObserverFuzzRuntimeSuffix)' == ''",
        )
        self.assertIn("Fuzz|Win32 and Fuzz|x64", validation[0].get("Text"))
        self.assertEqual(
            validation[1].get("Condition"),
            "'$(ObserverCompileAnalysis)' != 'true' And '$(LLVMRuntimeDir)' == ''",
        )
        self.assertIn("build.ps1 fuzz", validation[1].get("Text"))

    def test_props_own_no_compile_analysis_manifest_plumbing(self) -> None:
        # Direct MSBuild names the compiler manifests itself, so the props must not keep the
        # retired per-file manifest target or the build-graph override hooks it keyed on.
        for relative_path in ("build/ObserverProject.props", "build/ObserverFuzz.props"):
            with self.subTest(path=relative_path):
                text = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotRegex(text, r"(?i)\bgraph\b", "no retired build-graph wording")

        project = _project("build/ObserverProject.props")
        targets = [node.get("Name") for node in project.findall(f".//{MSBUILD}Target")]
        self.assertNotIn("ObserverNameCompilerManifests", targets)
        self.assertIsNone(project.find(f".//{MSBUILD}ObserverManifestName"))

        text = (REPOSITORY_ROOT / "build/ObserverProject.props").read_text(encoding="utf-8")
        for retired in (
            "ObserverNameCompilerManifests",
            "ObserverManifestName",
            "sourceDependencies",
            "clang:-MJ",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, text)

    def test_module_version_props_derive_every_macro_from_the_version_file(self) -> None:
        project = _project("build/ObserverModuleVersion.props")

        version_file = project.find(f".//{MSBUILD}ObserverModuleVersionFile")
        self.assertIsNotNone(version_file)
        self.assertEqual(
            version_file.text,
            "$(RepositoryRoot)src\\modules\\$(ObserverModuleName)\\VERSION",
        )
        raw = project.find(f".//{MSBUILD}ObserverModuleVersionRaw")
        self.assertIsNotNone(raw)
        self.assertEqual(
            raw.text,
            "$([System.IO.File]::ReadAllText('$(ObserverModuleVersionFile)'))",
        )
        for component, member in (("Major", "Major"), ("Minor", "Minor"), ("Patch", "Build")):
            with self.subTest(component=component):
                node = project.find(f".//{MSBUILD}ObserverModuleVersion{component}")
                self.assertIsNotNone(node)
                self.assertEqual(
                    node.text,
                    f"$([System.Version]::Parse('$(ObserverModuleVersion)').{member})",
                )
                self.assertEqual(
                    node.get("Condition"), "'$(ObserverModuleVersionValid)' == 'true'"
                )

        definitions = [
            node.text
            for node in project.findall(f".//{MSBUILD}PreprocessorDefinitions")
        ]
        self.assertEqual(len(definitions), 2)
        for text in definitions:
            with self.subTest(text=text):
                for macro in ("MAJOR", "MINOR", "PATCH"):
                    self.assertIn(f"OBSERVER_MODULE_VERSION_{macro}=$(ObserverModuleVersion", text)
                self.assertTrue(text.endswith(";%(PreprocessorDefinitions)"))
        resource_definitions = project.find(
            f".//{MSBUILD}ResourceCompile/{MSBUILD}PreprocessorDefinitions"
        )
        self.assertIsNotNone(resource_definitions)
        for macro in (
            "OBSERVER_MODULE_NAME=$(ObserverModuleName)",
            "OBSERVER_MODULE_FILE=$(ObserverModuleName).so",
            "OBSERVER_MODULE_VERSION=$(ObserverModuleVersion)",
        ):
            self.assertIn(f"{macro};", resource_definitions.text)

        resource = project.find(f".//{MSBUILD}ItemGroup/{MSBUILD}ResourceCompile")
        self.assertIsNotNone(resource)
        self.assertEqual(resource.get("Include"), "$(RepositoryRoot)src\\modules\\version.rc")
        item_group = project.find(f".//{MSBUILD}ItemGroup[{MSBUILD}ResourceCompile]")
        self.assertEqual(
            item_group.get("Condition"), "'$(ObserverConfigurationType)' == 'DynamicLibrary'"
        )

        # MSBuild and core.module_version.parse_version must accept exactly the same versions.
        valid = project.find(f".//{MSBUILD}ObserverModuleVersionValid")
        self.assertIsNotNone(valid)
        self.assertEqual(
            valid.text,
            "$([System.Text.RegularExpressions.Regex]::IsMatch("
            "'$(ObserverModuleVersion)', "
            "'^(0|[1-9][0-9]{0,4})\\.(0|[1-9][0-9]{0,4})\\.(0|[1-9][0-9]{0,4})$'))",
        )
        pattern = re.compile(re.search(r"'(\^\(0\|.*\$)'", valid.text).group(1))
        for version in ("3.1.0", "0.0.0", "65535.65535.65535"):
            with self.subTest(version=version):
                self.assertIsNotNone(pattern.fullmatch(version))
                self.assertEqual(parse_version(version), tuple(
                    int(part) for part in version.split(".")
                ))
        for version in ("03.1.0", "3.1", "3.1.0.0", "3.1.0-rc1", "", "999999.0.0"):
            with self.subTest(version=version):
                self.assertIsNone(pattern.fullmatch(version))
                with self.assertRaises(ModuleVersionError):
                    parse_version(version)

        errors = project.findall(
            f".//{MSBUILD}Target[@Name='ValidateObserverModuleVersion']/{MSBUILD}Error"
        )
        self.assertEqual(
            [node.get("Condition") for node in errors],
            [
                "'$(ObserverModuleName)' == ''",
                "'$(ObserverModuleVersionValid)' != 'true'",
                "'$(ObserverModuleVersionValid)' == 'true' And ("
                "$(ObserverModuleVersionMajor) > 65535 Or "
                "$(ObserverModuleVersionMinor) > 65535 Or "
                "$(ObserverModuleVersionPatch) > 65535)",
            ],
        )

    def test_every_project_compiling_a_module_declares_its_version_identity(self) -> None:
        for project_name, module in (
            ("renpy", "renpy"),
            ("rpgmaker", "rpgmaker"),
            ("zanzarah", "zanzarah"),
            ("fuzz-renpy", "renpy"),
            ("fuzz-rpgmaker", "rpgmaker"),
            ("fuzz-zanzarah", "zanzarah"),
        ):
            with self.subTest(project=project_name):
                project = _project(f"build/projects/{project_name}.vcxproj")
                imports = [node.get("Project") for node in project.findall(f"{MSBUILD}Import")]
                self.assertIn("..\\ObserverModuleVersion.props", imports)
                self.assertLess(
                    imports.index("..\\ObserverProject.props"),
                    imports.index("..\\ObserverModuleVersion.props"),
                )
                name = project.find(f".//{MSBUILD}ObserverModuleName")
                self.assertIsNotNone(name)
                self.assertEqual(name.text, module)

    def test_projects_without_module_sources_stay_out_of_the_version_contract(self) -> None:
        for project_name in ("tests", "leak-probe", "fuzz-pickle"):
            with self.subTest(project=project_name):
                project = _project(f"build/projects/{project_name}.vcxproj")
                imports = [node.get("Project") for node in project.findall(f"{MSBUILD}Import")]
                self.assertNotIn("..\\ObserverModuleVersion.props", imports)
                self.assertIsNone(project.find(f".//{MSBUILD}ObserverModuleName"))

    def test_leak_probe_is_shipping_x64_release_with_release_zlib(self) -> None:
        project = _project("build/projects/leak-probe.vcxproj")

        runtimes = [
            node.text for node in project.findall(f".//{MSBUILD}RuntimeLibrary")
        ]
        dependencies = [
            node.text for node in project.findall(f".//{MSBUILD}AdditionalDependencies")
        ]
        validation = project.find(
            f".//{MSBUILD}Target[@Name='ValidateLeakProbeConfiguration']/{MSBUILD}Error"
        )

        self.assertEqual(runtimes, ["MultiThreaded"])
        self.assertEqual(dependencies, ["zs.lib;%(AdditionalDependencies)"])
        self.assertIsNotNone(validation)
        self.assertEqual(
            validation.get("Condition"),
            "'$(Configuration)|$(Platform)' != 'Release|x64'",
        )


if __name__ == "__main__":
    unittest.main()

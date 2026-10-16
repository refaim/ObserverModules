from __future__ import annotations

from pathlib import Path
import json
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MSBUILD = "{http://schemas.microsoft.com/developer/msbuild/2003}"


def _project(relative_path: str) -> ET.Element:
    return ET.parse(REPOSITORY_ROOT / relative_path).getroot()


class MSBuildContractsTests(unittest.TestCase):
    def test_xp_subsystem_entry_and_static_runtime_reach_evaluated_link_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "xp.proj"
            props = (REPOSITORY_ROOT / "build/unpackrat-vista.props").as_posix()
            project.write_text(
                f'<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">'
                f'<Import Project="{props}" />'
                '<ItemGroup><Link Include="fixture" /></ItemGroup></Project>', encoding="utf-8",
            )
            scenarios = (
                ("Debug", (), False), ("Release", (), True),
                ("Release", ("-p:unpackratEnableClangTidy=true", "-p:unpackratRunCodeAnalysis=true"), True),
                ("Release", ("-p:unpackratEnableClangTidy=true", "-p:unpackratStandaloneClangTidy=true"), False),
                ("Release", ("-p:unpackratCompileAnalysis=true",), False),
                ("ASan", (), False), ("Coverage", (), False), ("Fuzz", (), False), ("UBSan", (), False),
            )
            for configuration, flags, enabled in scenarios:
                for kind, subsystem in (("DynamicLibrary", "Windows"), ("Application", "Console")):
                    for platform, moniker, minimum, windows in (("Win32", "x86", "5.01", "5.1.2600.0"),
                                                               ("x64", "x64", "5.02", "5.2.3790.1830")):
                        with self.subTest(configuration=configuration, flags=flags, kind=kind, platform=platform):
                            result = subprocess.run(
                                [str(main._find_msbuild()), str(project), "-nologo", "-getItem:Link",
                                 f"-p:Configuration={configuration}", f"-p:ConfigurationType={kind}",
                                 f"-p:Platform={platform}", f"-p:PlatformMoniker={moniker}",
                                 "-getProperty:unpackratVistaCompatibility,SupportLTL,WindowsTargetPlatformMinVersion", *flags],
                                capture_output=True, text=True, check=True,
                            )
                            evaluated = json.loads(result.stdout)
                            metadata = evaluated["Items"]["Link"][0]
                            self.assertEqual((metadata.get("SubSystem"), metadata.get("MinimumRequiredVersion")),
                                             (subsystem, minimum) if enabled else (None, None))
                            self.assertEqual(evaluated["Properties"]["unpackratVistaCompatibility"], "true" if enabled else "")
                            self.assertEqual(evaluated["Properties"]["SupportLTL"], "")
                            self.assertEqual(evaluated["Properties"]["WindowsTargetPlatformMinVersion"], windows if enabled else "")
                            if enabled:
                                self.assertIn(f"objs\\{moniker}\\YY_Thunks_for_WinXP.obj", metadata["AdditionalDependencies"])
                            if enabled and kind == "DynamicLibrary":
                                self.assertEqual(metadata["EntryPointSymbol"], "DllMainCRTStartupForYY_Thunks")
                                alias = ("_YY_ThunksOriginalDllMainCRTStartup@12=__DllMainCRTStartup@12"
                                         if platform == "Win32" else "YY_ThunksOriginalDllMainCRTStartup=_DllMainCRTStartup")
                                self.assertIn("/alternatename:" + alias, metadata["AdditionalOptions"])
                            else:
                                self.assertNotIn("EntryPointSymbol", metadata)
                                self.assertNotIn("AdditionalOptions", metadata)

    def test_configuration_props_pin_the_retained_windows_sdk_family(self) -> None:
        # A freshly installed 10.0.28000 SDK must not retarget the production and instrumented
        # builds: the configuration props keep the existing 10.0.26100.0 family explicitly.
        project = _project("build/unpackrat-configuration.props")
        version = project.find(f".//{MSBUILD}WindowsTargetPlatformVersion")
        self.assertIsNotNone(version)
        self.assertEqual(version.text, "10.0.26100.0")

    def test_common_compile_definitions_carry_the_vista_window_version_pair(self) -> None:
        # The Vista minimums WINVER and _WIN32_WINNT must reach the compiler through the
        # shared MSBuild toolchain, not through a reserved-identifier macro declared in
        # api.h, and the inherited %(PreprocessorDefinitions) must stay appended.
        project = _project("build/unpackrat-project.props")
        definitions = project.find(
            f".//{MSBUILD}ItemDefinitionGroup/{MSBUILD}ClCompile/"
            f"{MSBUILD}PreprocessorDefinitions"
        )
        self.assertIsNotNone(definitions)
        self.assertEqual(
            definitions.text,
            "NOMINMAX;WINVER=0x0600;_WIN32_WINNT=0x0600;%(PreprocessorDefinitions)",
        )

    def test_direct_msbuild_fallback_stays_below_managed_work_root(self) -> None:
        project = _project("build/unpackrat-project.props")

        artifacts_root = project.find(f".//{MSBUILD}ArtifactsRoot")
        self.assertIsNotNone(artifacts_root)
        self.assertEqual(
            artifacts_root.text,
            "$(RepositoryRoot)out\\work\\manual-msbuild\\",
        )

    def test_analysis_reports_have_stable_unique_project_paths(self) -> None:
        project = _project("build/unpackrat-project.props")

        report_name = project.find(f".//{MSBUILD}unpackratAnalysisReportName")
        self.assertIsNotNone(report_name)
        self.assertEqual(report_name.text, "$(ProjectName)")
        self.assertEqual(
            report_name.get("Condition"), "'$(unpackratAnalysisReportName)' == ''"
        )

        report_logs = project.findall(f".//{MSBUILD}PREfastLog")
        self.assertIn(
            "$(unpackratAnalysisReportDirectory)\\$(PlatformMoniker)\\"
            "$(unpackratAnalysisReportName).sarif",
            (log.text for log in report_logs),
        )

    def test_coverage_links_the_architecture_specific_profile_runtime(self) -> None:
        project = _project("build/unpackrat-project.props")

        names = {
            node.get("Condition"): node.text
            for node in project.findall(f".//{MSBUILD}unpackratProfileRuntime")
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
            if node.get("Condition") == "'$(unpackratProfileRuntime)' != ''"
        ]
        options = [
            node.text for node in link.findall(f"{MSBUILD}AdditionalOptions")
            if node.get("Condition") == "'$(unpackratProfileRuntime)' != ''"
        ]
        self.assertEqual(
            dependencies,
            ["$(LLVMRuntimeDir)\\$(unpackratProfileRuntime);%(AdditionalDependencies)"],
        )
        self.assertEqual(
            options,
            ["/NODEFAULTLIB:clang_rt.profile.lib %(AdditionalOptions)"],
        )

    def test_coverage_rejects_a_manual_build_without_a_usable_profile_runtime(self) -> None:
        project = _project("build/unpackrat-project.props")
        validation = project.findall(
            f".//{MSBUILD}Target[@Name='ValidateunpackratProfileRuntime']/{MSBUILD}Error"
        )

        self.assertEqual(len(validation), 2)
        self.assertEqual(
            validation[0].get("Condition"),
            "'$(unpackratCompileAnalysis)' != 'true' And "
            "'$(unpackratProfileRuntime)' != '' And '$(LLVMRuntimeDir)' == ''",
        )
        self.assertIn("build.ps1 test-coverage", validation[0].get("Text"))
        self.assertEqual(
            validation[1].get("Condition"),
            "'$(unpackratCompileAnalysis)' != 'true' And "
            "'$(Configuration)' == 'Coverage' And '$(unpackratProfileRuntime)' == ''",
        )
        self.assertIn("Win32 and x64 only", validation[1].get("Text"))

    def test_fuzz_links_the_architecture_specific_libfuzzer_runtime(self) -> None:
        project = _project("build/unpackrat-fuzz.props")

        suffixes = {
            node.get("Condition"): node.text
            for node in project.findall(f".//{MSBUILD}unpackratFuzzRuntimeSuffix")
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
        for library in ("clang_rt.asan_dynamic", "clang_rt.asan_static_runtime_thunk"):
            self.assertIn(
                f"$(LLVMRuntimeDir)\\{library}-$(unpackratFuzzRuntimeSuffix).lib;",
                dependencies,
            )
        self.assertTrue(dependencies.endswith(";%(AdditionalDependencies)"))
        for library in ("clang_rt.fuzzer", "clang_rt.asan_static_runtime_thunk"):
            self.assertIn(
                f'/WHOLEARCHIVE:"$(LLVMRuntimeDir)\\{library}-$(unpackratFuzzRuntimeSuffix).lib"',
                options,
            )
        self.assertIn("/INFERASANLIBS:NO", options)
        self.assertNotIn("clang_rt.asan_cxx-", dependencies)

    def test_release_compatibility_uses_only_the_source_built_xp_object(self) -> None:
        project = _project("build/unpackrat-vista.props")
        group = project.find(f"{MSBUILD}PropertyGroup")
        self.assertEqual(group.get("Condition"),
                         "'$(Configuration)' == 'Release' And '$(unpackratCompileAnalysis)' != 'true' And '$(unpackratStandaloneClangTidy)' != 'true'")
        self.assertEqual(group.find(f"{MSBUILD}unpackratVistaCompatibility").text, "true")
        self.assertEqual(project.findall(f"{MSBUILD}Import"), [])
        self.assertIsNone(project.find(f".//{MSBUILD}SupportLTL"))
        link = project.find(f".//{MSBUILD}Link")
        self.assertEqual(link.find(f"{MSBUILD}AdditionalDependencies").text,
                         "$(unpackratYYThunksObject);%(AdditionalDependencies)")
        validation = project.find(f"{MSBUILD}Target")
        self.assertEqual(validation.get("Condition"), "'$(unpackratVistaCompatibility)' == 'true'")
        errors = validation.findall(f"{MSBUILD}Error")
        self.assertEqual(len(errors), 1)
        self.assertIn("YY-Thunks XP object", errors[0].get("Text"))
        common = _project("build/unpackrat-project.props")
        self.assertEqual(common.findall(f"{MSBUILD}Import")[-1].get("Project"),
                         "$(MSBuildThisFileDirectory)unpackrat-vista.props")

    def test_manifest_pins_source_thunks_without_vc_ltl(self) -> None:
        manifest = json.loads((REPOSITORY_ROOT / "vcpkg.json").read_text())
        dependencies = {item["name"]: item.get("version>=") for item in manifest["dependencies"]}
        self.assertNotIn("vc-ltl5", dependencies)
        self.assertFalse((REPOSITORY_ROOT / "build/vcpkg/ports/vc-ltl5").exists())
        self.assertFalse((REPOSITORY_ROOT / "licenses/VC-LTL5.txt").exists())
        self.assertEqual(dependencies["yy-thunks"], "1.2.3")
        metadata = json.loads((REPOSITORY_ROOT / "build/vcpkg/ports/yy-thunks/vcpkg.json").read_text())
        self.assertEqual((metadata["version"], metadata["port-version"], metadata["license"]), ("1.2.3", 1, "MIT"))

    def test_fuzz_win32_keeps_msvc_exception_unwinding_under_asan(self) -> None:
        project = _project("build/unpackrat-fuzz.props")

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
        project = _project("build/unpackrat-fuzz.props")
        validation = project.findall(
            f".//{MSBUILD}Target[@Name='ValidateFuzzConfiguration']/{MSBUILD}Error"
        )

        self.assertEqual(len(validation), 2)
        self.assertEqual(
            validation[0].get("Condition"),
            "'$(unpackratCompileAnalysis)' != 'true' And "
            "'$(unpackratFuzzRuntimeSuffix)' == ''",
        )
        self.assertIn("Fuzz|Win32 and Fuzz|x64", validation[0].get("Text"))
        self.assertEqual(
            validation[1].get("Condition"),
            "'$(unpackratCompileAnalysis)' != 'true' And '$(LLVMRuntimeDir)' == ''",
        )
        self.assertIn("build.ps1 fuzz", validation[1].get("Text"))

    def test_props_own_no_compile_analysis_manifest_plumbing(self) -> None:
        # Direct MSBuild names the compiler manifests itself, so the props must not keep the
        # retired per-file manifest target or the build-graph override hooks it keyed on.
        for relative_path in ("build/unpackrat-project.props", "build/unpackrat-fuzz.props"):
            with self.subTest(path=relative_path):
                text = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotRegex(text, r"(?i)\bgraph\b", "no retired build-graph wording")

        project = _project("build/unpackrat-project.props")
        targets = [node.get("Name") for node in project.findall(f".//{MSBUILD}Target")]
        self.assertNotIn("unpackratNameCompilerManifests", targets)
        self.assertIsNone(project.find(f".//{MSBUILD}unpackratManifestName"))

        text = (REPOSITORY_ROOT / "build/unpackrat-project.props").read_text(encoding="utf-8")
        for retired in (
            "unpackratNameCompilerManifests",
            "unpackratManifestName",
            "sourceDependencies",
            "clang:-MJ",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, text)

    def test_module_version_props_derive_every_macro_from_the_version_file(self) -> None:
        project = _project("build/unpackrat-module-version.props")

        version_file = project.find(f".//{MSBUILD}unpackratModuleVersionFile")
        self.assertIsNotNone(version_file)
        self.assertEqual(
            version_file.text,
            "$(RepositoryRoot)src\\modules\\$(unpackratModuleName)\\VERSION",
        )
        raw = project.find(f".//{MSBUILD}unpackratModuleVersionRaw")
        self.assertIsNotNone(raw)
        self.assertEqual(
            raw.text,
            "$([System.IO.File]::ReadAllText('$(unpackratModuleVersionFile)'))",
        )
        for component, member in (("Major", "Major"), ("Minor", "Minor"), ("Patch", "Build")):
            with self.subTest(component=component):
                node = project.find(f".//{MSBUILD}unpackratModuleVersion{component}")
                self.assertIsNotNone(node)
                self.assertEqual(
                    node.text,
                    f"$([System.Version]::Parse('$(unpackratModuleVersion)').{member})",
                )
                self.assertEqual(
                    node.get("Condition"), "'$(unpackratModuleVersionValid)' == 'true'"
                )

        definitions = [
            node.text
            for node in project.findall(f".//{MSBUILD}PreprocessorDefinitions")
        ]
        self.assertEqual(len(definitions), 2)
        for text in definitions:
            with self.subTest(text=text):
                for macro in ("MAJOR", "MINOR", "PATCH"):
                    self.assertIn(f"UNPACKRAT_MODULE_VERSION_{macro}=$(unpackratModuleVersion", text)
                self.assertTrue(text.endswith(";%(PreprocessorDefinitions)"))
        resource_definitions = project.find(
            f".//{MSBUILD}ResourceCompile/{MSBUILD}PreprocessorDefinitions"
        )
        self.assertIsNotNone(resource_definitions)
        for macro in (
            "UNPACKRAT_MODULE_NAME=$(unpackratModuleName)",
            "UNPACKRAT_MODULE_FILE=$(unpackratModuleName).so",
            "UNPACKRAT_MODULE_VERSION=$(unpackratModuleVersion)",
        ):
            self.assertIn(f"{macro};", resource_definitions.text)

        resource = project.find(f".//{MSBUILD}ItemGroup/{MSBUILD}ResourceCompile")
        self.assertIsNotNone(resource)
        self.assertEqual(resource.get("Include"), "$(RepositoryRoot)src\\modules\\version.rc")
        item_group = project.find(f".//{MSBUILD}ItemGroup[{MSBUILD}ResourceCompile]")
        self.assertEqual(
            item_group.get("Condition"), "'$(unpackratConfigurationType)' == 'DynamicLibrary'"
        )

        # MSBuild is the single version-format authority; the Python driver reads the file plain.
        valid = project.find(f".//{MSBUILD}unpackratModuleVersionValid")
        self.assertIsNotNone(valid)
        self.assertEqual(
            valid.text,
            "$([System.Text.RegularExpressions.Regex]::IsMatch("
            "'$(unpackratModuleVersion)', "
            "'^(0|[1-9][0-9]{0,4})\\.(0|[1-9][0-9]{0,4})\\.(0|[1-9][0-9]{0,4})$'))",
        )
        pattern = re.compile(re.search(r"'(\^\(0\|.*\$)'", valid.text).group(1))
        for version in ("3.1.0", "0.0.0", "65535.65535.65535"):
            with self.subTest(version=version):
                self.assertIsNotNone(pattern.fullmatch(version))
        for version in ("03.1.0", "3.1", "3.1.0.0", "3.1.0-rc1", "", "999999.0.0"):
            with self.subTest(version=version):
                self.assertIsNone(pattern.fullmatch(version))

        errors = project.findall(
            f".//{MSBUILD}Target[@Name='ValidateunpackratModuleVersion']/{MSBUILD}Error"
        )
        self.assertEqual(
            [node.get("Condition") for node in errors],
            [
                "'$(unpackratModuleName)' == ''",
                "'$(unpackratModuleVersionValid)' != 'true'",
                "'$(unpackratModuleVersionValid)' == 'true' And ("
                "$(unpackratModuleVersionMajor) > 65535 Or "
                "$(unpackratModuleVersionMinor) > 65535 Or "
                "$(unpackratModuleVersionPatch) > 65535)",
            ],
        )

    def test_every_project_compiling_a_module_declares_its_version_identity(self) -> None:
        for project_name, module in (
            ("renpy", "renpy"),
            ("rpgmaker", "rpgmaker"),
            ("zanzarah", "zanzarah"),
            ("godot", "godot"),
            ("fuzz-renpy", "renpy"),
            ("fuzz-rpgmaker", "rpgmaker"),
            ("fuzz-zanzarah", "zanzarah"),
        ):
            with self.subTest(project=project_name):
                project = _project(f"build/projects/{project_name}.vcxproj")
                imports = [node.get("Project") for node in project.findall(f"{MSBUILD}Import")]
                self.assertIn("..\\unpackrat-module-version.props", imports)
                self.assertLess(
                    imports.index("..\\unpackrat-project.props"),
                    imports.index("..\\unpackrat-module-version.props"),
                )
                name = project.find(f".//{MSBUILD}unpackratModuleName")
                self.assertIsNotNone(name)
                self.assertEqual(name.text, module)

    def test_projects_without_module_sources_stay_out_of_the_version_contract(self) -> None:
        for project_name in ("tests", "leak-probe", "fuzz-pickle"):
            with self.subTest(project=project_name):
                project = _project(f"build/projects/{project_name}.vcxproj")
                imports = [node.get("Project") for node in project.findall(f"{MSBUILD}Import")]
                self.assertNotIn("..\\unpackrat-module-version.props", imports)
                self.assertIsNone(project.find(f".//{MSBUILD}unpackratModuleName"))

    def test_leak_probe_is_shipping_x64_release_with_release_zlib(self) -> None:
        project = _project("build/projects/leak-probe.vcxproj")

        runtimes = [
            node.text for node in project.findall(f".//{MSBUILD}RuntimeLibrary")
        ]
        dependencies = [
            node.text for node in project.findall(f".//{MSBUILD}AdditionalDependencies")
        ]
        imports = [node.get("Project") for node in project.findall(f"{MSBUILD}Import")]
        validation = project.find(
            f".//{MSBUILD}Target[@Name='ValidateLeakProbeConfiguration']/{MSBUILD}Error"
        )

        self.assertEqual(runtimes, ["MultiThreaded"])
        # The release zlib is supplied by the shared compat-link props, not by a
        # local entry in the probe itself.
        self.assertEqual(dependencies, [])
        self.assertIn("..\\unpackrat-third-party-link.props", imports)
        self.assertIsNotNone(validation)
        self.assertEqual(
            validation.get("Condition"),
            "'$(Configuration)|$(Platform)' != 'Release|x64'",
        )


if __name__ == "__main__":
    unittest.main()

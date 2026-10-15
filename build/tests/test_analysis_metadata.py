from __future__ import annotations

from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MSBUILD = "{http://schemas.microsoft.com/developer/msbuild/2003}"
PROPS = "build/unpackrat-project.props"
# The one switch the standalone MSVC analysis actor flips on; every analysis-only
# change below is keyed on exactly this condition.
ANALYSIS = "'$(unpackratCompileAnalysis)' == 'true'"


def _project(relative_path: str) -> ET.Element:
    return ET.parse(REPOSITORY_ROOT / relative_path).getroot()


def _cl_metadata(project: ET.Element, name: str) -> list[ET.Element]:
    return project.findall(
        f".//{MSBUILD}ItemDefinitionGroup/{MSBUILD}ClCompile/{MSBUILD}{name}"
    )


class CompileAnalysisMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = _project(PROPS)

    def test_compile_analysis_drives_pure_msvc_native_analysis(self) -> None:
        expected = {
            "EnablePREfast": "true",
            "PREfastRulesetFile": "$(RepositoryRoot)build\\unpackrat-native-analysis.ruleset",
            "AdditionalOptions": "/analyze:only /analyze:log:format:sarif %(AdditionalOptions)",
        }
        for name, value in expected.items():
            with self.subTest(metadata=name):
                by_condition = {
                    node.get("Condition"): node.text
                    for node in _cl_metadata(self.project, name)
                }
                self.assertEqual(by_condition.get(ANALYSIS), value)

        # A compile-analysis-only actor sets just unpackratCompileAnalysis=true, so the
        # strict ruleset must resolve directly from the literal path: CodeAnalysisRuleSet
        # is defined only by the report-only switch (unpackratRunCodeAnalysis), so relying
        # on it here would hand PREfast an empty ruleset in the compile-analysis actor.
        ruleset_nodes = _cl_metadata(self.project, "PREfastRulesetFile")
        for node in ruleset_nodes:
            self.assertNotIn("$(CodeAnalysisRuleSet)", node.text)
        property_rulesets = {
            node.get("Condition"): node.text
            for node in self.project.findall(f".//{MSBUILD}CodeAnalysisRuleSet")
        }
        self.assertEqual(
            property_rulesets,
            {
                "'$(unpackratRunCodeAnalysis)' == 'true'":
                    "$(RepositoryRoot)build\\unpackrat-native-analysis.ruleset"
            },
        )

    def test_compile_analysis_keeps_one_sarif_log_per_translation_unit(self) -> None:
        # The log path depends on the per-item object file name, which does not exist when
        # the item definitions are evaluated: an ItemDefinitionGroup %(ObjectFileName)
        # reference collapses to the shared $(IntDir) default and same-basename units
        # collide. The log is therefore computed on a target that runs immediately before
        # ClCompile, once every per-item ObjectFileName override is resolved.
        logs = {
            node.get("Condition"): node.text
            for node in _cl_metadata(self.project, "PREfastLog")
        }
        self.assertNotIn(ANALYSIS, logs)

        target = self.project.find(
            f".//{MSBUILD}Target[@Name='ComputeunpackratCompileAnalysisLogs']"
        )
        self.assertIsNotNone(target)
        self.assertEqual(target.get("BeforeTargets"), "ClCompile")
        self.assertEqual(target.get("Condition"), ANALYSIS)
        updates = target.findall(f"{MSBUILD}ItemGroup/{MSBUILD}ClCompile/{MSBUILD}PREfastLog")
        self.assertEqual(
            [node.text for node in updates],
            ["%(ObjectFileName)%(FileName).nativecodeanalysis.sarif"],
        )

    def test_shipping_release_selects_spectre_libraries_but_analysis_does_not(self) -> None:
        configuration = _project("build/unpackrat-configuration.props")
        spectre = configuration.findall(f".//{MSBUILD}SpectreMitigation")
        self.assertEqual([(node.get("Condition"), node.text) for node in spectre],
                         [(None, "false"), ("'$(Configuration)' == 'Release' And '$(unpackratCompileAnalysis)' != 'true' And '$(unpackratStandaloneClangTidy)' != 'true'", "Spectre")])

        # /Qspectre still reaches the compile-only MSVC analysis item itself.  The global
        # false keeps compile-only analysis from selecting unused Spectre CRT libraries.
        project = _project(PROPS)
        compile_spectre = project.findall(
            f".//{MSBUILD}ItemDefinitionGroup/{MSBUILD}ClCompile/{MSBUILD}SpectreMitigation"
        )
        self.assertEqual(
            [(node.get("Condition"), node.text) for node in compile_spectre],
            [("'$(Configuration)' == 'Release' Or " + ANALYSIS, "Spectre")],
        )

    def test_compile_analysis_is_disjoint_from_tidy_and_link_analysis(self) -> None:
        # The analysis path must never turn on clang-tidy; tidy keeps its own switch.
        tidy = self.project.findall(f".//{MSBUILD}EnableClangTidyCodeAnalysis")
        self.assertEqual(
            {node.get("Condition") for node in tidy},
            {"'$(unpackratEnableClangTidy)' == 'true'"},
        )
        for node in self.project.iter():
            if "unpackratCompileAnalysis" in (node.get("Condition") or ""):
                self.assertNotIn("ClangTidy", node.tag)

        link = self.project.find(f".//{MSBUILD}ItemDefinitionGroup/{MSBUILD}Link")
        self.assertIsNotNone(link)
        for child in link:
            self.assertNotIn(
                "unpackratCompileAnalysis", child.get("Condition") or ""
            )
        options = link.find(f"{MSBUILD}AdditionalOptions")
        self.assertIsNotNone(options)
        self.assertNotIn("/ANALYZE", options.text)

    def test_compile_analysis_uses_a_dedicated_intermediate_tree(self) -> None:
        int_dirs = {
            node.get("Condition"): node.text
            for node in self.project.findall(f".//{MSBUILD}IntDir")
        }
        self.assertEqual(
            int_dirs,
            {
                None:
                    "$(ArtifactsRoot)obj\\$(PlatformMoniker)\\$(Configuration)\\$(ProjectName)\\",
                ANALYSIS:
                    "$(ArtifactsRoot)obj-analysis\\$(PlatformMoniker)\\$(Configuration)\\$(ProjectName)\\",
            },
        )

    def test_language_standard_is_explicit_for_every_mode(self) -> None:
        standards = {
            node.text: node.get("Condition")
            for node in _cl_metadata(self.project, "LanguageStandard")
        }
        self.assertEqual(set(standards), {"stdcpp23", "stdcpplatest"})

        latest = standards["stdcpplatest"]
        cpp23 = standards["stdcpp23"]
        # Release compiles as the latest standard regardless of the tidy switch, so the
        # fallback must exclude Release while the tidy branch keeps selecting latest.
        self.assertIn("'$(Configuration)' == 'Release'", latest)
        self.assertIn("'$(unpackratEnableClangTidy)' == 'true'", latest)
        self.assertIn("'$(Configuration)' != 'Release'", cpp23)
        self.assertIn("'$(unpackratEnableClangTidy)' != 'true'", cpp23)
        for config in ("Coverage", "Fuzz", "UBSan"):
            with self.subTest(config=config):
                self.assertIn(f"'$(Configuration)' == '{config}'", latest)
                self.assertIn(f"'$(Configuration)' != '{config}'", cpp23)

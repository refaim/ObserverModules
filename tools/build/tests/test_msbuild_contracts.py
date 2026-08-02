from __future__ import annotations

from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MSBUILD = "{http://schemas.microsoft.com/developer/msbuild/2003}"


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

    def test_fuzz_validation_allows_only_x64_fuzz_or_compile_analysis(self) -> None:
        project = _project("build/ObserverFuzz.props")
        validation = project.find(
            f".//{MSBUILD}Target[@Name='ValidateFuzzConfiguration']/{MSBUILD}Error"
        )

        self.assertIsNotNone(validation)
        self.assertEqual(
            validation.get("Condition"),
            "'$(ObserverCompileAnalysis)' != 'true' And "
            "'$(Configuration)|$(Platform)' != 'Fuzz|x64'",
        )

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

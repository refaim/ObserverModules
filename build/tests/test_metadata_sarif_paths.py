"""Real MSBuild metadata regression for the per-translation-unit analysis logs.

The standalone compile-analysis path writes one ``nativecodeanalysis.sarif``
beside each object file.  ``build/unpackrat-project.props`` imports before the
``ClCompile`` items exist, so a ``%(ObjectFileName)`` reference placed in its
``ItemDefinitionGroup`` collapses to the shared ``$(IntDir)`` default: the
per-item ``ObjectFileName`` overrides are applied when the items are created,
after the item definition has already been evaluated.  Two translation units
that share a source basename therefore collide on a single log.

These tests drive the real, installed MSBuild over a metadata-only fixture (no
compiler, no linker, no vcpkg) and assert that the project-owned hook yields one
unique log per item once the per-item object file names are resolved, while the
ordinary report-only metadata is left untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from xml.sax.saxutils import escape


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROPS = REPOSITORY_ROOT / "build" / "unpackrat-project.props"

# Two translation units that share the ``bounded_stream`` basename plus one
# ordinary unit.  The shared basename is the collision the hook must break using
# the per-item object file name; the fake source paths are test data only.
UNITS = (
    (r"C:\fixture\src\core\io\bounded_stream.cpp", "bounded_stream_core.obj"),
    (r"C:\fixture\src\tests\unit\bounded_stream.cpp", "bounded_stream_unit.obj"),
    (r"C:\fixture\src\tests\main.cpp", None),
)

# A plain project: an empty ``ClCompile`` target lets the ``BeforeTargets`` hook
# attach, and an ``ItemDefinitionGroup`` mirrors the vendor default where
# ``ObjectFileName`` names the object directory rather than the object file.
FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<Project DefaultTargets="ClCompile" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup>
    <ProjectName>sarif-fixture</ProjectName>
  </PropertyGroup>
  <ItemDefinitionGroup>
    <ClCompile>
      <ObjectFileName>$(IntDir)</ObjectFileName>
      <PREfastAdditionalOptions>{prefast_options}</PREfastAdditionalOptions>
    </ClCompile>
  </ItemDefinitionGroup>
  <Import Project="{props}" />
  <ItemGroup>
{items}  </ItemGroup>
  <Target Name="PrepareForBuild" />
  <Target Name="ClCompile" />
</Project>
"""


def _fixture_text(options: str = "quiet;preserved-fixture-option") -> str:
    lines = []
    for include, object_name in UNITS:
        if object_name is None:
            lines.append(f'    <ClCompile Include="{include}" />')
        else:
            lines.append(
                f'    <ClCompile Include="{include}">\n'
                f'      <ObjectFileName>$(IntDir){object_name}</ObjectFileName>\n'
                f'    </ClCompile>'
            )
    return FIXTURE.format(props=PROPS, items="\n".join(lines) + "\n",
                          prefast_options=escape(options))


class CompileAnalysisSarifPathTests(unittest.TestCase):
    def _evaluated_items(
        self, properties: dict[str, str], *, options: str = "quiet;preserved-fixture-option",
    ) -> list[dict[str, str]]:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "sarif-fixture.proj"
            project.write_text(_fixture_text(options), encoding="utf-8")
            arguments = [
                str(main._find_msbuild()),
                str(project),
                "-nologo",
                "-t:ClCompile",
                "-getItem:ClCompile",
            ]
            arguments.extend(f"-p:{name}={value}" for name, value in properties.items())
            completed = subprocess.run(
                arguments, capture_output=True, encoding="utf-8", check=True
            )
        return json.loads(completed.stdout)["Items"]["ClCompile"]

    def test_compile_only_analysis_removes_only_the_exact_vendor_quiet_option(self) -> None:
        options = (
            "", "quiet", "quiet;quiet;preserved-fixture-option",
            "quiet-neighbor;quiet;still-quiet;quiet-",
            'quiet;plugin:"C:\\a path\\ü\'s, plugin.dll";custom;quiet',
        )
        for compile_only in ("true", "false"):
            for inherited in options:
                with self.subTest(compile_only=compile_only, inherited=inherited):
                    items = self._evaluated_items({
                        "Configuration": "Release", "Platform": "x64",
                        "unpackratCompileAnalysis": compile_only,
                        "unpackratRunCodeAnalysis": "true",
                    }, options=inherited)
                    expected = [option for option in inherited.split(";") if option
                                and (compile_only != "true" or option != "quiet")]
                    actual = [[option for option in item.get("PREfastAdditionalOptions", "").split(";")
                               if option] for item in items]
                    self.assertEqual(actual, [expected] * len(UNITS))

    def test_compile_analysis_logs_are_unique_per_translation_unit(self) -> None:
        items = self._evaluated_items(
            {
                "Configuration": "Release",
                "Platform": "x64",
                "unpackratCompileAnalysis": "true",
                "SpectreMitigation": "false",
            }
        )
        self.assertEqual(len(items), len(UNITS))
        for item in items:
            self.assertEqual(item["SpectreMitigation"], "Spectre")
            self.assertIn("/analyze:only", item["AdditionalOptions"])
            # The log must be derived from the resolved per-item object file name,
            # never from the shared $(IntDir) default the early definitions see.
            self.assertEqual(
                item["PREfastLog"],
                f'{item["ObjectFileName"]}{item["Filename"]}.nativecodeanalysis.sarif',
            )
        logs = [item["PREfastLog"] for item in items]
        self.assertEqual(len(set(logs)), len(items))
        shared = [
            item["PREfastLog"] for item in items if item["Filename"] == "bounded_stream"
        ]
        self.assertEqual(len(shared), 2)
        self.assertNotEqual(shared[0], shared[1])

    def test_report_only_build_keeps_the_shared_analysis_report(self) -> None:
        report = r"C:\reports\shared.nativecodeanalysis.sarif"
        items = self._evaluated_items(
            {
                "Configuration": "Release",
                "Platform": "x64",
                "unpackratRunCodeAnalysis": "true",
                "unpackratAnalysisReportPath": report,
            }
        )
        self.assertEqual({item["PREfastLog"] for item in items}, {report})


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path


BUILD_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = BUILD_ROOT.parent
sys.path.insert(0, str(BUILD_ROOT))

from graphs.source import SourceTools, _repository_files, source_checks  # noqa: E402
from core.paths import BuildPaths  # noqa: E402


class SourceGraphTests(unittest.TestCase):
    def tools(self) -> SourceTools:
        return SourceTools(
            pwsh=Path(r"C:\tools\pwsh.exe"),
            clang_format=Path(r"C:\tools\clang-format.exe"),
            cppcheck=Path(r"C:\tools\cppcheck.exe"),
            psscriptanalyzer=Path(r"C:\modules\PSScriptAnalyzer.psd1"),
            vcpkg_root=Path(r"C:\tools\vcpkg"),
            environment=(("PATH", r"C:\tools"),),
            identity=(
                ("clang_format", r"C:\tools\clang-format.exe"),
                ("clang_format_version", "21.1"),
                ("cppcheck", r"C:\tools\cppcheck.exe"),
                ("cppcheck_version", "2.18"),
                ("psscriptanalyzer", r"C:\modules\PSScriptAnalyzer.psd1"),
                ("psscriptanalyzer_version", "1.24"),
                ("pwsh", r"C:\tools\pwsh.exe"),
                ("pwsh_version", "7.5"),
                ("vcpkg_root", r"C:\tools\vcpkg"),
            ),
        )

    def graph(self):
        return source_checks(REPOSITORY, self.tools(), jobs=7)

    def test_contract_inventory_excludes_transient_build_state(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            source = repository / "src/file.cpp"
            source.parent.mkdir()
            source.touch()
            for relative in (".coverage", "build/.coverage.agent"):
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()

            files = _repository_files(repository)

        self.assertEqual(files, (source,))

    def test_every_independent_source_check_is_a_demand_node(self) -> None:
        graph = self.graph()
        cpp_sources = sorted(
            path
            for path in (REPOSITORY / "src").rglob("*")
            if path.is_file() and path.suffix in {".cpp", ".h", ".hpp"}
        )
        powershell_sources = [REPOSITORY / "build.ps1"] + sorted(
            path
            for path in (REPOSITORY / "build").rglob("*")
            if path.is_file() and path.suffix in {".ps1", ".psm1"}
        )
        contracts = sorted((REPOSITORY / "build/tests").glob("*.Tests.ps1"))

        self.assertEqual(
            len(graph.nodes),
            len(cpp_sources) + len(powershell_sources) + len(contracts) + 8,
        )
        self.assertEqual(graph.targets, ("source-checks",))
        self.assertEqual(dict(graph.pools), {"restore": 1, "slot": 7})
        self.assertEqual(
            len([node for node in graph.nodes if node.name.startswith("format-")]),
            len(cpp_sources),
        )
        self.assertEqual(
            len([node for node in graph.nodes if node.name.startswith("pssa-")]),
            len(powershell_sources),
        )
        self.assertEqual(
            len([node for node in graph.nodes if node.name.startswith("contract-")]),
            len(contracts),
        )
        self.assertEqual(
            {node.name for node in graph.nodes if node.name.startswith("cppcheck-")},
            {"cppcheck-x86", "cppcheck-x64", "cppcheck-arm64"},
        )
        self.assertEqual(
            {node.name for node in graph.nodes if node.name.startswith("restore-vcpkg-")},
            {"restore-vcpkg-x86", "restore-vcpkg-x64", "restore-vcpkg-arm64"},
        )
        for architecture in ("x86", "x64", "arm64"):
            self.assertEqual(
                graph.node(f"cppcheck-{architecture}").inputs,
                (f"restore-vcpkg-{architecture}",),
            )
        self.assertTrue(
            all(
                not node.inputs
                for node in graph.nodes
                if node.name.startswith(("format-", "pssa-", "contract-"))
            )
        )

        merged = graph.node("merge-source-findings")
        pssa_names = {
            node.name for node in graph.nodes if node.name.startswith("pssa-")
        }
        self.assertEqual(
            set(merged.inputs),
            {"cppcheck-x86", "cppcheck-x64", "cppcheck-arm64"} | pssa_names,
        )
        gate = graph.node("source-checks")
        direct = {
            node.name
            for node in graph.nodes
            if node.name.startswith(("format-", "contract-"))
        }
        self.assertEqual(set(gate.inputs), direct | {"merge-source-findings"})

    def test_cppcheck_matrix_contains_only_requested_supported_architectures(self) -> None:
        graph = source_checks(REPOSITORY, self.tools(), jobs=7, architectures=("arm64", "x64"))

        self.assertEqual(
            {node.name for node in graph.nodes if node.name.startswith("cppcheck-")},
            {"cppcheck-arm64", "cppcheck-x64"},
        )
        self.assertEqual(
            {node.name for node in graph.nodes if node.name.startswith("restore-vcpkg-")},
            {"restore-vcpkg-arm64", "restore-vcpkg-x64"},
        )
        self.assertNotIn("cppcheck-x86", graph.node("merge-source-findings").inputs)
        for architectures in ((), ("mips",), ("x64", "x64")):
            with self.subTest(architectures=architectures), self.assertRaisesRegex(ValueError, "architectures"):
                source_checks(REPOSITORY, self.tools(), architectures=architectures)

    def test_templates_keep_tool_paths_literal_and_cppcheck_findings_publishable(self) -> None:
        graph = self.graph()

        formatted = graph.node("format-src.modules.renpy.pickle.cpp")
        self.assertEqual(formatted.command.argv[0], r"C:\tools\pwsh.exe")
        format_script = formatted.command.stdin.decode()
        self.assertIn("Invoke-Checked 'C:\\tools\\clang-format.exe'", format_script)
        self.assertIn("'--dry-run'", format_script)
        self.assertIn("'--Werror'", format_script)
        self.assertIn(str(REPOSITORY / "src/modules/renpy/pickle.cpp"), format_script)

        cppcheck = graph.node("cppcheck-x86")
        cppcheck_script = cppcheck.command.stdin.decode()
        restore = graph.node("restore-vcpkg-x86")
        include_dir = (
            BuildPaths(REPOSITORY).cas(restore.uid, restore.name).output
            / "observer-x86-windows-static/include"
        )
        self.assertIn("Invoke-Checked 'C:\\tools\\cppcheck.exe'", cppcheck_script)
        self.assertIn("'--platform=win32W'", cppcheck_script)
        self.assertIn("'-D_M_IX86=600'", cppcheck_script)
        self.assertIn(f"'-I{include_dir}'", cppcheck_script)
        self.assertIn("'--suppress=*:out/cas/*-restore-vcpkg-*/out/*'", cppcheck_script)
        self.assertIn('"--output-file=$outDir\\cppcheck.sarif"', cppcheck_script)
        self.assertNotIn("--error-exitcode", cppcheck_script)
        self.assertIn("Cppcheck did not produce cppcheck.sarif", cppcheck_script)
        self.assertIn("'cppcheck/x86/' + \"$index/\"", cppcheck_script)

        pssa = graph.node("pssa-build.ps1")
        pssa_script = pssa.command.stdin.decode()
        self.assertIn("Import-Module 'C:\\modules\\PSScriptAnalyzer.psd1'", pssa_script)
        self.assertIn(str(REPOSITORY / "build.ps1"), pssa_script)
        self.assertIn(str(REPOSITORY / "build/PSScriptAnalyzerSettings.psd1"), pssa_script)
        self.assertIn('"$outDir\\psscriptanalyzer.sarif"', pssa_script)
        self.assertNotIn("PSScriptAnalyzer reported", pssa_script)
        self.assertIn("'psscriptanalyzer/build.ps1/'", pssa_script)

        contracts = sorted((REPOSITORY / "build/tests").glob("*.Tests.ps1"))
        self.assertTrue(contracts)
        for test in contracts:
            relative = test.relative_to(REPOSITORY).as_posix()
            contract = graph.node(f"contract-{relative.lower().replace('/', '.')}")
            self.assertEqual(contract.command.argv[0], r"C:\tools\pwsh.exe")
            self.assertIn(str(test), contract.command.stdin.decode())

        merge = graph.node("merge-source-findings")
        self.assertEqual(merge.command.argv[1:4], ("-m", "core.sarif", "merge"))
        gate = graph.node("source-checks")
        self.assertEqual(gate.command.argv[1:4], ("-m", "core.sarif", "gate"))

    def test_tool_identity_changes_only_affected_source_check_branch(self) -> None:
        before = self.graph()
        identity = dict(self.tools().identity)
        identity["cppcheck_version"] = "2.19"
        after = source_checks(
            REPOSITORY,
            replace(self.tools(), identity=tuple(identity.items())),
            jobs=7,
        )

        self.assertNotEqual(
            before.node("cppcheck-x64").uid,
            after.node("cppcheck-x64").uid,
        )
        for name in (
            "format-src.modules.renpy.pickle.cpp",
            "pssa-build.ps1",
            "restore-vcpkg-x64",
        ):
            self.assertEqual(before.node(name).uid, after.node(name).uid)

        identity = dict(self.tools().identity)
        identity["vcpkg_root"] = r"C:\new-vcpkg"
        moved_restore = source_checks(
            REPOSITORY,
            replace(
                self.tools(),
                vcpkg_root=Path(r"C:\new-vcpkg"),
                identity=tuple(identity.items()),
            ),
            jobs=7,
        )
        self.assertNotEqual(
            before.node("restore-vcpkg-x64").uid,
            moved_restore.node("restore-vcpkg-x64").uid,
        )
        self.assertNotEqual(
            before.node("cppcheck-x64").uid,
            moved_restore.node("cppcheck-x64").uid,
        )
        for name in (
            "format-src.modules.renpy.pickle.cpp",
            "pssa-build.ps1",
        ):
            self.assertEqual(before.node(name).uid, moved_restore.node(name).uid)

    def test_rendered_powershell_is_parseable(self) -> None:
        graph = self.graph()
        scripts = "\n".join(
            node.command.stdin.decode()
            for node in graph.nodes
            if node.command.argv[0] == r"C:\tools\pwsh.exe"
        )
        parser = """
$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseInput(
    [Console]::In.ReadToEnd(), [ref]$tokens, [ref]$errors) | Out-Null
if ($errors.Count -ne 0) { $errors | Out-String | Write-Error; exit 1 }
"""
        result = subprocess.run(
            [
                shutil.which("pwsh"),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                parser,
            ],
            input=scripts,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

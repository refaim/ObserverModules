from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
import tempfile
import unittest


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from graphs import analysis, common, fuzz, native  # noqa: E402


@dataclass(frozen=True)
class FakeToolchain:
    msbuild: Path
    pwsh: Path
    vcpkg_root: Path
    llvm_dir: Path
    environment: tuple[tuple[str, str], ...]
    identity: dict[str, str]


def write(root: Path, relative: str, content: str = "") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def toolchain(root: Path, *, runtime: bool = False) -> FakeToolchain:
    tools = root / "tools"
    vcpkg = tools / "vcpkg"
    llvm = tools / "llvm"
    vcpkg.mkdir(parents=True)
    llvm.mkdir()
    if runtime:
        (llvm / "lib/clang/19/lib/windows").mkdir(parents=True)
    for path in (tools / "MSBuild.exe", tools / "pwsh.exe", vcpkg / "vcpkg.exe"):
        path.touch()
    return FakeToolchain(
        tools / "MSBuild.exe",
        tools / "pwsh.exe",
        vcpkg,
        llvm,
        (("PATH", str(tools)),),
        {"msbuild": "17.14", "msvc": "14.44", "llvm": "19.1"},
    )


def native_repository(root: Path) -> Path:
    project = """<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup><ClCompile Include="$(RepositoryRoot)src\\{name}.cpp" /></ItemGroup>
</Project>
"""
    for relative in (
        "build/ObserverProjectConfigurations.props",
        "build/ObserverConfiguration.props",
        "build/ObserverProject.props",
    ):
        write(root, relative, "<Project />\n")
    write(root, "vcpkg.json", "{}\n")
    write(root, "build/vcpkg/triplets/observer-x64-windows-static.cmake")
    for name in ("renpy", "rpgmaker", "zanzarah", "tests", "leak-probe"):
        write(root, f"src/{name}.cpp", "int value;\n")
        write(root, f"build/projects/{name}.vcxproj", project.format(name=name))
    return root


class AnalysisCoverageTests(unittest.TestCase):
    def test_compiler_manifest_accepts_only_exact_relevant_dependency_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            source = write(repository, "src/module/source.cpp", "int value;\n")
            first_party = write(repository, "src/shared.h", "first-party\n")
            package_root = repository / "out/cas/package/out"
            package = write(package_root, "include/package.h", "package\n")
            system = write(repository, "sdk/system.h", "system\n")
            content = json.dumps(
                {
                    "Data": {
                        "Source": str(source.resolve()),
                        "Includes": [
                            str(first_party.resolve()),
                            str(first_party.resolve()),
                            str(package.resolve()),
                            str(system.resolve()),
                        ],
                    }
                }
            ).encode()

            files = analysis.dependency_inputs(repository, package_root, source, content)

        self.assertEqual(
            set(files),
            {"compiler/dependencies.json", "src/module/source.cpp", "src/shared.h", "vcpkg/include/package.h"},
        )

    def test_invalid_or_mismatched_compiler_manifests_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            source = write(repository, "src/source.cpp", "int value;\n")
            package = repository / "out/cas/package/out"
            package.mkdir(parents=True)
            invalid = (
                b"{",
                b"{}",
                json.dumps({"Data": {"Source": 1, "Includes": []}}).encode(),
                json.dumps({"Data": {"Source": str(source), "Includes": {}}}).encode(),
                json.dumps({"Data": {"Source": str(source), "Includes": [None]}}).encode(),
                json.dumps({"Data": {"Source": str(source), "Includes": ["relative.h"]}}).encode(),
                json.dumps({"Data": {"Source": str(source), "Includes": [str(repository / 'src/missing.h')]}}).encode(),
            )
            for content in invalid:
                with self.subTest(content=content), self.assertRaisesRegex(ValueError, "invalid MSVC"):
                    analysis.dependency_inputs(repository, package, source, content)
            mismatch = json.dumps(
                {"Data": {"Source": str(write(repository, "src/other.cpp")), "Includes": []}}
            ).encode()
            with self.assertRaisesRegex(ValueError, "source mismatch"):
                analysis.dependency_inputs(repository, package, source, mismatch)

    def test_project_inventory_ignores_empty_items_and_rejects_external_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            write(
                repository,
                "build/projects/empty.vcxproj",
                '<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">'
                "<ItemGroup><ClCompile /></ItemGroup></Project>",
            )
            self.assertEqual(analysis._projects(repository), ())

            write(
                repository,
                "build/projects/empty.vcxproj",
                '<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">'
                '<ItemGroup><ClCompile Include="C:\\external.cpp" /></ItemGroup></Project>',
            )
            with self.assertRaisesRegex(ValueError, "unsupported ClCompile path"):
                analysis._projects(repository)

    def test_fuzz_analysis_signs_fuzz_props_and_unknown_architecture_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            for relative in (
                "build/ObserverProjectConfigurations.props",
                "build/ObserverConfiguration.props",
                "build/ObserverProject.props",
                "build/ObserverFuzz.props",
            ):
                write(repository, relative, "<Project />\n")
            write(repository, "build/ObserverNativeAnalysis.ruleset", "<RuleSet />\n")
            write(repository, ".clang-tidy", "Checks: bugprone-*\n")
            write(repository, "vcpkg.json", "{}\n")
            write(repository, "build/vcpkg/triplets/observer-x64-windows-static.cmake")
            write(repository, "src/fuzz/pickle.cpp", "int value;\n")
            write(
                repository,
                "build/projects/fuzz-pickle.vcxproj",
                '<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">'
                '<ItemGroup><ClCompile Include="$(RepositoryRoot)src\\fuzz\\pickle.cpp" />'
                "</ItemGroup></Project>",
            )
            fake = toolchain(root)

            before_discovery = analysis.analysis_discovery_slice(repository, fake)
            name = before_discovery.targets[0]
            manifest = json.dumps(
                {"Data": {"Source": str((repository / "src/fuzz/pickle.cpp").resolve()), "Includes": []}}
            ).encode()
            before = analysis.analysis_slice(
                repository, fake, discovery=before_discovery, manifests={name: manifest}
            )
            write(repository, "build/ObserverFuzz.props", "<Project><!-- changed --></Project>\n")
            after_discovery = analysis.analysis_discovery_slice(repository, fake)
            after = analysis.analysis_slice(
                repository, fake, discovery=after_discovery, manifests={name: manifest}
            )
            node_name = "analyze-msvc-x64-fuzz-pickle-fuzz.pickle"

            self.assertNotEqual(before.node(node_name).uid, after.node(node_name).uid)
            with self.assertRaisesRegex(ValueError, "unsupported architecture: mips"):
                analysis.analysis_discovery_slice(repository, fake, architectures=("mips",))
            with self.assertRaisesRegex(ValueError, "unsupported architecture: mips"):
                analysis.analysis_slice(
                    repository,
                    fake,
                    discovery=after_discovery,
                    manifests={name: manifest},
                    architectures=("mips",),
                )
            with self.assertRaisesRegex(ValueError, "missing dependency manifest"):
                analysis.analysis_slice(
                    repository, fake, discovery=after_discovery, manifests={}
                )


class CommonAndFuzzCoverageTests(unittest.TestCase):
    def test_environment_without_vcpkg_root_preserves_path_and_extra_values(self) -> None:
        fake = type("Toolchain", (), {"environment": (("Path", "base"),)})()

        environment = dict(
            common.tool_environment(
                fake,
                prepend_path=Path("runtime"),
                extra=(("MODE", "checked"),),
            )
        )

        self.assertEqual(environment["PATH"], f"runtime{common.os.pathsep}base")
        self.assertEqual(environment["MODE"], "checked")
        self.assertNotIn("VCPKG_ROOT", environment)

    def test_missing_sanitizer_runtime_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = toolchain(root)

            with self.assertRaisesRegex(FileNotFoundError, "sanitizer runtimes"):
                fuzz._runtime(fake)

    def test_fuzzer_project_rejects_unsigned_source_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            for relative in fuzz._COMMON:
                write(repository, relative, "<Project />\n")
            write(
                repository,
                "build/projects/fuzz-pickle.vcxproj",
                '<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">'
                '<ItemGroup><ClCompile Include="src\\fuzz\\pickle.cpp" />'
                "</ItemGroup></Project>",
            )

            with self.assertRaisesRegex(ValueError, "unsupported ClCompile path"):
                fuzz._project_files(
                    repository, "pickle", Path("unused"), object(), {}
                )

    def test_empty_seed_corpus_and_nonpositive_duration_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            (repository / "src/fuzz/corpus/pickle").mkdir(parents=True)
            with self.assertRaisesRegex(FileNotFoundError, "no checked-in fuzzer seeds"):
                fuzz._seed_files(repository, "pickle")

        with self.assertRaisesRegex(ValueError, "fuzz seconds must be positive"):
            fuzz.fuzz_graph(
                Path("unused"), object(), discovery=None, manifests={},
                run_nonce="run", seconds=0,
            )


class NativeCoverageTests(unittest.TestCase):
    def test_project_metadata_rejects_external_input_and_ignores_empty_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            project = write(
                repository,
                "build/projects/sample.vcxproj",
                '<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">'
                "<ItemGroup><ClCompile /><ModuleDefinitionFile /></ItemGroup></Project>",
            )

            inputs = native._project_inputs(repository, project)

            self.assertEqual(inputs[-1], "build/projects/sample.vcxproj")
            self.assertEqual(len(inputs), len(native._COMMON_INPUTS) + 1)
            with self.assertRaisesRegex(ValueError, "unsupported project input"):
                native._relative(repository, "C:\\external.cpp", project)

    def test_invalid_job_capacity_and_nonrunnable_release_leak_probe_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "jobs must be a positive integer"):
            native.native_graph(
                Path("unused"), object(), jobs=0, runnable_architectures=()
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = native_repository(root / "repo")
            fake = toolchain(root)
            discovery = native.native_dependency_discovery_slice(
                repository, fake, configurations=("Release",), include_leak_probe=True
            )
            projects = ("renpy", "rpgmaker", "zanzarah", "tests", "leak-probe")
            manifests = {
                name: json.dumps(
                    {
                        "Data": {
                            "Source": str((repository / f"src/{next(project for project in projects if name.endswith('-' + project))}.cpp").resolve()),
                            "Includes": [],
                        }
                    }
                ).encode()
                for name in discovery.targets
            }
            graph = native.native_graph(
                repository,
                fake,
                discovery=discovery,
                manifests=manifests,
                architectures=("x64",),
                configurations=("Release",),
                runnable_architectures=(),
                test_shards=1,
                include_leak_probe=True,
            )

        self.assertIn("build-leak-probe-x64-release", graph.targets)
        self.assertFalse(any(name.startswith("test-shard-") for name in graph.targets))


if __name__ == "__main__":
    unittest.main(verbosity=2)

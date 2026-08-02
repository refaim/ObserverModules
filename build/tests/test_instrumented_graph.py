from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.paths import BuildPaths  # noqa: E402
from core.graph import Graph, Node  # noqa: E402
from graphs.instrumented import (  # noqa: E402
    InstrumentedVariant,
    instrumented_build_slice,
    instrumented_dependency_discovery_slice,
)
from graphs.analysis import (  # noqa: E402
    clang_dependency_discovery_slice,
    dependency_node_name,
)


@dataclass(frozen=True)
class FakeToolchain:
    msbuild: Path
    pwsh: Path
    vcpkg_root: Path
    llvm_dir: Path
    environment: tuple[tuple[str, str], ...]
    identity: dict[str, str]


class InstrumentedBuildGraphTests(unittest.TestCase):
    def repository(self, root: Path) -> Path:
        project = """<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup><ClCompile Include="$(RepositoryRoot)src\\{name}.cpp" /></ItemGroup>
  {definition}
</Project>
"""
        files = {
            "src/renpy.cpp": '#include "renpy.h"\n',
            "src/renpy.h": "#pragma once\n",
            "src/rpgmaker.cpp": '#include "rpgmaker.h"\n',
            "src/rpgmaker.h": "#pragma once\n",
            "src/zanzarah.cpp": '#include "zanzarah.h"\n',
            "src/zanzarah.h": "#pragma once\n",
            "src/tests.cpp": '#include "tests.h"\n',
            "src/tests.h": "#pragma once\n",
            "src/leak-probe.cpp": '#include "tests.h"\n',
            "src/renpy.def": "EXPORTS\n  OpenStorage\n",
            "build/ObserverProjectConfigurations.props": "<Project />\n",
            "build/ObserverConfiguration.props": "<Project />\n",
            "build/ObserverProject.props": "<Project />\n",
            "vcpkg.json": '{"dependencies":["zlib"]}\n',
        }
        for architecture in ("x86", "x64"):
            for flavor in ("", "-asan"):
                files[
                    f"build/vcpkg/triplets/observer-{architecture}-windows-static{flavor}.cmake"
                ] = "set(VCPKG_LIBRARY_LINKAGE static)\n"
        for name in ("renpy", "rpgmaker", "zanzarah", "tests"):
            definition = (
                "<ItemDefinitionGroup><Link><ModuleDefinitionFile>"
                "$(RepositoryRoot)src\\renpy.def</ModuleDefinitionFile></Link>"
                "</ItemDefinitionGroup>"
                if name == "renpy"
                else (
                    "<ItemDefinitionGroup><Link><ModuleDefinitionFile />"
                    "</Link></ItemDefinitionGroup>"
                    if name == "rpgmaker"
                    else ""
                )
            )
            files[f"build/projects/{name}.vcxproj"] = project.format(
                name=name, definition=definition
            )
        files["build/projects/leak-probe.vcxproj"] = project.format(
            name="leak-probe", definition=""
        )
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    def toolchain(self, root: Path) -> FakeToolchain:
        tools = root / "tools"
        tools.mkdir()
        vcpkg = tools / "vcpkg"
        vcpkg.mkdir()
        (vcpkg / "vcpkg.exe").touch()
        llvm = tools / "llvm"
        (llvm / "bin").mkdir(parents=True)
        (llvm / "bin/clang-cl.exe").write_bytes(b"clang-v1")
        (llvm / "bin/clang-scan-deps.exe").write_bytes(b"scan-v1")
        msbuild, pwsh = tools / "MSBuild.exe", tools / "pwsh.exe"
        msbuild.touch()
        pwsh.touch()
        return FakeToolchain(
            msbuild, pwsh, vcpkg, llvm,
            (("PATH", str(tools)),),
            {"msbuild": "17.14", "msvc": "14.44", "llvm": "20.1"},
        )

    def llvm_runtime(self, root: Path) -> Path:
        runtime = root / "llvm-runtime"
        runtime.mkdir()
        for name in (
            "clang_rt.ubsan_standalone-x86_64.lib",
            "clang_rt.ubsan_standalone_cxx-x86_64.lib",
        ):
            (runtime / name).touch()
        return runtime

    def variants(self, runtime: Path) -> tuple[InstrumentedVariant, ...]:
        return (
            InstrumentedVariant("coverage", "x64"),
            InstrumentedVariant("asan", "x86"),
            InstrumentedVariant("asan", "x64"),
            InstrumentedVariant(
                "ubsan", "x64", runtime,
                {"standalone": "sha256:a", "cxx": "sha256:b"},
            ),
        )

    def manifests(self, repository: Path, discovery) -> dict[str, bytes]:
        result = {}
        for target in discovery.targets:
            project = next(
                name
                for name in ("renpy", "rpgmaker", "zanzarah", "tests")
                if f"-{name}-" in target
            )
            result[target] = json.dumps(
                {
                    "Data": {
                        "Source": str((repository / f"src/{project}.cpp").resolve()),
                        "Includes": [str((repository / f"src/{project}.h").resolve())],
                    }
                }
            ).encode()
        return result

    def test_canonical_restores_discoveries_and_builds_cover_every_variant_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            variants = self.variants(self.llvm_runtime(root))
            discovery = instrumented_dependency_discovery_slice(
                repository, toolchain, variants=variants, jobs=8
            )
            graph = instrumented_build_slice(
                repository,
                toolchain,
                discovery=discovery,
                manifests=self.manifests(repository, discovery),
                variants=variants,
                jobs=8,
            )
            paths = BuildPaths(repository)

        names = tuple(item.name for item in discovery.nodes)
        self.assertEqual(names.count("restore-vcpkg-x64"), 1)
        self.assertEqual(names.count("restore-vcpkg-asan-x86"), 1)
        self.assertEqual(names.count("restore-vcpkg-asan-x64"), 1)
        self.assertEqual(len(discovery.targets), 16)
        clang_capture_count = 4 * sum(
            item.kind in {"coverage", "ubsan"} for item in variants
        )
        self.assertEqual(
            len(discovery.nodes),
            3 + len(discovery.targets) + clang_capture_count,
        )
        coverage_discovery = discovery.node(
            next(name for name in discovery.targets if "-x64-coverage-renpy-" in name)
        )
        ubsan_discovery = discovery.node(
            next(name for name in discovery.targets if "-x64-ubsan-renpy-" in name)
        )
        asan_discovery = discovery.node(
            next(name for name in discovery.targets if "-x64-asan-renpy-" in name)
        ).command.stdin.decode("utf-8")
        for normalized in (coverage_discovery, ubsan_discovery):
            self.assertEqual(len(normalized.inputs), 1)
            raw = discovery.node(normalized.inputs[0])
            script = raw.command.stdin.decode("utf-8")
            self.assertIn("/p:ObserverClangCommandPath=", script)
            self.assertIn("'/p:LLVMInstallDir=", script)
            self.assertNotIn("PlatformToolset=v143", script)
            self.assertNotIn("ObserverSourceDependenciesPath", script)
            self.assertEqual(
                normalized.command.argv[1:4],
                ("-m", "core.clang_dependencies", "scan"),
            )
        self.assertNotIn("PlatformToolset=v143", asan_discovery)
        self.assertEqual(len(graph.nodes), len(discovery.nodes) + len(graph.targets))
        self.assertEqual(len(graph.targets), 16)
        self.assertEqual(graph.pools, {"restore": 1, "slot": 8})

        for variant in variants:
            restore = (
                f"restore-vcpkg-asan-{variant.architecture}"
                if variant.kind == "asan"
                else f"restore-vcpkg-{variant.architecture}"
            )
            for project in ("renpy", "rpgmaker", "zanzarah", "tests"):
                build = graph.node(
                    f"build-{project}-{variant.architecture}-{variant.kind}"
                )
                self.assertEqual(len(build.inputs), 1)
                self.assertIn(
                    f"-{variant.architecture}-{variant.kind}-{project}-", build.inputs[0]
                )
                script = build.command.stdin.decode("utf-8")
                self.assertIn(f"'/p:Configuration={variant.configuration}'", script)
                self.assertIn("'/m:1'", script)
                self.assertIn("'/p:BuildProjectReferences=false'", script)
                self.assertIn(str(paths.cas(graph.node(restore).uid).output), script)
                if variant.kind in {"coverage", "ubsan"}:
                    self.assertIn("'/p:LLVMInstallDir=", script)
                else:
                    self.assertNotIn("'/p:LLVMInstallDir=", script)
                if variant.kind == "ubsan":
                    self.assertIn("'/p:LLVMRuntimeDir=", script)
                else:
                    self.assertNotIn("'/p:LLVMRuntimeDir=", script)

    def test_invalid_variants_manifests_and_runtime_contracts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            runtime = self.llvm_runtime(root)
            good = (InstrumentedVariant("coverage", "x64"),)
            discovery = instrumented_dependency_discovery_slice(
                repository, toolchain, variants=good
            )

            for variant in (
                InstrumentedVariant("msan", "x64"),
                InstrumentedVariant("asan", "arm64"),
                InstrumentedVariant("ubsan", "x86", runtime, {"hash": "x"}),
            ):
                with self.subTest(variant=variant), self.assertRaisesRegex(ValueError, "variant"):
                    instrumented_dependency_discovery_slice(
                        repository, toolchain, variants=(variant,)
                    )
            with self.assertRaisesRegex(ValueError, "duplicate"):
                instrumented_dependency_discovery_slice(
                    repository, toolchain, variants=good + good
                )
            with self.assertRaisesRegex(ValueError, "at least one"):
                instrumented_dependency_discovery_slice(
                    repository, toolchain, variants=()
                )
            with self.assertRaisesRegex(ValueError, "jobs"):
                instrumented_dependency_discovery_slice(
                    repository, toolchain, variants=good, jobs=True
                )
            with self.assertRaisesRegex(ValueError, "runtime"):
                instrumented_dependency_discovery_slice(
                    repository,
                    toolchain,
                    variants=(InstrumentedVariant("ubsan", "x64"),),
                )
            with self.assertRaisesRegex(ValueError, "runtime"):
                instrumented_dependency_discovery_slice(
                    repository,
                    toolchain,
                    variants=(InstrumentedVariant("coverage", "x64", runtime, {"x": "y"}),),
                )

            manifests = self.manifests(repository, discovery)
            with self.assertRaisesRegex(ValueError, "missing dependency manifest"):
                instrumented_build_slice(
                    repository,
                    toolchain,
                    discovery=discovery,
                    manifests={},
                    variants=good,
                )
            with self.assertRaisesRegex(ValueError, "discovery is required"):
                instrumented_build_slice(
                    repository,
                    toolchain,
                    discovery=None,
                    manifests=manifests,
                    variants=good,
                )
            with self.assertRaisesRegex(ValueError, "jobs"):
                instrumented_build_slice(
                    repository,
                    toolchain,
                    discovery=discovery,
                    manifests=manifests,
                    variants=good,
                    jobs=0,
                )
            conflicting_pool = Graph(
                discovery.nodes,
                discovery.targets,
                {"restore": 1, "slot": 3},
            )
            with self.assertRaisesRegex(ValueError, "conflicting slot"):
                instrumented_build_slice(
                    repository,
                    toolchain,
                    discovery=conflicting_pool,
                    manifests=manifests,
                    variants=good,
                    jobs=2,
                )

            project = repository / "build/projects/tests.vcxproj"
            project.write_text(
                project.read_text(encoding="utf-8").replace(
                    "$(RepositoryRoot)src\\tests.cpp", "src\\tests.cpp"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unsupported project input"):
                instrumented_build_slice(
                    repository,
                    toolchain,
                    discovery=discovery,
                    manifests=manifests,
                    variants=good,
                )
            project.write_text(
                project.read_text(encoding="utf-8").replace(
                    "src\\tests.cpp", "$(RepositoryRoot)src\\tests.cpp"
                ),
                encoding="utf-8",
            )

            empty_runtime = root / "empty-runtime"
            empty_runtime.mkdir()
            with self.assertRaisesRegex(FileNotFoundError, "UBSan runtime library"):
                instrumented_dependency_discovery_slice(
                    repository,
                    toolchain,
                    variants=(
                        InstrumentedVariant(
                            "ubsan", "x64", empty_runtime, {"hash": "x"}
                        ),
                    ),
                )

            original = discovery.node("restore-vcpkg-x64")
            conflicting = Node(
                original.name,
                "0" * 32,
                original.pool,
                original.command,
                original.inputs,
            )
            conflict_graph = Graph(
                (conflicting,), (conflicting.name,), {"restore": 1, "slot": 2}
            )
            with (
                mock.patch(
                    "graphs.instrumented.clang_dependency_discovery_slice",
                    side_effect=(discovery, conflict_graph),
                ),
                self.assertRaisesRegex(ValueError, "conflicting canonical node"),
            ):
                instrumented_dependency_discovery_slice(
                    repository,
                    toolchain,
                    variants=(
                        InstrumentedVariant("coverage", "x64"),
                        InstrumentedVariant(
                            "ubsan", "x64", runtime,
                            {"standalone": "a", "cxx": "b"},
                        ),
                    ),
                )

    def test_source_toolchain_and_ubsan_runtime_identities_invalidate_exact_consumers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            runtime = self.llvm_runtime(root)
            variants = (
                InstrumentedVariant("coverage", "x64"),
                InstrumentedVariant("asan", "x64"),
                InstrumentedVariant("ubsan", "x64", runtime, {"hash": "before"}),
            )
            discovery = instrumented_dependency_discovery_slice(
                repository, toolchain, variants=variants
            )
            manifests = self.manifests(repository, discovery)
            before = instrumented_build_slice(
                repository,
                toolchain,
                discovery=discovery,
                manifests=manifests,
                variants=variants,
            )
            clang = toolchain.llvm_dir / "bin/clang-cl.exe"
            clang.write_bytes(b"clang-v2")
            clang_discovery = instrumented_dependency_discovery_slice(
                repository, toolchain, variants=variants
            )
            clang_changed = instrumented_build_slice(
                repository,
                toolchain,
                discovery=clang_discovery,
                manifests=manifests,
                variants=variants,
            )
            clang.write_bytes(b"clang-v1")
            scanner = toolchain.llvm_dir / "bin/clang-scan-deps.exe"
            scanner.write_bytes(b"scan-v2")
            scanner_discovery = instrumented_dependency_discovery_slice(
                repository, toolchain, variants=variants
            )
            scanner_changed = instrumented_build_slice(
                repository,
                toolchain,
                discovery=scanner_discovery,
                manifests=manifests,
                variants=variants,
            )
            scanner.write_bytes(b"scan-v1")
            changed_variants = (
                variants[0],
                variants[1],
                InstrumentedVariant("ubsan", "x64", runtime, {"hash": "after"}),
            )
            runtime_changed = instrumented_build_slice(
                repository,
                toolchain,
                discovery=discovery,
                manifests=manifests,
                variants=changed_variants,
            )
            (repository / "src/renpy.h").write_text(
                "#pragma once\n// changed\n", encoding="utf-8"
            )
            source_changed = instrumented_build_slice(
                repository,
                toolchain,
                discovery=discovery,
                manifests=manifests,
                variants=variants,
            )
            changed_toolchain = replace(
                toolchain, identity=toolchain.identity | {"llvm": "20.2"}
            )
            changed_discovery = instrumented_dependency_discovery_slice(
                repository, changed_toolchain, variants=variants
            )
            toolchain_changed = instrumented_build_slice(
                repository,
                changed_toolchain,
                discovery=changed_discovery,
                manifests=manifests,
                variants=variants,
            )

        for kind in ("coverage", "asan", "ubsan"):
            for project in ("renpy", "rpgmaker", "zanzarah", "tests"):
                name = f"build-{project}-x64-{kind}"
                self.assertEqual(
                    kind == "ubsan",
                    before.node(name).uid != runtime_changed.node(name).uid,
                    name,
                )
                self.assertEqual(
                    kind in {"coverage", "ubsan"},
                    before.node(name).uid != clang_changed.node(name).uid,
                    name,
                )
                self.assertEqual(
                    kind in {"coverage", "ubsan"},
                    before.node(name).uid != scanner_changed.node(name).uid,
                    name,
                )
                self.assertEqual(
                    project == "renpy",
                    before.node(name).uid != source_changed.node(name).uid,
                    name,
                )
                self.assertNotEqual(
                    before.node(name).uid, toolchain_changed.node(name).uid, name
                )

    def test_clang_discovery_skips_unsupported_leak_arch_and_signs_prior_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            source = repository / "src/leak-probe.cpp"
            name = dependency_node_name(
                repository, "x64", "leak-probe", source, "coverage"
            )
            manifest = json.dumps(
                {
                    "Data": {
                        "Source": str(source.resolve()),
                        "Includes": [str((repository / "src/tests.h").resolve())],
                    }
                }
            ).encode()
            with mock.patch("graphs.analysis._manifest_index", return_value={name: manifest}):
                discovery = clang_dependency_discovery_slice(
                    repository,
                    toolchain,
                    project_names=("leak-probe",),
                    configuration="Coverage",
                    name_qualifier="coverage",
                    architectures=("x86", "x64"),
                )
            with mock.patch("graphs.analysis._manifest_index", return_value={}):
                without_prior = clang_dependency_discovery_slice(
                    repository,
                    toolchain,
                    project_names=("leak-probe",),
                    configuration="Coverage",
                    name_qualifier="coverage",
                    architectures=("x86", "x64"),
                )
            asan = (InstrumentedVariant("asan", "x64"),)
            asan_discovery = instrumented_dependency_discovery_slice(
                repository, toolchain, variants=asan
            )
            instrumented_build_slice(
                repository,
                toolchain,
                discovery=asan_discovery,
                manifests=self.manifests(repository, asan_discovery),
                variants=asan,
            )

        self.assertEqual(discovery.targets, (name,))
        capture = discovery.node(discovery.node(name).inputs[0])
        previous_capture = without_prior.node(without_prior.node(name).inputs[0])
        self.assertNotEqual(capture.uid, previous_capture.uid)

if __name__ == "__main__":
    unittest.main(verbosity=2)

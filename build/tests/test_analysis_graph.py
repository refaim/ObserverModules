from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.paths import BuildPaths  # noqa: E402
from graphs.analysis import (  # noqa: E402
    _manifest_index,
    analysis_discovery_slice,
    analysis_slice,
    load_dependency_manifests,
)


@dataclass(frozen=True)
class FakeToolchain:
    msbuild: Path
    pwsh: Path
    vcpkg_root: Path
    llvm_dir: Path
    environment: tuple[tuple[str, str], ...]
    identity: dict[str, str]


class AnalysisSliceTests(unittest.TestCase):
    def repository(self, root: Path) -> Path:
        project = """<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup><ClCompile Include="$(RepositoryRoot){source}" /></ItemGroup>
</Project>
"""
        files = {
            "src/modules/renpy/pickle.cpp": '#include "pickle.h"\n',
            "src/modules/renpy/pickle.h": "#pragma once\n",
            "src/modules/rpgmaker/rpgmaker.cpp": "#include <zlib.h>\n",
            "build/projects/renpy.vcxproj": project.format(
                source="src/modules/renpy/pickle.cpp"
            ),
            "build/projects/rpgmaker.vcxproj": project.format(
                source="src/modules/rpgmaker/rpgmaker.cpp"
            ),
            "build/ObserverProjectConfigurations.props": "<Project />\n",
            "build/ObserverConfiguration.props": "<Project />\n",
            "build/ObserverProject.props": "<Project />\n",
            "build/ObserverNativeAnalysis.ruleset": "<RuleSet />\n",
            ".clang-tidy": "Checks: bugprone-*\n",
            "vcpkg.json": '{"dependencies":["zlib"]}\n',
            "build/vcpkg/triplets/observer-x64-windows-static.cmake": (
                "set(VCPKG_LIBRARY_LINKAGE static)\n"
            ),
            "build/vcpkg/triplets/observer-x86-windows-static.cmake": (
                "set(VCPKG_LIBRARY_LINKAGE static)\n"
            ),
        }
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    def toolchain(self, root: Path) -> FakeToolchain:
        tools = root / "fake tools"
        tools.mkdir()
        paths = {
            "msbuild": tools / "MSBuild.exe",
            "pwsh": tools / "pwsh.exe",
            "vcpkg_root": tools / "vcpkg",
            "llvm_dir": tools / "llvm",
        }
        paths["vcpkg_root"].mkdir()
        (paths["vcpkg_root"] / "vcpkg.exe").touch()
        paths["llvm_dir"].mkdir()
        paths["msbuild"].touch()
        paths["pwsh"].touch()
        return FakeToolchain(
            **paths,
            environment=(
                ("PATH", str(tools)),
                ("VCPKG_ROOT", str(tools / "vcpkg-current")),
            ),
            identity={"msbuild": "17.14", "msvc": "14.44", "llvm": "19.1.5"},
        )

    @staticmethod
    def manifest(source: Path, *includes: Path) -> bytes:
        return json.dumps(
            {
                "Version": "1.2",
                "Data": {
                    "Source": str(source.resolve()),
                    "ProvidedModule": "",
                    "ImportedModules": [],
                    "Includes": [str(path.resolve()) for path in includes],
                },
            },
            separators=(",", ":"),
        ).encode()

    def staged_graphs(
        self, repository: Path, toolchain: FakeToolchain, *, jobs: int = 2,
        architectures: tuple[str, ...] = ("x64",),
    ):
        discovery = analysis_discovery_slice(
            repository, toolchain, jobs=jobs, architectures=architectures
        )
        paths = BuildPaths(repository)
        manifests = {}
        for target in discovery.targets:
            source = repository / (
                "src/modules/renpy/pickle.cpp"
                if target.endswith("modules.renpy.pickle")
                else "src/modules/rpgmaker/rpgmaker.cpp"
            )
            includes = (
                (repository / "src/modules/renpy/pickle.h",)
                if target.endswith("modules.renpy.pickle")
                else ()
            )
            if "-rpgmaker-" in target:
                architecture = target.removeprefix("discover-dependencies-").split("-", 1)[0]
                restore = discovery.node(f"restore-vcpkg-{architecture}")
                package = paths.cas(restore.uid, restore.name).output / "include/zlib.h"
                package.parent.mkdir(parents=True, exist_ok=True)
                if not package.exists():
                    package.write_text("#pragma once\n", encoding="utf-8")
                includes = (package,)
            manifests[target] = self.manifest(source, *includes)
        graph = analysis_slice(
            repository,
            toolchain,
            discovery=discovery,
            manifests=manifests,
            jobs=jobs,
            architectures=architectures,
        )
        return discovery, graph

    def test_two_analysis_backends_are_independent_demand_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            discovery, graph = self.staged_graphs(repository, self.toolchain(root))

        self.assertEqual(
            graph.targets,
            ("analysis-x64",),
        )
        self.assertEqual(graph.pools, {"misc": 2, "restore": 1, "slot": 2})
        self.assertEqual(
            tuple(node.name for node in graph.nodes),
            (
                "restore-vcpkg-x64",
                "discover-dependencies-x64-renpy-modules.renpy.pickle",
                "discover-dependencies-x64-rpgmaker-modules.rpgmaker.rpgmaker",
                "analyze-msvc-x64-renpy-modules.renpy.pickle",
                "analyze-tidy-x64-renpy-modules.renpy.pickle",
                "normalize-msvc-x64-renpy-modules.renpy.pickle",
                "normalize-tidy-x64-renpy-modules.renpy.pickle",
                "analyze-msvc-x64-rpgmaker-modules.rpgmaker.rpgmaker",
                "analyze-tidy-x64-rpgmaker-modules.rpgmaker.rpgmaker",
                "normalize-msvc-x64-rpgmaker-modules.rpgmaker.rpgmaker",
                "normalize-tidy-x64-rpgmaker-modules.rpgmaker.rpgmaker",
                "merge-analysis-x64",
                "analysis-x64",
            ),
        )
        restore = graph.nodes[0]
        self.assertEqual(dict(restore.command.env)["VCPKG_ROOT"], str(root / "fake tools/vcpkg"))
        raw = (graph.nodes[3], graph.nodes[4], graph.nodes[7], graph.nodes[8])
        for node in raw:
            self.assertEqual(node.pool, "slot")
            self.assertEqual(node.command.cwd, str(repository.resolve()))
            self.assertEqual(dict(node.command.env)["PATH"], str(root / "fake tools"))
            script = node.command.stdin.decode("utf-8")
            self.assertIn("'/t:ClCompile'", script)
            self.assertIn("'/p:Configuration=Debug'", script)
            self.assertIn("'/p:Platform=x64'", script)
            self.assertIn("'/p:SelectedFiles=", script)
            self.assertIn("'/p:VcpkgRoot=", script)
        self.assertEqual(raw[0].inputs, (discovery.targets[0],))
        self.assertEqual(raw[1].inputs, (discovery.targets[0],))
        self.assertEqual(raw[2].inputs, (discovery.targets[1],))
        self.assertEqual(raw[3].inputs, (discovery.targets[1],))
        msvc = raw[0].command.stdin.decode("utf-8")
        tidy = raw[1].command.stdin.decode("utf-8")
        self.assertIn('"/p:ObserverAnalysisReportPath=$outDir\\renpy.sarif"', msvc)
        self.assertIn('"/p:IntDir=$buildDir\\"', msvc)
        self.assertIn("MSVC analysis did not produce renpy.sarif", msvc)
        self.assertIn(")\nif (-not (Test-Path", msvc)
        self.assertIn('"/p:IntDir=$outDir\\obj\\"', tidy)
        self.assertIn("'/p:LLVMInstallDir=", tidy)
        self.assertIn("clang-tidy did not produce renpy.ClangTidy.log", tidy)
        self.assertIn(")\nif (-not (Test-Path", tidy)
        normalize_msvc, normalize_tidy = graph.nodes[5:7]
        rpg_normalize_msvc, rpg_normalize_tidy, merge, gate = graph.nodes[9:]
        self.assertEqual(normalize_msvc.inputs, (raw[0].name,))
        self.assertEqual(normalize_tidy.inputs, (raw[1].name,))
        self.assertEqual(rpg_normalize_msvc.inputs, (raw[2].name,))
        self.assertEqual(rpg_normalize_tidy.inputs, (raw[3].name,))
        self.assertEqual(
            merge.inputs,
            (
                normalize_msvc.name,
                normalize_tidy.name,
                rpg_normalize_msvc.name,
                rpg_normalize_tidy.name,
            ),
        )
        self.assertEqual(gate.inputs, (merge.name,))
        self.assertEqual(normalize_msvc.command.argv[1:4], ("-m", "core.sarif", "normalize-msvc"))
        self.assertEqual(normalize_tidy.command.argv[1:4], ("-m", "core.sarif", "convert-tidy"))
        self.assertEqual(merge.command.argv[1:4], ("-m", "core.sarif", "merge"))
        self.assertEqual(gate.command.argv[1:4], ("-m", "core.sarif", "gate"))
        self.assertTrue(
            all(node.pool == "misc" for node in graph.nodes[5:7] + graph.nodes[9:])
        )

    def test_tidy_configuration_invalidates_only_tidy_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            _before_discovery, before = self.staged_graphs(repository, toolchain)
            (repository / ".clang-tidy").write_text(
                "Checks: bugprone-*,performance-*\n", encoding="utf-8"
            )
            _after_discovery, after = self.staged_graphs(repository, toolchain)

        unchanged = (
            "restore-vcpkg-x64",
            "analyze-msvc-x64-renpy-modules.renpy.pickle",
            "normalize-msvc-x64-renpy-modules.renpy.pickle",
            "analyze-msvc-x64-rpgmaker-modules.rpgmaker.rpgmaker",
            "normalize-msvc-x64-rpgmaker-modules.rpgmaker.rpgmaker",
        )
        changed = (
            "analyze-tidy-x64-renpy-modules.renpy.pickle",
            "normalize-tidy-x64-renpy-modules.renpy.pickle",
            "analyze-tidy-x64-rpgmaker-modules.rpgmaker.rpgmaker",
            "normalize-tidy-x64-rpgmaker-modules.rpgmaker.rpgmaker",
            "merge-analysis-x64",
            "analysis-x64",
        )
        for name in unchanged:
            self.assertEqual(before.node(name).uid, after.node(name).uid)
        for name in changed:
            self.assertNotEqual(before.node(name).uid, after.node(name).uid)

    def test_dynamic_include_forms_are_deferred_to_msvc_dependency_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            forms = (
                "#define HEADER \"pickle.h\"\n#include HEADER\n",
                '#include_next "pickle.h"\n',
                '#if __has_include("pickle.h")\n#include "pickle.h"\n#endif\n',
            )
            for source in forms:
                with self.subTest(source=source):
                    (repository / "src/modules/renpy/pickle.cpp").write_text(
                        source, encoding="utf-8"
                    )
                    graph = analysis_discovery_slice(repository, toolchain)

                    discovery = graph.node(
                        "discover-dependencies-x64-renpy-modules.renpy.pickle"
                    )
                    script = discovery.command.stdin.decode("utf-8")
                    self.assertIn("/p:ObserverSourceDependenciesPath=", script)
                    self.assertIn("dependencies.json", script)
                    self.assertNotIn("ObserverRunCodeAnalysis", script)

    def test_architecture_selects_distinct_platform_triplet_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            discovery, graph = self.staged_graphs(
                repository, self.toolchain(root), architectures=("x86",)
            )

        self.assertEqual(graph.targets, ("analysis-x86",))
        self.assertEqual(graph.nodes[0].name, "restore-vcpkg-x86")
        self.assertIn("observer-x86-windows-static", graph.nodes[0].command.stdin.decode())
        raw_script = graph.nodes[3].command.stdin.decode()
        self.assertIn("'/p:Platform=Win32'", raw_script)
        self.assertIn("analyze-msvc-x86-renpy", graph.nodes[3].name)

    def test_phase_two_signs_only_compiler_reported_project_and_package_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            unrelated = repository / "src/unused.h"
            unrelated.write_text("one\n", encoding="utf-8")
            toolchain = self.toolchain(root)
            _discovery, baseline = self.staged_graphs(repository, toolchain)
            unrelated.write_text("two\n", encoding="utf-8")
            _discovery, unrelated_changed = self.staged_graphs(repository, toolchain)
            (repository / "src/modules/renpy/pickle.h").write_text(
                "#pragma once\n// changed\n", encoding="utf-8"
            )
            _discovery, header_changed = self.staged_graphs(repository, toolchain)
            package = next((repository / "out/cas").glob("*-restore-vcpkg-x64"))
            (package / "out/include/zlib.h").write_text("// changed\n", encoding="utf-8")
            _discovery, package_changed = self.staged_graphs(repository, toolchain)

        names = {
            "renpy": "analyze-msvc-x64-renpy-modules.renpy.pickle",
            "rpgmaker": "analyze-msvc-x64-rpgmaker-modules.rpgmaker.rpgmaker",
        }
        for name in names.values():
            self.assertEqual(baseline.node(name).uid, unrelated_changed.node(name).uid)
        self.assertNotEqual(
            unrelated_changed.node(names["renpy"]).uid,
            header_changed.node(names["renpy"]).uid,
        )
        self.assertEqual(
            unrelated_changed.node(names["rpgmaker"]).uid,
            header_changed.node(names["rpgmaker"]).uid,
        )
        self.assertNotEqual(
            header_changed.node(names["rpgmaker"]).uid,
            package_changed.node(names["rpgmaker"]).uid,
        )

    def test_published_manifests_are_loaded_by_exact_discovery_uid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            discovery = analysis_discovery_slice(repository, self.toolchain(root))
            paths = BuildPaths(repository)
            expected = {}
            for target in discovery.targets:
                node = discovery.node(target)
                cas = paths.cas(node.uid, node.name)
                cas.output.mkdir(parents=True)
                source = repository / (
                    "src/modules/renpy/pickle.cpp"
                    if target.endswith("modules.renpy.pickle")
                    else "src/modules/rpgmaker/rpgmaker.cpp"
                )
                content = self.manifest(source)
                (cas.output / "dependencies.json").write_bytes(content)
                cas.touch.touch()
                expected[target] = content

            loaded = load_dependency_manifests(repository, discovery)

        self.assertEqual(loaded, expected)

    def test_manifest_index_keeps_newest_candidate_regardless_of_enumeration_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            paths = BuildPaths(repository)
            paths.prepare()
            name = "discover-dependencies-x64-renpy-modules.renpy.pickle"
            older = paths.cas("1" * 32, name)
            newer = paths.cas("2" * 32, name)
            for cas, content, timestamp in (
                (older, b"older", 100),
                (newer, b"newer", 200),
            ):
                cas.output.mkdir(parents=True)
                (cas.output / "dependencies.json").write_bytes(content)
                cas.touch.touch()
                os.utime(cas.touch, ns=(timestamp, timestamp))

            with mock.patch.object(Path, "glob", return_value=(newer.entry, older.entry)):
                loaded = _manifest_index(repository, {name})

        self.assertEqual(loaded, {name: b"newer"})

    def test_incomplete_dependency_discovery_is_not_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            discovery = analysis_discovery_slice(repository, self.toolchain(root))

            with self.assertRaisesRegex(FileNotFoundError, "dependency discovery is incomplete"):
                load_dependency_manifests(repository, discovery)

    def test_prior_manifest_dependencies_seed_only_their_discovery_uid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            initial = analysis_discovery_slice(repository, toolchain)
            renpy = initial.node(initial.targets[0])
            cas = BuildPaths(repository).cas(renpy.uid, renpy.name)
            cas.output.mkdir(parents=True)
            (cas.output / "dependencies.json").write_bytes(
                self.manifest(
                    repository / "src/modules/renpy/pickle.cpp",
                    repository / "src/modules/renpy/pickle.h",
                )
            )
            cas.touch.touch()
            other = BuildPaths(repository).cas("1" * 32, renpy.name)
            other.output.mkdir(parents=True)
            (other.output / "dependencies.json").write_bytes(
                (cas.output / "dependencies.json").read_bytes()
            )
            other.touch.touch()
            BuildPaths(repository).cas("2" * 32, renpy.name).entry.mkdir(parents=True)
            before = analysis_discovery_slice(repository, toolchain)
            (repository / "src/modules/renpy/pickle.h").write_text(
                "#pragma once\n// topology may have changed\n", encoding="utf-8"
            )
            after = analysis_discovery_slice(repository, toolchain)

        self.assertNotEqual(before.node(before.targets[0]).uid, after.node(after.targets[0]).uid)
        self.assertEqual(before.node(before.targets[1]).uid, after.node(after.targets[1]).uid)

    def test_source_namespace_addition_invalidates_discovery_for_has_include(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            before = analysis_discovery_slice(repository, toolchain)
            (repository / "src/optional.h").write_text("#pragma once\n", encoding="utf-8")
            after = analysis_discovery_slice(repository, toolchain)

        for name in before.targets:
            self.assertNotEqual(before.node(name).uid, after.node(name).uid)

    def test_malformed_cas_candidate_is_ignored_during_discovery_seed_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            name = "discover-dependencies-x64-renpy-modules.renpy.pickle"
            (repository / "out/cas" / f"{'z' * 32}-{name}").mkdir(parents=True)
            (repository / "out/cas" / f"{'3' * 32}-discover-dependencies-x64-unused-unit").mkdir()

            graph = analysis_discovery_slice(repository, self.toolchain(root))

        self.assertIn(name, graph.targets)

    def test_x64_only_leak_probe_is_not_scheduled_for_cross_architectures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            (repository / "build/projects/leak-probe.vcxproj").write_bytes(
                (repository / "build/projects/renpy.vcxproj").read_bytes()
            )
            toolchain = self.toolchain(root)
            _x86_discovery, x86 = self.staged_graphs(
                repository, toolchain, architectures=("x86",)
            )
            _x64_discovery, x64 = self.staged_graphs(
                repository, toolchain, architectures=("x64",)
            )

        self.assertFalse(any("-leak-probe-" in node.name for node in x86.nodes))
        self.assertTrue(any("-leak-probe-" in node.name for node in x64.nodes))


if __name__ == "__main__":
    unittest.main(verbosity=2)

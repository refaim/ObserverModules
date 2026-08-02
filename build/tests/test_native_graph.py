from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from graphs.native import native_dependency_discovery_slice, native_graph  # noqa: E402


@dataclass(frozen=True)
class FakeToolchain:
    msbuild: Path
    pwsh: Path
    vcpkg_root: Path
    environment: tuple[tuple[str, str], ...]
    identity: dict[str, str]


class NativeGraphTests(unittest.TestCase):
    def test_parallel_msvc_builds_do_not_depend_on_shared_compiler_pdb_server(
        self,
    ) -> None:
        root = ET.parse(
            BUILD_ROOT / "ObserverProject.props"
        ).getroot()
        debug_information = root.find(
            ".//{http://schemas.microsoft.com/developer/msbuild/2003}DebugInformationFormat"
        )

        self.assertIsNotNone(debug_information)
        self.assertEqual(debug_information.text, "OldStyle")

    def repository(self, root: Path) -> Path:
        project = """<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup>
    <ClCompile Include="$(RepositoryRoot)src\\{name}.cpp" />
    <ClInclude Include="$(RepositoryRoot)src\\{name}.h" />
  </ItemGroup>
  {definition}
</Project>
"""
        files = {
            "src/renpy.cpp": '#include "renpy.h"\n#include <zlib.h>\n',
            "src/renpy.h": "#pragma once\n",
            "src/rpgmaker.cpp": '#include "rpgmaker.h"\n',
            "src/rpgmaker.h": "#pragma once\n",
            "src/zanzarah.cpp": '#include "zanzarah.h"\n',
            "src/zanzarah.h": "#pragma once\n",
            "src/tests.cpp": (
                '#include "tests.h"\n#include <catch2/catch_test_macros.hpp>\n'
            ),
            "src/tests.h": "#pragma once\n",
            "src/leak-probe.cpp": '#include "leak-probe.h"\n#include <zlib.h>\n',
            "src/leak-probe.h": "#pragma once\n",
            "src/renpy.def": "EXPORTS\n  OpenStorage\n",
            "build/ObserverProjectConfigurations.props": "<Project />\n",
            "build/ObserverConfiguration.props": "<Project />\n",
            "build/ObserverProject.props": "<Project />\n",
            "vcpkg.json": '{"dependencies":["zlib"]}\n',
        }
        for architecture in ("x64", "arm64"):
            files[f"build/vcpkg/triplets/observer-{architecture}-windows-static.cmake"] = (
                "set(VCPKG_LIBRARY_LINKAGE static)\n"
            )
        for name in ("renpy", "rpgmaker", "zanzarah", "tests", "leak-probe"):
            definition = (
                "<ItemDefinitionGroup><Link><ModuleDefinitionFile>"
                "$(RepositoryRoot)src\\renpy.def</ModuleDefinitionFile></Link>"
                "</ItemDefinitionGroup>"
                if name == "renpy"
                else ""
            )
            files[f"build/projects/{name}.vcxproj"] = project.format(
                name=name, definition=definition
            )
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    def toolchain(self, root: Path) -> FakeToolchain:
        tools = root / "fake tools"
        tools.mkdir()
        vcpkg_root = tools / "vcpkg"
        vcpkg_root.mkdir()
        (vcpkg_root / "vcpkg.exe").touch()
        msbuild = tools / "MSBuild.exe"
        pwsh = tools / "pwsh.exe"
        msbuild.touch()
        pwsh.touch()
        return FakeToolchain(
            msbuild=msbuild,
            pwsh=pwsh,
            vcpkg_root=vcpkg_root,
            environment=(("PATH", str(tools)),),
            identity={"msbuild": "17.14", "msvc": "14.44"},
        )

    def staged_graphs(self, repository: Path, toolchain: FakeToolchain, **options):
        discovery = native_dependency_discovery_slice(
            repository,
            toolchain,
            jobs=options.get("jobs", 2),
            architectures=options.get("architectures", ("x64",)),
            configurations=options.get("configurations", ("Debug",)),
            include_leak_probe=options.get("include_leak_probe", False),
        )
        manifests = {}
        for target in discovery.targets:
            project = next(
                name
                for name in ("renpy", "rpgmaker", "zanzarah", "tests", "leak-probe")
                if target.endswith(f"-{name}")
            )
            source = repository / f"src/{project}.cpp"
            manifests[target] = json.dumps(
                {
                    "Data": {
                        "Source": str(source.resolve()),
                        "Includes": [str((repository / f"src/{project}.h").resolve())],
                    }
                }
            ).encode()
        return discovery, native_graph(
            repository, toolchain, discovery=discovery, manifests=manifests, **options
        )

    def test_projects_restore_and_test_shards_form_a_fine_grained_dag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            discovery, graph = self.staged_graphs(
                repository,
                self.toolchain(root),
                architectures=("x64", "arm64"),
                configurations=("Debug", "Release"),
                runnable_architectures=("x64",),
                test_shards=2,
                jobs=8,
            )

        names = tuple(node.name for node in graph.nodes)
        self.assertEqual(names.count("restore-vcpkg-x64"), 1)
        self.assertEqual(names.count("restore-vcpkg-arm64"), 1)
        self.assertIn("build-renpy-x64-debug", names)
        self.assertIn("build-tests-arm64-release", names)
        self.assertFalse(any("leak-probe" in name for name in names))
        self.assertEqual(
            tuple(name for name in names if name.startswith("test-shard-")),
            (
                "test-shard-x64-debug-0",
                "test-shard-x64-debug-1",
                "test-shard-x64-release-0",
                "test-shard-x64-release-1",
            ),
        )
        self.assertEqual(graph.pools, {"restore": 1, "slot": 8})

        restore = graph.node("restore-vcpkg-x64")
        builds = tuple(
            graph.node(f"build-{project}-x64-debug")
            for project in ("renpy", "rpgmaker", "zanzarah", "tests")
        )
        expected = tuple(
            next(name for name in discovery.targets if f"-debug-{project}-" in name)
            for project in ("renpy", "rpgmaker", "zanzarah", "tests")
        )
        self.assertEqual(tuple(node.inputs[0] for node in builds), expected)
        self.assertTrue(all(node.pool == "slot" for node in builds))
        for node in builds:
            script = node.command.stdin.decode("utf-8")
            self.assertIn("'/m:1'", script)
            self.assertIn("'/p:BuildProjectReferences=false'", script)
            self.assertIn('"/p:OutDir=$outDir\\"', script)
            self.assertIn('"/p:IntDir=$buildDir\\"', script)
        for node in builds:
            self.assertIn("'/p:VcpkgInstalledDir=", node.command.stdin.decode())
        shard = graph.node("test-shard-x64-debug-0")
        self.assertEqual(
            shard.inputs,
            tuple(node.name for node in builds),
        )
        shard_script = shard.command.stdin.decode("utf-8")
        for artifact in ("renpy.so", "rpgmaker.so", "zanzarah.so", "tests.exe"):
            self.assertIn(artifact, shard_script)
        self.assertIn("'--shard-count'", shard_script)
        self.assertIn("'2'", shard_script)
        self.assertIn("'--shard-index'", shard_script)
        self.assertIn("'0'", shard_script)
        self.assertIn("'JUnit::out=", shard_script)

    def test_leak_probe_is_an_explicit_release_only_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            discovery, graph = self.staged_graphs(
                repository,
                self.toolchain(root),
                configurations=("Debug", "Release"),
                runnable_architectures=(),
                include_leak_probe=True,
            )

        names = tuple(node.name for node in graph.nodes)
        self.assertIn("build-leak-probe-x64-release", names)
        self.assertNotIn("build-leak-probe-x64-debug", names)
        leak_probe = graph.node("build-leak-probe-x64-release")
        self.assertEqual(len(leak_probe.inputs), 1)
        self.assertIn("-release-leak-probe-", leak_probe.inputs[0])
        self.assertTrue(any("-release-leak-probe-" in name for name in discovery.targets))

    def test_external_corpus_is_test_only_and_nonce_invalidated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            corpus = root / "corpus"
            other_corpus = root / "other-corpus"
            corpus.mkdir()
            other_corpus.mkdir()
            options = dict(
                configurations=("Debug",), runnable_architectures=("x64",), test_shards=1
            )
            _discovery, baseline = self.staged_graphs(repository, toolchain, **options)
            _discovery, no_corpus = self.staged_graphs(
                repository, toolchain, corpus=None, run_nonce="ignored", **options
            )
            _discovery, first = self.staged_graphs(
                repository, toolchain, corpus=corpus, run_nonce="run-one", **options
            )
            (corpus / "large-external-file.bin").write_bytes(b"not signed")
            _discovery, content_changed = self.staged_graphs(
                repository, toolchain, corpus=corpus, run_nonce="run-one", **options
            )
            _discovery, rerun = self.staged_graphs(
                repository, toolchain, corpus=corpus, run_nonce="run-two", **options
            )
            _discovery, moved = self.staged_graphs(
                repository, toolchain, corpus=other_corpus, run_nonce="run-one", **options
            )
            _discovery, build_only = self.staged_graphs(
                repository, toolchain, corpus=corpus, run_nonce="build-only",
                configurations=("Release",), runnable_architectures=(),
            )

        shard_name = "test-shard-x64-debug-0"
        corpus_name = "corpus-shard-x64-debug-0"
        self.assertEqual(baseline.node(shard_name).uid, no_corpus.node(shard_name).uid)
        self.assertEqual(baseline.node(shard_name).uid, first.node(shard_name).uid)
        self.assertFalse(any(node.name.startswith("corpus-shard-") for node in baseline.nodes))
        self.assertEqual(first.node(corpus_name).uid, content_changed.node(corpus_name).uid)
        self.assertNotEqual(first.node(corpus_name).uid, rerun.node(corpus_name).uid)
        self.assertNotEqual(first.node(corpus_name).uid, moved.node(corpus_name).uid)
        self.assertEqual(first.node(corpus_name).inputs, first.node(shard_name).inputs)
        self.assertEqual(dict(first.node(corpus_name).command.env)["OBSERVER_TEST_CORPUS"], str(corpus.resolve()))
        self.assertNotIn("OBSERVER_TEST_CORPUS", dict(first.node(shard_name).command.env))
        self.assertIn("'[compatibility]'", first.node(corpus_name).command.stdin.decode())
        self.assertTrue(all(
            "OBSERVER_TEST_CORPUS" not in dict(node.command.env)
            for node in first.nodes if not node.name.startswith("corpus-shard-")
        ))
        self.assertFalse(any(
            node.name.startswith(("test-shard-", "corpus-")) for node in build_only.nodes
        ))

    def test_project_content_invalidates_only_its_build_and_consuming_shards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            options = dict(
                architectures=("x64",),
                configurations=("Debug",),
                runnable_architectures=("x64",),
                test_shards=2,
            )
            _discovery, before = self.staged_graphs(repository, toolchain, **options)
            (repository / "src/renpy.cpp").write_text(
                '#include "renpy.h"\n#include <zlib.h>\n// changed\n', encoding="utf-8"
            )
            _discovery, after = self.staged_graphs(repository, toolchain, **options)
            (repository / "src/rpgmaker.h").write_text(
                "#pragma once\n#include <new_package/header.h>\n", encoding="utf-8"
            )
            _discovery, unknown_package = self.staged_graphs(
                repository, toolchain, **options
            )

        for name in (
            "restore-vcpkg-x64",
            "build-rpgmaker-x64-debug",
            "build-zanzarah-x64-debug",
            "build-tests-x64-debug",
        ):
            self.assertEqual(before.node(name).uid, after.node(name).uid)
        self.assertNotEqual(
            before.node("build-renpy-x64-debug").uid,
            after.node("build-renpy-x64-debug").uid,
        )
        for index in range(2):
            name = f"test-shard-x64-debug-{index}"
            self.assertNotEqual(before.node(name).uid, after.node(name).uid)
        rpgmaker = "build-rpgmaker-x64-debug"
        self.assertEqual(len(unknown_package.node(rpgmaker).inputs), 1)
        self.assertNotEqual(after.node(rpgmaker).uid, unknown_package.node(rpgmaker).uid)

    def test_invalid_axes_are_rejected_before_graph_construction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            with self.assertRaisesRegex(ValueError, "unsupported architecture"):
                native_graph(
                    repository,
                    toolchain,
                    discovery=None,
                    manifests={},
                    architectures=("mips",),
                    runnable_architectures=(),
                )
            with self.assertRaisesRegex(ValueError, "unsupported configuration"):
                native_graph(
                    repository,
                    toolchain,
                    discovery=None,
                    manifests={},
                    configurations=("RelWithDebInfo",),
                    runnable_architectures=(),
                )
            with self.assertRaisesRegex(ValueError, "test_shards"):
                native_graph(
                    repository,
                    toolchain,
                    discovery=None,
                    manifests={},
                    runnable_architectures=("x64",),
                    test_shards=0,
                )
            with self.assertRaisesRegex(ValueError, "unsupported configuration"):
                native_dependency_discovery_slice(
                    repository, toolchain, configurations=("RelWithDebInfo",)
                )
            with self.assertRaisesRegex(ValueError, "unsupported architecture"):
                native_graph(
                    repository, toolchain, discovery=None, manifests={},
                    runnable_architectures=("mips",),
                )
            with self.assertRaisesRegex(ValueError, "dependency discovery is required"):
                native_graph(
                    repository, toolchain, discovery=None, manifests={},
                    runnable_architectures=(),
                )
            corpus = root / "corpus"
            corpus.mkdir()
            with self.assertRaisesRegex(ValueError, "run nonce"):
                native_graph(
                    repository, toolchain, discovery=None, manifests={},
                    runnable_architectures=("x64",), corpus=corpus, run_nonce="",
                )
            with self.assertRaises(FileNotFoundError):
                native_graph(
                    repository, toolchain, discovery=None, manifests={},
                    runnable_architectures=("x64",), corpus=root / "missing", run_nonce="run",
                )
            file_corpus = root / "corpus.bin"
            file_corpus.touch()
            with self.assertRaises(NotADirectoryError):
                native_graph(
                    repository, toolchain, discovery=None, manifests={},
                    runnable_architectures=("x64",), corpus=file_corpus, run_nonce="run",
                )

    def test_native_build_requires_every_compiler_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            discovery = native_dependency_discovery_slice(repository, toolchain)

            with self.assertRaisesRegex(ValueError, "missing dependency manifest"):
                native_graph(
                    repository, toolchain, discovery=discovery, manifests={},
                    runnable_architectures=(),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)

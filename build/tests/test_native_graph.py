from __future__ import annotations

import json
import unittest
from pathlib import Path

from build import native_graph


WORKSPACE = Path(__file__).resolve().parents[2]


class NativeGraphManifestTests(unittest.TestCase):
    def test_exact_project_and_translation_unit_manifest(self) -> None:
        manifest = native_graph.load_manifest(WORKSPACE)

        self.assertEqual(
            [project.name for project in manifest.projects],
            [
                "renpy",
                "rpgmaker",
                "zanzarah",
                "tests",
                "fuzz-pickle",
                "fuzz-renpy",
                "fuzz-rpgmaker",
                "fuzz-zanzarah",
                "leak-probe",
            ],
        )
        self.assertEqual(
            [len(project.translation_units) for project in manifest.projects],
                [6, 4, 4, 15, 2, 5, 3, 3, 5],
            )
        self.assertEqual(len(manifest.translation_units), 47)
        self.assertEqual(len({unit.key for unit in manifest.translation_units}), 47)

    def test_manifest_rejects_a_project_outside_the_explicit_allowlist(self) -> None:
        with self.assertRaisesRegex(native_graph.NativeGraphError, "unknown project"):
            native_graph.load_manifest(WORKSPACE, project_names=("renpy", "not-a-project"))

    def test_manifest_paths_are_repository_relative_existing_cpp_files(self) -> None:
        manifest = native_graph.load_manifest(WORKSPACE)

        for unit in manifest.translation_units:
            self.assertFalse(unit.source.is_absolute())
            self.assertEqual(unit.source.suffix, ".cpp")
            self.assertTrue((WORKSPACE / unit.source).is_file())
            self.assertNotIn("..", unit.source.parts)

    def test_item_definition_clcompile_is_not_a_translation_unit(self) -> None:
        manifest = native_graph.load_manifest(WORKSPACE)
        leak_probe = next(project for project in manifest.projects if project.name == "leak-probe")

        self.assertEqual(
            [unit.source.as_posix() for unit in leak_probe.translation_units],
            [
                "src/core/io/bounded_stream.cpp",
                "src/tests/leaks/probe.cpp",
                "src/core/compression/zlib_codec.cpp",
                "src/tests/support/archive_fixtures.cpp",
                "src/tests/support/zlib_fixture.cpp",
            ],
        )


class NativeGraphExpansionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = native_graph.load_manifest(WORKSPACE)
        cls.nodes = native_graph.expand_x64_nodes(cls.manifest)
        cls.by_name = {node["name"]: node for node in cls.nodes}

    def test_native_build_and_test_execution_are_separate_nodes(self) -> None:
        build_nodes = [node for node in self.nodes if node["name"].startswith("build-")]
        run_nodes = [node for node in self.nodes if node["name"].startswith("run-tests-")]

        self.assertEqual(len(build_nodes), 25)
        self.assertEqual(len(run_nodes), 5)
        for configuration in ("debug", "release", "coverage", "asan", "ubsan"):
            runner = self.by_name[f"run-tests-{configuration}"]
            self.assertEqual(
                runner["deps"],
                [
                    f"build-{configuration}-renpy",
                    f"build-{configuration}-rpgmaker",
                    f"build-{configuration}-tests",
                    f"build-{configuration}-zanzarah",
                ],
            )
            self.assertEqual(runner["resources"], {"cpu": 1, "test-run": 1})

        fuzz_builds = [node for node in build_nodes if node["name"].startswith("build-fuzz-")]
        self.assertEqual(len(fuzz_builds), 4)
        self.assertFalse(any(node["name"].startswith("run-fuzz-") for node in self.nodes))

    def test_fuzz_build_names_have_one_exported_cross_graph_contract(self) -> None:
        targets = ("pickle", "renpy", "rpgmaker", "zanzarah")

        self.assertEqual(
            {native_graph.fuzz_build_node_name(target) for target in targets},
            {
                node["name"]
                for node in self.nodes
                if node["name"].startswith("build-fuzz-")
            },
        )
        with self.assertRaisesRegex(native_graph.NativeGraphError, "unknown fuzz target"):
            native_graph.fuzz_build_node_name("unknown")

    def test_release_build_names_have_one_exported_cross_graph_contract(self) -> None:
        expected = {
            native_graph.build_project_node_name("Release", project)
            for project in ("renpy", "rpgmaker", "zanzarah", "tests", "leak-probe")
        }

        self.assertTrue(expected.issubset(self.by_name))
        with self.assertRaisesRegex(native_graph.NativeGraphError, "unsupported project build"):
            native_graph.build_project_node_name("ASan", "leak-probe")

    def test_analysis_expands_to_47_msvc_and_47_tidy_units(self) -> None:
        msvc_nodes = [node for node in self.nodes if node["name"].startswith("analyze-msvc-")]
        tidy_nodes = [node for node in self.nodes if node["name"].startswith("analyze-tidy-")]
        normalize_msvc = [node for node in self.nodes if node["name"].startswith("normalize-msvc-")]
        normalize_tidy = [node for node in self.nodes if node["name"].startswith("normalize-tidy-")]

        self.assertEqual(len(msvc_nodes), 47)
        self.assertEqual(len(tidy_nodes), 47)
        self.assertEqual(len(normalize_msvc), 47)
        self.assertEqual(len(normalize_tidy), 47)
        self.assertEqual(len(self.by_name["merge-msvc-sarif"]["deps"]), 47)
        self.assertEqual(len(self.by_name["merge-tidy-sarif"]["deps"]), 47)
        self.assertEqual(
            self.by_name["analysis-gate"]["deps"],
            ["merge-msvc-sarif", "merge-tidy-sarif"],
        )

    def test_selected_file_tidy_uses_isolated_intdir_and_exact_source(self) -> None:
        node = next(
            node
            for node in self.nodes
            if node["name"].startswith("analyze-tidy-renpy-")
            and any("src/core/io/bounded_stream.cpp" in argument for argument in node["argv"])
        )

        self.assertIn("-SelectedFile", node["argv"])
        selected_index = node["argv"].index("-SelectedFile") + 1
        self.assertEqual(node["argv"][selected_index], "src/core/io/bounded_stream.cpp")
        self.assertEqual(node["resources"], {"clang-tidy": 1, "cpu": 1})
        self.assertEqual(len(node["writes"]), 1)
        self.assertTrue(node["writes"][0].startswith(".artifacts/analysis/clang-tidy/x64/renpy/"))
        self.assertTrue(node["writes"][0].endswith("/"))
        self.assertEqual(
            node["outputs"],
            [f"{node['writes'][0]}obj/renpy.ClangTidy.log"],
        )

    def test_selected_file_msvc_uses_isolated_intdir_and_exact_source(self) -> None:
        node = next(
            node
            for node in self.nodes
            if node["name"].startswith("analyze-msvc-renpy-")
            and any("src/core/io/bounded_stream.cpp" in argument for argument in node["argv"])
        )

        self.assertIn("-SelectedFile", node["argv"])
        selected_index = node["argv"].index("-SelectedFile") + 1
        self.assertEqual(node["argv"][selected_index], "src/core/io/bounded_stream.cpp")
        self.assertEqual(node["resources"], {"cpu": 1, "memory-gib": 2, "msvc-analysis": 1})
        self.assertEqual(len(node["writes"]), 1)
        self.assertTrue(node["writes"][0].startswith(".artifacts/analysis/msvc/x64/renpy/"))
        self.assertTrue(node["writes"][0].endswith("/"))

    def test_sarif_merges_receive_exact_normalizer_outputs_without_globs(self) -> None:
        for backend in ("msvc", "tidy"):
            normalizers = [
                node
                for node in self.nodes
                if node["name"].startswith(f"normalize-{backend}-")
            ]
            expected = [node["outputs"][0] for node in normalizers]
            merge = self.by_name[f"merge-{backend}-sarif"]
            argument_index = merge["argv"].index("-InputPathsJson") + 1

            self.assertEqual(merge["inputs"], expected)
            self.assertEqual(json.loads(merge["argv"][argument_index]), expected)
            self.assertFalse(any("*" in path or "?" in path for path in merge["inputs"]))

    def test_msvc_and_tidy_writes_are_disjoint_from_debug_build(self) -> None:
        analysis_writes = {
            write
            for node in self.nodes
            if node["name"].startswith(("analyze-msvc-", "analyze-tidy-"))
            for write in node["writes"]
        }
        debug_writes = {
            write
            for node in self.nodes
            if node["name"].startswith("build-debug-")
            for write in node["writes"]
        }

        self.assertTrue(analysis_writes)
        self.assertTrue(debug_writes)
        self.assertTrue(analysis_writes.isdisjoint(debug_writes))
        for node in self.nodes:
            for write in node["writes"]:
                self.assertTrue(write.startswith(".artifacts/"), (node["name"], write))

    def test_normalized_records_use_weighted_resources_and_explicit_writes(self) -> None:
        self.assertEqual(len(self.nodes), len(self.by_name))
        for node in self.nodes:
            self.assertIsInstance(node["resources"], dict)
            self.assertTrue(node["resources"])
            self.assertTrue(all(weight > 0 for weight in node["resources"].values()))
            self.assertEqual(node["run_after"], [])
            self.assertIsInstance(node["writes"], list)
            self.assertIn("outputs", node)
            self.assertIn("cacheable", node)


if __name__ == "__main__":
    unittest.main()

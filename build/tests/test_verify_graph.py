from __future__ import annotations

import unittest
from pathlib import Path

from build import graph_driver, verify_graph


WORKSPACE = Path(__file__).resolve().parents[2]


class VerifyGraphComposerTests(unittest.TestCase):
    def test_analysis_scope_is_schema_v2_and_runnable(self) -> None:
        composition = verify_graph.compose_x64_graph(WORKSPACE, scope="analysis")

        self.assertTrue(composition.runnable)
        self.assertEqual(composition.mapping["targets"], ["analysis-gate"])
        self.assertEqual(composition.mapping["failure_policy"], "continue")
        graph = graph_driver.Graph.from_mapping(
            composition.name, composition.mapping, WORKSPACE
        )
        names = set(graph._order(graph.targets))
        self.assertIn("doctor", names)
        self.assertIn("restore", names)
        self.assertIn("source-checks", names)
        self.assertIn("analysis-gate", names)
        self.assertEqual(len([name for name in names if name.startswith("analyze-msvc-")]), 47)
        self.assertEqual(len([name for name in names if name.startswith("analyze-tidy-")]), 47)

    def test_native_graph_argv_binds_to_public_graph_leaf_entrypoint(self) -> None:
        composition = verify_graph.compose_x64_graph(WORKSPACE, scope="analysis")
        nodes = {node["name"]: node for node in composition.mapping["nodes"]}

        for name, node in nodes.items():
            if name in {"doctor", "restore", "source-checks"}:
                continue
            argv = node["argv"]
            self.assertEqual(argv[:5], ["pwsh", "-NoLogo", "-NoProfile", "-File", "build.ps1"])
            self.assertEqual(argv[5], "graph-leaf")
            self.assertEqual(argv[6], "-GraphLeafAction")

    def test_dynamic_external_dependencies_are_exactly_satisfied_by_native_nodes(self) -> None:
        composition = verify_graph.compose_x64_graph(WORKSPACE, scope="full")

        self.assertFalse(composition.runnable)
        self.assertEqual(composition.unwired_sections, ("dynamic",))
        self.assertEqual(
            composition.external_dependencies,
            (
                "build-fuzz-pickle",
                "build-fuzz-renpy",
                "build-fuzz-rpgmaker",
                "build-fuzz-zanzarah",
                "build-release-leak-probe",
                "build-release-renpy",
                "build-release-rpgmaker",
                "build-release-tests",
                "build-release-zanzarah",
            ),
        )
        names = {node["name"] for node in composition.mapping["nodes"]}
        self.assertTrue(set(composition.external_dependencies).issubset(names))

    def test_full_scope_is_plan_only_and_covers_dynamic_terminal_nodes(self) -> None:
        composition = verify_graph.compose_x64_graph(WORKSPACE, scope="full")

        self.assertFalse(composition.runnable)
        self.assertEqual(
            composition.mapping["targets"],
            [
                "analysis-gate",
                "fuzz-timed-x64-pickle",
                "fuzz-timed-x64-renpy",
                "fuzz-timed-x64-rpgmaker",
                "fuzz-timed-x64-zanzarah",
                "leaks-aggregate",
                "package-evidence",
                "run-tests-asan",
                "run-tests-coverage",
                "run-tests-debug",
                "run-tests-release",
                "run-tests-ubsan",
            ],
        )
        graph = graph_driver.Graph.from_mapping(composition.name, composition.mapping, WORKSPACE)
        graph._validate_write_conflicts(graph._order(graph.targets))

    def test_every_resource_claim_has_declared_capacity(self) -> None:
        composition = verify_graph.compose_x64_graph(WORKSPACE, scope="full")
        capacities = composition.mapping["resources"]

        self.assertEqual(
            capacities,
            {
                "archive-io": 2,
                "binskim": 1,
                "clang-tidy": 4,
                "cpu": 12,
                "dumpbin": 2,
                "fuzz-runtime": 4,
                "fuzz-writer-x64-pickle": 1,
                "fuzz-writer-x64-renpy": 1,
                "fuzz-writer-x64-rpgmaker": 1,
                "fuzz-writer-x64-zanzarah": 1,
                "memory-gib": 16,
                "msvc-analysis": 4,
                "native-msbuild": 2,
                "package-init": 1,
                "runtime-smoke": 2,
                "sarif": 4,
                "test-run": 2,
                "umdh-capture": 1,
                "umdh-diff": 2,
                "umdh-session": 1,
            },
        )
        for node in composition.mapping["nodes"]:
            for resource, demand in node["resources"].items():
                self.assertLessEqual(demand, capacities[resource], (node["name"], resource))

    def test_unknown_scope_is_rejected(self) -> None:
        with self.assertRaisesRegex(verify_graph.VerifyGraphError, "unknown scope"):
            verify_graph.compose_x64_graph(WORKSPACE, scope="not-a-scope")


if __name__ == "__main__":
    unittest.main()

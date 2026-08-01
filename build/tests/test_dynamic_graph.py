from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from pathlib import PurePosixPath
import unittest

from build.graph_driver import Graph
from build.native_graph import (
    FUZZ_PROJECT_NAMES,
    build_project_node_name,
    expand_x64_nodes,
    fuzz_build_node_name,
    load_manifest,
)
from build.dynamic_graph import (
    ARCHITECTURES,
    FUZZ_TARGETS,
    LEAK_MODES,
    LEAK_SCENARIOS,
    MODULES,
    DynamicTopologyError,
    build_dynamic_topology,
    validate_dynamic_topology,
)

WORKSPACE = Path(__file__).resolve().parents[2]


class DynamicGraphTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.topology = build_dynamic_topology(
            architectures=ARCHITECTURES,
            host_architecture="x64",
            leak_windows=3,
            run_id="contract-run",
        )
        validate_dynamic_topology(self.topology)
        self.nodes = {node["name"]: node for node in self.topology["nodes"]}

    def nodes_of_kind(self, kind: str) -> list[dict[str, object]]:
        return [
            node
            for node in self.topology["nodes"]
            if f"kind={kind}" in node["fingerprint"]
        ]

    def test_schema_is_normalized_and_stable(self) -> None:
        self.assertEqual(2, self.topology["schema"])
        self.assertEqual(
            sorted(self.nodes),
            [node["name"] for node in self.topology["nodes"]],
        )
        self.assertEqual(
            json.dumps(self.topology, sort_keys=True, separators=(",", ":")),
            json.dumps(
                build_dynamic_topology(
                    architectures=ARCHITECTURES,
                    host_architecture="x64",
                    leak_windows=3,
                    run_id="contract-run",
                ),
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        for node in self.topology["nodes"]:
            self.assertEqual(
                {
                    "name",
                    "deps",
                    "run_after",
                    "argv",
                    "resources",
                    "inputs",
                    "writes",
                    "outputs",
                    "fingerprint",
                    "cacheable",
                },
                set(node),
            )
            self.assertTrue(node["resources"], node["name"])
            self.assertIsInstance(node["resources"], dict)
            self.assertEqual([], node["run_after"])
            for resource, units in node["resources"].items():
                self.assertTrue(resource)
                self.assertGreater(units, 0)

    def test_every_write_and_output_has_one_owner(self) -> None:
        write_owners: dict[str, str] = {}
        output_owners: dict[str, str] = {}
        for node in self.topology["nodes"]:
            for field, owners in (("writes", write_owners), ("outputs", output_owners)):
                for value in node[field]:
                    self.assertFalse(PurePosixPath(value).is_absolute(), value)
                    self.assertNotIn("..", PurePosixPath(value).parts, value)
                    self.assertNotIn(value, owners, f"{value}: {owners.get(value)} and {node['name']}")
                    owners[value] = node["name"]

    def test_validation_rejects_nested_write_ownership_and_external_collisions(self) -> None:
        nested = deepcopy(self.topology)
        first, second = nested["nodes"][:2]
        second["writes"].append(first["writes"][0] + "/child")
        with self.assertRaisesRegex(DynamicTopologyError, "overlap"):
            validate_dynamic_topology(nested)

        collision = deepcopy(self.topology)
        collision["external_nodes"].append(collision["nodes"][0]["name"])
        with self.assertRaisesRegex(DynamicTopologyError, "external"):
            validate_dynamic_topology(collision)

    def test_fuzz_extends_four_native_build_nodes_with_replay_and_timed_pipelines(self) -> None:
        native_names = {
            node["name"]
            for node in expand_x64_nodes(
                load_manifest(WORKSPACE, project_names=FUZZ_PROJECT_NAMES)
            )
        }
        self.assertEqual(0, len(self.nodes_of_kind("fuzz-build")))
        self.assertEqual(4, len(self.nodes_of_kind("fuzz-seed-replay")))
        self.assertEqual(4, len(self.nodes_of_kind("fuzz-timed")))
        for target in FUZZ_TARGETS:
            build_name = fuzz_build_node_name(target)
            replay = self.nodes[f"fuzz-seed-replay-x64-{target}"]
            timed = self.nodes[f"fuzz-timed-x64-{target}"]
            self.assertIn(build_name, self.topology["external_nodes"])
            self.assertIn(build_name, native_names)
            self.assertEqual([build_name], replay["deps"])
            self.assertEqual([replay["name"]], timed["deps"])
            self.assertFalse(replay["cacheable"])
            self.assertFalse(timed["cacheable"])
            self.assertTrue(all("contract-run" in path for path in replay["outputs"]))
            self.assertTrue(all("contract-run" in path for path in timed["outputs"]))
            self.assertIn("-RunId", replay["argv"])
            self.assertIn("contract-run", replay["argv"])
            self.assertIn("-TargetName", replay["argv"])
            self.assertIn(target, replay["argv"])
            self.assertIn("-Phase", timed["argv"])
            self.assertIn("timed", timed["argv"])
            self.assertEqual(1, timed["resources"][f"fuzz-writer-x64-{target}"])

    def test_leaks_are_fourteen_sessions_with_parallel_offline_analysis(self) -> None:
        self.assertEqual(14, len(self.nodes_of_kind("leak-preflight")))
        self.assertEqual(14, len(self.nodes_of_kind("leak-capture")))
        self.assertEqual(42, len(self.nodes_of_kind("leak-diff")))
        self.assertEqual(14, len(self.nodes_of_kind("leak-judge")))
        for mode in LEAK_MODES:
            for scenario in LEAK_SCENARIOS:
                stem = f"leak-{mode}-{scenario}"
                preflight = self.nodes[f"{stem}-preflight"]
                capture = self.nodes[f"{stem}-capture"]
                judge = self.nodes[f"{stem}-judge"]
                self.assertEqual(["leak-setup"], preflight["deps"])
                self.assertEqual([preflight["name"]], capture["deps"])
                self.assertIn("-Scenario", capture["argv"])
                self.assertIn(scenario, capture["argv"])
                self.assertEqual(1, capture["resources"]["umdh-capture"])
                expected_diffs = sorted(
                    [f"{stem}-diff-window-1", f"{stem}-diff-window-2", f"{stem}-diff-overall"]
                )
                self.assertEqual(expected_diffs, judge["deps"])
        aggregate = self.nodes["leaks-aggregate"]
        self.assertEqual(14, len(aggregate["deps"]))

    def test_audit_is_per_architecture_module_and_tool(self) -> None:
        self.assertEqual(9, len(self.nodes_of_kind("audit-dumpbin")))
        self.assertEqual(9, len(self.nodes_of_kind("audit-binskim")))
        self.assertEqual(9, len(self.nodes_of_kind("audit-module")))
        for architecture in ARCHITECTURES:
            for module in MODULES:
                stem = f"audit-{architecture}-{module}"
                release = (
                    build_project_node_name("Release", module)
                    if architecture == "x64"
                    else f"release-{architecture}-{module}"
                )
                self.assertEqual([release], self.nodes[f"{stem}-dumpbin"]["deps"])
                self.assertEqual([release], self.nodes[f"{stem}-binskim"]["deps"])
                self.assertEqual(
                    sorted([f"{stem}-dumpbin", f"{stem}-binskim"]),
                    self.nodes[stem]["deps"],
                )

    def test_package_has_creation_content_and_runtime_leaf_per_module_archive(self) -> None:
        self.assertEqual(9, len(self.nodes_of_kind("package-create")))
        self.assertEqual(9, len(self.nodes_of_kind("package-content-smoke")))
        self.assertEqual(6, len(self.nodes_of_kind("package-runtime-smoke")))
        self.assertEqual(3, len(self.nodes_of_kind("package-runtime-deferred")))
        self.assertEqual(3, len(self.nodes_of_kind("symbols-create")))
        self.assertEqual(3, len(self.nodes_of_kind("symbols-content-smoke")))
        for architecture in ARCHITECTURES:
            for module in MODULES:
                stem = f"package-{architecture}-{module}"
                create = self.nodes[f"{stem}-create"]
                content = self.nodes[f"{stem}-content"]
                runtime = self.nodes[f"{stem}-runtime"]
                self.assertEqual(
                    sorted(["package-init", f"audit-{architecture}-{module}"]),
                    create["deps"],
                )
                self.assertEqual([create["name"]], content["deps"])
                expected_runtime_kind = (
                    "package-runtime-smoke" if architecture in {"x86", "x64"} else "package-runtime-deferred"
                )
                self.assertIn(f"kind={expected_runtime_kind}", runtime["fingerprint"])
                expected_runtime_deps = [content["name"]]
                if expected_runtime_kind == "package-runtime-smoke":
                    expected_runtime_deps.append(
                        build_project_node_name("Release", "tests")
                        if architecture == "x64"
                        else f"release-{architecture}-tests"
                    )
                self.assertEqual(sorted(expected_runtime_deps), runtime["deps"])
        evidence = self.nodes["package-evidence"]
        self.assertEqual(12, len(evidence["deps"]))

    def test_x64_release_anchors_are_exported_by_the_native_generator(self) -> None:
        native_names = {
            node["name"] for node in expand_x64_nodes(load_manifest(WORKSPACE))
        }
        expected = {
            build_project_node_name("Release", project)
            for project in (*MODULES, "tests", "leak-probe")
        }
        self.assertTrue(expected.issubset(native_names))
        self.assertTrue(expected.issubset(set(self.topology["external_nodes"])))
        self.assertIn(
            build_project_node_name("Release", "leak-probe"),
            self.nodes["leak-setup"]["deps"],
        )

    def test_x64_dynamic_records_compose_with_schema_v2_native_records(self) -> None:
        dynamic = build_dynamic_topology(
            architectures=("x64",),
            host_architecture="x64",
            leak_windows=3,
            run_id="schema-v2-integration",
        )
        native_nodes = expand_x64_nodes(load_manifest(WORKSPACE))
        resource_names = {
            resource
            for node in (*native_nodes, *dynamic["nodes"])
            for resource in node["resources"]
        }
        capacities = {resource: 64 for resource in resource_names}
        source_checks = {
            "name": "source-checks",
            "deps": [],
            "run_after": [],
            "resources": {"cpu": 1},
            "argv": ["pwsh", "-NoProfile", "-Command", "exit 0"],
            "inputs": [],
            "writes": [],
            "outputs": [],
            "fingerprint": ["contract=test-source-checks"],
            "cacheable": False,
        }
        Graph.from_mapping(
            "native-dynamic-integration",
            {
                "resources": capacities,
                "failure_policy": "continue",
                "targets": ["leaks-aggregate", "package-evidence"],
                "nodes": [source_checks, *native_nodes, *dynamic["nodes"]],
            },
            WORKSPACE,
        )


if __name__ == "__main__":
    unittest.main()

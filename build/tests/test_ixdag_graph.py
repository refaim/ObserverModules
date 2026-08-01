from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from build.ixdag.graph import (
    Graph,
    GraphError,
    Node,
    ObserverMatrix,
    Project,
    TranslationUnit,
    build_observer_microdag,
    direct_renderer,
)


class IxDagNodeTests(unittest.TestCase):
    def test_node_is_typed_immutable_and_has_one_named_pool(self) -> None:
        node = Node(
            name="compile-a",
            deps=(),
            pool="msvc",
            argv=("cl", "/c", "src/a.cpp"),
            inputs=("src/a.cpp",),
            outputs=(".artifacts/a.obj",),
        )

        self.assertEqual("msvc", node.pool)
        with self.assertRaises(FrozenInstanceError):
            node.pool = "other"  # type: ignore[misc]
        with self.assertRaisesRegex(GraphError, "glob"):
            Node("bad", (), "cpu", ("tool",), ("src/*.cpp",), ("out",))
        with self.assertRaisesRegex(GraphError, "relative"):
            Node("bad", (), "cpu", ("tool",), ("../src/a.cpp",), ("out",))

    def test_content_uid_changes_with_source_bytes_and_flows_to_consumers(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.txt"
            source.write_text("first", encoding="utf-8")
            graph = Graph(
                nodes=(
                    Node("producer", (), "cpu", ("copy",), ("source.txt",), ("out/a",)),
                    Node("consumer", ("producer",), "cpu", ("use",), ("out/a",), ("out/b",)),
                ),
                targets=("consumer",),
            )

            first = graph.descriptors(root)
            source.write_text("second", encoding="utf-8")
            second = graph.descriptors(root)

            self.assertNotEqual(first["producer"]["uid"], second["producer"]["uid"])
            self.assertNotEqual(first["consumer"]["uid"], second["consumer"]["uid"])
            self.assertEqual("producer", first["consumer"]["deps"][0])

    def test_graph_rejects_hidden_generated_inputs_and_duplicate_outputs(self) -> None:
        with self.assertRaisesRegex(GraphError, "direct dependency"):
            Graph(
                nodes=(
                    Node("a", (), "cpu", ("a",), (), ("out/a",)),
                    Node("b", (), "cpu", ("b",), ("out/a",), ("out/b",)),
                ),
                targets=("b",),
            )
        with self.assertRaisesRegex(GraphError, "output owner"):
            Graph(
                nodes=(
                    Node("a", (), "cpu", ("a",), (), ("out/shared",)),
                    Node("b", (), "cpu", ("b",), (), ("out/shared",)),
                ),
                targets=("b",),
            )


class IxDagObserverMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        paths = (
            "build/shared.props",
            "build/projects/module.vcxproj",
            "build/projects/tests.vcxproj",
            "build/projects/fuzz-pickle.vcxproj",
            "build/projects/leak-probe.vcxproj",
            "src/module/a.cpp",
            "src/module/b.cpp",
            "src/tests/test.cpp",
            "src/fuzz/pickle.cpp",
            "src/leaks/probe.cpp",
            "src/fuzz/corpus/pickle/seed",
        )
        for relative_path in paths:
            path = self.root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative_path, encoding="utf-8")
        self.matrix = ObserverMatrix(
            shared_inputs=("build/shared.props",),
            projects=(
                Project(
                    "module",
                    "build/projects/module.vcxproj",
                    ("Debug", "Release"),
                    (
                        TranslationUnit("module", "src/module/a.cpp", "a"),
                        TranslationUnit("module", "src/module/b.cpp", "b"),
                    ),
                ),
                Project(
                    "tests",
                    "build/projects/tests.vcxproj",
                    ("Debug", "Release"),
                    (TranslationUnit("tests", "src/tests/test.cpp", "test"),),
                ),
                Project(
                    "fuzz-pickle",
                    "build/projects/fuzz-pickle.vcxproj",
                    ("Fuzz",),
                    (TranslationUnit("fuzz-pickle", "src/fuzz/pickle.cpp", "fuzz"),),
                ),
                Project(
                    "leak-probe",
                    "build/projects/leak-probe.vcxproj",
                    ("Release",),
                    (TranslationUnit("leak-probe", "src/leaks/probe.cpp", "probe"),),
                ),
            ),
            test_configurations=("Debug", "Release"),
            fuzz_targets=("pickle",),
            fuzz_seed_inputs={"pickle": ("src/fuzz/corpus/pickle/seed",)},
            leak_modes=("operations", "lifecycle"),
            leak_scenarios=("small-success", "malformed"),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_matrix_expands_project_config_tu_and_scenario_axes(self) -> None:
        graph = build_observer_microdag(self.matrix, renderer=direct_renderer)
        nodes = {node.name: node for node in graph.nodes}

        builds = [name for name in nodes if name.startswith("build-")]
        analyzes = [name for name in nodes if name.startswith("analyze-")]
        normalizes = [name for name in nodes if name.startswith("normalize-")]
        leak_cases = [name for name in nodes if name.startswith("leak-case-")]
        self.assertEqual(6, len(builds))
        self.assertEqual(10, len(analyzes))
        self.assertEqual(10, len(normalizes))
        self.assertEqual(4, len(leak_cases))
        self.assertEqual(
            tuple(sorted(name for name in normalizes if name.startswith("normalize-msvc-"))),
            nodes["merge-msvc-sarif"].deps,
        )
        self.assertEqual(
            ("analysis-gate", "fuzz-gate", "leaks-gate", "run-tests-debug", "run-tests-release"),
            nodes["verify"].deps,
        )

    def test_every_node_has_one_pool_and_exact_owned_inputs_outputs(self) -> None:
        graph = build_observer_microdag(self.matrix, renderer=direct_renderer)
        descriptors = graph.descriptors(self.root)
        output_owners = {
            output: node.name for node in graph.nodes for output in node.outputs
        }

        self.assertEqual(set(descriptors), {node.name for node in graph.nodes})
        for node in graph.nodes:
            self.assertTrue(node.pool)
            self.assertIsInstance(node.pool, str)
            self.assertTrue(node.outputs)
            self.assertFalse(any("*" in path or "?" in path for path in node.inputs))
            for input_path in node.inputs:
                owner = output_owners.get(input_path)
                if owner is not None:
                    self.assertIn(owner, node.deps)

    def test_jinja_template_is_only_a_descriptor_emitter(self) -> None:
        template = (
            Path(__file__).parents[1] / "ixdag" / "templates" / "node.json.j2"
        ).read_text(encoding="utf-8")

        self.assertEqual("{{ descriptor | tojson }}", template.strip())


if __name__ == "__main__":
    unittest.main()

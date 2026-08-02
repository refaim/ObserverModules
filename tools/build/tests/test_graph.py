from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import unittest


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.graph import Command, Graph, GraphError, Node, merge_graphs  # noqa: E402


def node(
    name: str,
    *,
    inputs: tuple[str, ...] = (),
    pool: str = "cpu",
) -> Node:
    return Node(
        name=name,
        uid=hashlib.md5(name.encode(), usedforsecurity=False).hexdigest(),
        pool=pool,
        command=Command(("tool", name)),
        inputs=inputs,
    )


class GraphTests(unittest.TestCase):
    def test_graphs_merge_shared_exact_nodes_pools_and_targets(self) -> None:
        shared = node("shared")
        first = Graph(
            (shared, node("first", inputs=(shared.name,))),
            ("first",),
            {"cpu": 2},
        )
        second = Graph(
            (shared, node("second", inputs=(shared.name,), pool="io")),
            ("shared", "second"),
            {"cpu": 2, "io": 1},
        )

        merged = merge_graphs(first, second)

        self.assertEqual(tuple(current.name for current in merged.nodes), ("shared", "first", "second"))
        self.assertEqual(merged.targets, ("first", "shared", "second"))
        self.assertEqual(dict(merged.pools), {"cpu": 2, "io": 1})

    def test_graph_merge_rejects_missing_inputs_and_conflicting_shared_contracts(self) -> None:
        with self.assertRaisesRegex(GraphError, "at least one graph"):
            merge_graphs()

        first = Graph((node("shared"),), ("shared",), {"cpu": 1})
        conflicting_node = Node("shared", "f" * 32, "cpu", Command(("tool",)))
        second = Graph((conflicting_node,), ("shared",), {"cpu": 1})
        with self.assertRaisesRegex(GraphError, "conflicting node definition.*shared"):
            merge_graphs(first, second)

        third = Graph((node("other"),), ("other",), {"cpu": 2})
        with self.assertRaisesRegex(GraphError, "conflicting pool capacity.*cpu"):
            merge_graphs(first, third)

    def test_edges_and_targets_are_direct_node_names(self) -> None:
        producer = node("producer")
        consumer = node("consumer", inputs=("producer",))
        graph = Graph((consumer, producer), ("consumer",), {"cpu": 2})

        self.assertEqual(consumer.inputs, ("producer",))
        self.assertEqual(graph.node("producer"), producer)
        self.assertEqual(graph.dependencies_of("consumer"), (producer,))

    def test_unknown_dependencies_targets_and_duplicate_names_are_rejected(self) -> None:
        with self.assertRaisesRegex(GraphError, "duplicate node name"):
            Graph((node("same"), node("same")), ("same",), {"cpu": 1})
        with self.assertRaisesRegex(GraphError, "unknown dependency.*missing"):
            Graph(
                (node("consumer", inputs=("missing",)),),
                ("consumer",),
                {"cpu": 1},
            )
        with self.assertRaisesRegex(GraphError, "unknown target.*missing"):
            Graph((node("only"),), ("missing",), {"cpu": 1})

    def test_cycles_are_rejected_before_execution(self) -> None:
        with self.assertRaisesRegex(GraphError, "cycle.*first.*second.*first"):
            Graph(
                (
                    node("second", inputs=("first",)),
                    node("first", inputs=("second",)),
                ),
                ("first",),
                {"cpu": 1},
            )

    def test_each_node_selects_one_known_positive_pool(self) -> None:
        with self.assertRaisesRegex(GraphError, "unknown pool.*missing"):
            Graph((node("only", pool="missing"),), ("only",), {"cpu": 1})
        for invalid in (0, -1, True):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                GraphError, "invalid pool capacity"
            ):
                Graph((node("only"),), ("only",), {"cpu": invalid})  # type: ignore[dict-item]

    def test_node_identity_and_command_are_safe_runtime_descriptors(self) -> None:
        command = Command(
            ("pwsh", "argument with spaces", "&literal", ""),
            env=(("ZED", "last"), ("ALPHA", "first")),
            cwd="work/a directory",
            stdin=b"Write-Output 'literal'\r\n",
        )
        current = Node("safe-name.1", "0" * 32, "cpu", command)

        self.assertEqual(current.command.env, (("ALPHA", "first"), ("ZED", "last")))
        self.assertEqual(current.command.stdin, b"Write-Output 'literal'\r\n")
        self.assertEqual(current.command.argv[-1], "")
        with self.assertRaisesRegex(GraphError, "uid.*lowercase MD5"):
            Node("safe", "NOT-A-UID", "cpu", Command(("tool",)))
        with self.assertRaisesRegex(GraphError, "node name.*128"):
            node("a" * 129)

    def test_ambiguous_command_and_node_data_are_rejected(self) -> None:
        invalid_commands = (
            lambda: Command(()),
            lambda: Command(("bad\0argument",)),
            lambda: Command(("tool",), env=(("PATH", "1"), ("Path", "2"))),
            lambda: Command(("tool",), cwd="bad\0cwd"),
            lambda: Command(("tool",), stdin="not bytes"),  # type: ignore[arg-type]
        )
        for constructor in invalid_commands:
            with self.subTest(constructor=constructor), self.assertRaises(GraphError):
                constructor()

        with self.assertRaisesRegex(GraphError, "duplicate dependencies"):
            Node("safe", "0" * 32, "cpu", Command(("tool",)), ("same", "same"))

    def test_graph_container_and_lookups_are_explicit(self) -> None:
        only = node("only")
        invalid_graphs = (
            lambda: Graph((), ("only",), {"cpu": 1}),
            lambda: Graph((only,), (), {"cpu": 1}),
            lambda: Graph((only,), ("only", "only"), {"cpu": 1}),
            lambda: Graph((object(),), ("only",), {"cpu": 1}),  # type: ignore[arg-type]
            lambda: Graph((only,), ("only",), {"": 1}),
        )
        for constructor in invalid_graphs:
            with self.subTest(constructor=constructor), self.assertRaises(GraphError):
                constructor()

        graph = Graph((only,), ("only",), {"cpu": 1})
        with self.assertRaisesRegex(GraphError, "unknown node"):
            graph.node("missing")
        with self.assertRaisesRegex(GraphError, "unknown node"):
            graph.dependencies_of("missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)

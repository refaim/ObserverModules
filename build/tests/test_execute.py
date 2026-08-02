from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
import sys
import unittest


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.execute import ExecutionError, Executor  # noqa: E402
from core.graph import Command, Graph, GraphError, Node  # noqa: E402


def node(name: str, *, inputs: tuple[str, ...] = (), pool: str = "cpu") -> Node:
    return Node(
        name,
        hashlib.md5(name.encode(), usedforsecurity=False).hexdigest(),
        pool,
        Command(("synthetic", name)),
        inputs,
    )


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_target_subset_returns_none_and_skips_other_targets(self) -> None:
        graph = Graph(
            (node("selected"), node("other")),
            ("selected", "other"),
            {"cpu": 2},
        )
        complete: set[str] = set()
        calls: list[str] = []

        async def runner(current: Node) -> None:
            calls.append(current.name)

        result = await Executor(
            graph,
            is_complete=lambda current: current.name in complete,
            runner=runner,
            publish=lambda current: complete.add(current.name),
        ).run(("selected",))

        self.assertIsNone(result)
        self.assertEqual(calls, ["selected"])

        with self.assertRaisesRegex(GraphError, "target.*empty"):
            await Executor(
                graph,
                is_complete=lambda _current: False,
                runner=runner,
                publish=lambda _current: None,
            ).run(())

    async def test_demand_traversal_deduplicates_a_shared_dependency(self) -> None:
        graph = Graph(
            (
                node("shared"),
                node("left", inputs=("shared",)),
                node("right", inputs=("shared",)),
                node("unreachable"),
            ),
            ("left", "right"),
            {"cpu": 4},
        )
        complete: set[str] = set()
        calls: list[str] = []

        async def runner(current: Node) -> None:
            calls.append(current.name)
            await asyncio.sleep(0.01)

        await Executor(
            graph,
            is_complete=lambda current: current.name in complete,
            runner=runner,
            publish=lambda current: complete.add(current.name),
        ).run()

        self.assertEqual(calls.count("shared"), 1)
        self.assertCountEqual(calls, ["shared", "left", "right"])
        self.assertNotIn("unreachable", calls)

    async def test_named_pool_limits_concurrency(self) -> None:
        graph = Graph(
            (node("one"), node("two"), node("three")),
            ("one", "two", "three"),
            {"cpu": 2},
        )
        complete: set[str] = set()
        active = 0
        maximum = 0

        async def runner(_current: Node) -> None:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.02)
            finally:
                active -= 1

        await Executor(
            graph,
            is_complete=lambda current: current.name in complete,
            runner=runner,
            publish=lambda current: complete.add(current.name),
        ).run()

        self.assertEqual(maximum, 2)

    async def test_shared_slot_pool_limits_total_cross_family_concurrency(self) -> None:
        graph = Graph(
            (node("compile", pool="slot"),) + tuple(
                node(f"audit-{index}", pool="audit") for index in range(4)
            ),
            ("compile",) + tuple(f"audit-{index}" for index in range(4)),
            {"slot": 2, "audit": 4},
        )
        complete: set[str] = set()
        active = maximum = 0

        async def runner(_current: Node) -> None:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.02)
            finally:
                active -= 1

        await Executor(
            graph,
            is_complete=lambda current: current.name in complete,
            runner=runner,
            publish=lambda current: complete.add(current.name),
        ).run()

        self.assertEqual(maximum, 2)

    async def test_warm_target_cache_skips_dependencies(self) -> None:
        graph = Graph(
            (node("dependency"), node("target", inputs=("dependency",))),
            ("target",),
            {"cpu": 2},
        )

        async def runner(_current: Node) -> None:
            raise AssertionError("warm target and its dependencies must not run")

        await Executor(
            graph,
            is_complete=lambda current: current.name == "target",
            runner=runner,
            publish=lambda _current: None,
        ).run()

    async def test_external_lock_rechecks_cache_before_dependencies(self) -> None:
        graph = Graph(
            (node("dependency"), node("target", inputs=("dependency",))),
            ("target",),
            {"cpu": 1},
        )
        complete = False
        events: list[str] = []

        @asynccontextmanager
        async def lock(current: Node):
            nonlocal complete
            events.append(f"enter:{current.name}")
            complete = current.name == "target"
            try:
                yield
            finally:
                events.append(f"leave:{current.name}")

        async def runner(_current: Node) -> None:
            raise AssertionError("cache was completed while waiting for the lock")

        await Executor(
            graph,
            is_complete=lambda current: complete and current.name == "target",
            runner=runner,
            publish=lambda _current: None,
            acquire_lock=lock,
        ).run()

        self.assertEqual(events, ["enter:target", "leave:target"])

    async def test_runner_must_publish_completion(self) -> None:
        graph = Graph((node("broken"),), ("broken",), {"cpu": 1})

        async def runner(_current: Node) -> None:
            pass

        with self.assertRaises(ExceptionGroup) as raised:
            await Executor(
                graph,
                is_complete=lambda _current: False,
                runner=runner,
                publish=lambda _current: None,
            ).run()
        self.assertIn("broken", repr(raised.exception.subgroup(ExecutionError)))

    async def test_task_group_cancels_running_siblings_on_first_failure(self) -> None:
        graph = Graph(
            (node("failure"), node("slow")),
            ("failure", "slow"),
            {"cpu": 2},
        )
        slow_started = asyncio.Event()
        slow_cancelled = asyncio.Event()

        async def runner(current: Node) -> None:
            if current.name == "slow":
                slow_started.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    slow_cancelled.set()
                    raise
            await slow_started.wait()
            raise RuntimeError("expected failure")

        with self.assertRaises(ExceptionGroup) as raised:
            await Executor(
                graph,
                is_complete=lambda _current: False,
                runner=runner,
                publish=lambda _current: None,
            ).run()

        self.assertIn("expected failure", repr(raised.exception.subgroup(RuntimeError)))
        self.assertTrue(slow_cancelled.is_set())

    async def test_completion_predicate_is_synchronous_and_returns_bool(self) -> None:
        graph = Graph((node("invalid"),), ("invalid",), {"cpu": 1})

        async def runner(_current: Node) -> None:
            pass

        with self.assertRaises(ExceptionGroup) as raised:
            await Executor(
                graph,
                is_complete=lambda _current: "yes",  # type: ignore[arg-type,return-value]
                runner=runner,
                publish=lambda _current: None,
            ).run()
        self.assertIn("did not return bool", repr(raised.exception.subgroup(ExecutionError)))


if __name__ == "__main__":
    unittest.main(verbosity=2)

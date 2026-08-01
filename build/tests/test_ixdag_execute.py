from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from build.ixdag.execute import (
    Command,
    Executor,
    Graph,
    GraphError,
    Node,
    NodeExecutionError,
    ProcessRequest,
    run_process,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_ROOT = REPOSITORY_ROOT / ".artifacts"
ARTIFACTS_ROOT.mkdir(exist_ok=True)


def object_id(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def node(
    name: str,
    *,
    deps: tuple[str, ...] = (),
    pool: str = "cpu",
    commands: tuple[Command, ...] | None = None,
) -> Node:
    return Node(
        name=name,
        object_id=object_id(name),
        deps=deps,
        pool=pool,
        commands=commands or (Command(("synthetic", name)),),
    )


class RecordingRunner:
    def __init__(self, *, fail: str | None = None, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.calls: list[ProcessRequest] = []
        self.active: dict[str, int] = {}
        self.maximum: dict[str, int] = {}

    async def __call__(self, request: ProcessRequest) -> int:
        self.calls.append(request)
        pool = request.pool
        self.active[pool] = self.active.get(pool, 0) + 1
        self.maximum[pool] = max(self.maximum.get(pool, 0), self.active[pool])
        try:
            out_dir = Path(request.env["IX_OUT"])
            (out_dir / "payload.txt").write_text(request.node_name, encoding="utf-8")
            if self.delay:
                await asyncio.sleep(self.delay)
            return 19 if request.node_name == self.fail else 0
        finally:
            self.active[pool] -= 1


class IxExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            dir=ARTIFACTS_ROOT,
            prefix="ixdag-tests-",
        )
        self.workspace = Path(self.temporary_directory.name)
        self.state = self.workspace / "state"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def graph(
        self,
        nodes: tuple[Node, ...],
        *,
        targets: tuple[str, ...],
        pools: dict[str, int] | None = None,
    ) -> Graph:
        return Graph(nodes=nodes, targets=targets, pools=pools or {"cpu": 4})

    async def test_demand_visit_deduplicates_a_shared_dependency(self) -> None:
        graph = self.graph(
            (
                node("shared"),
                node("left", deps=("shared",)),
                node("right", deps=("shared",)),
                node("unreachable"),
            ),
            targets=("left", "right"),
        )
        runner = RecordingRunner(delay=0.01)

        results = await Executor(graph, self.workspace, self.state, runner=runner).run()

        names = [request.node_name for request in runner.calls]
        self.assertEqual(names.count("shared"), 1)
        self.assertCountEqual(names, ["shared", "left", "right"])
        self.assertNotIn("unreachable", results)

    async def test_named_pool_semaphore_limits_concurrency(self) -> None:
        graph = self.graph(
            (node("first", pool="serial"), node("second", pool="serial")),
            targets=("first", "second"),
            pools={"serial": 1},
        )
        runner = RecordingRunner(delay=0.03)

        await Executor(graph, self.workspace, self.state, runner=runner).run()

        self.assertEqual(runner.maximum["serial"], 1)
        self.assertEqual(runner.calls[0].env["IX_POOL_CAPACITY"], "1")

    async def test_complete_target_is_cached_without_visiting_its_dependencies(self) -> None:
        graph = self.graph(
            (node("dependency"), node("target", deps=("dependency",))),
            targets=("target",),
        )
        first_runner = RecordingRunner()
        executor = Executor(graph, self.workspace, self.state, runner=first_runner)
        await executor.run()
        dependency_dir = executor.output_dir("dependency")
        dependency_marker = dependency_dir / ".complete"
        dependency_marker.unlink()

        warm_runner = RecordingRunner()
        results = await Executor(graph, self.workspace, self.state, runner=warm_runner).run()

        self.assertEqual(results["target"].status, "cached")
        self.assertEqual(warm_runner.calls, [])
        self.assertFalse(dependency_marker.exists())

    async def test_partial_output_is_moved_to_trash_before_rerun(self) -> None:
        current = node("recover")
        graph = self.graph((current,), targets=(current.name,))
        executor = Executor(graph, self.workspace, self.state, runner=RecordingRunner())
        out_dir = executor.output_dir(current.name)
        out_dir.mkdir(parents=True)
        (out_dir / "stale.txt").write_text("stale", encoding="utf-8")

        await executor.run()

        self.assertFalse((out_dir / "stale.txt").exists())
        trashed = list((self.state / "trash").glob("*"))
        self.assertEqual(len(trashed), 1)
        self.assertEqual((trashed[0] / "stale.txt").read_text(encoding="utf-8"), "stale")
        self.assertTrue((out_dir / ".complete").is_file())

    async def test_failed_node_is_trashed_and_does_not_publish_completion(self) -> None:
        graph = self.graph(
            (node("failure"), node("dependent", deps=("failure",))),
            targets=("dependent",),
        )
        runner = RecordingRunner(fail="failure")
        executor = Executor(graph, self.workspace, self.state, runner=runner)

        with self.assertRaisesRegex(NodeExecutionError, "failure.*exit code 19"):
            await executor.run()

        self.assertEqual([request.node_name for request in runner.calls], ["failure"])
        self.assertFalse(executor.output_dir("failure").exists())
        trashed_payloads = list((self.state / "trash").glob("*/payload.txt"))
        self.assertEqual(len(trashed_payloads), 1)
        self.assertEqual(trashed_payloads[0].read_text(encoding="utf-8"), "failure")

    async def test_failure_cancels_and_trashes_an_already_running_sibling(self) -> None:
        graph = self.graph(
            (node("failure"), node("slow")),
            targets=("failure", "slow"),
        )
        slow_started = asyncio.Event()

        async def runner(request: ProcessRequest) -> int:
            out_dir = Path(request.env["IX_OUT"])
            (out_dir / "payload.txt").write_text(request.node_name, encoding="utf-8")
            if request.node_name == "slow":
                slow_started.set()
                await asyncio.sleep(60)
                return 0
            await slow_started.wait()
            return 31

        executor = Executor(graph, self.workspace, self.state, runner=runner)

        with self.assertRaisesRegex(NodeExecutionError, "failure.*exit code 31"):
            await executor.run()

        self.assertFalse(executor.output_dir("failure").exists())
        self.assertFalse(executor.output_dir("slow").exists())
        trashed = list((self.state / "trash").glob("*/payload.txt"))
        self.assertCountEqual(
            [path.read_text(encoding="utf-8") for path in trashed],
            ["failure", "slow"],
        )

    async def test_command_cwd_cannot_escape_the_workspace(self) -> None:
        unsafe = node("unsafe", commands=(Command(("tool",), cwd=".."),))
        graph = self.graph((unsafe,), targets=(unsafe.name,))
        runner = RecordingRunner()

        with self.assertRaisesRegex(GraphError, "cwd must stay within workspace"):
            await Executor(graph, self.workspace, self.state, runner=runner).run()

        self.assertEqual(runner.calls, [])

    async def test_default_process_runner_passes_literal_argv_without_a_shell(self) -> None:
        process = mock.AsyncMock()
        process.wait.return_value = 0
        request = ProcessRequest(
            node_name="literal",
            pool="cpu",
            argv=("program", "argument with spaces", "&not-a-command"),
            cwd=self.workspace,
            env=dict(os.environ),
            log_path=self.workspace / "literal.log",
        )

        with mock.patch.object(
            asyncio,
            "create_subprocess_exec",
            return_value=process,
        ) as create:
            return_code = await run_process(request)

        self.assertEqual(return_code, 0)
        positional, keyword = create.call_args
        self.assertEqual(positional, request.argv)
        self.assertNotIn("shell", keyword)
        self.assertEqual(keyword["cwd"], str(self.workspace))

    async def test_default_runner_merges_stdout_and_stderr_into_the_node_log(self) -> None:
        logged = node(
            "logged",
            commands=(
                Command(
                    (
                        sys.executable,
                        "-c",
                        "import sys; print('stdout-line'); print('stderr-line', file=sys.stderr)",
                    )
                ),
            ),
        )
        graph = self.graph((logged,), targets=(logged.name,))

        results = await Executor(graph, self.workspace, self.state).run()

        text = results["logged"].log_path.read_text(encoding="utf-8")
        self.assertIn("stdout-line", text)
        self.assertIn("stderr-line", text)

    async def test_cycle_is_rejected_instead_of_deadlocking_on_a_node_lock(self) -> None:
        graph = self.graph(
            (node("a", deps=("b",)), node("b", deps=("a",))),
            targets=("a",),
        )

        with self.assertRaisesRegex(GraphError, "a -> b -> a"):
            Executor(graph, self.workspace, self.state)


if __name__ == "__main__":
    unittest.main(verbosity=2)

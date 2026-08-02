from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from filelock import FileLock, Timeout


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.clean import CleanError, clean  # noqa: E402
from core.graph import Command, Graph, Node  # noqa: E402
from core.runtime import BuildRuntime, ProcessFailed  # noqa: E402


RUN_ID = "20260801-runtime"


def node(
    name: str = "analyze-renpy.pickle",
    *,
    env: tuple[tuple[str, str], ...] = (),
    inputs: tuple[str, ...] = (),
) -> Node:
    return Node(
        name=name,
        uid=hashlib.md5(name.encode(), usedforsecurity=False).hexdigest(),
        pool="cpu",
        command=Command(
            (r"C:\tools\analyze.exe", "literal argument"),
            env=env,
            cwd=r"C:\repo\source",
            stdin=b"exact recipe\r\n",
        ),
        inputs=inputs,
    )


class FakeRunner:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls: list[tuple[Command, Path]] = []

    async def run(self, command: Command, *, log) -> int:
        self.calls.append((command, Path(log.name)))
        log.write(b"runner log")
        log.flush()
        return self.exit_code


class BuildRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_lock_uses_persistent_native_async_filelock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            runtime = BuildRuntime(repository, RUN_ID, process_runner=FakeRunner())
            current = node()

            with mock.patch("core.runtime.AsyncFileLock") as factory:
                lock = runtime.lock(current)

            self.assertIs(lock, factory.return_value)
            factory.assert_called_once_with(
                runtime.paths.lock(current.uid),
                timeout=-1,
                poll_interval=0.05,
                fallback_to_soft=False,
                preserve_lock_file=True,
            )

    async def test_success_removes_only_exact_scratch_and_empty_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            runner = FakeRunner()
            runtime = BuildRuntime(repository, RUN_ID, process_runner=runner)
            runtime.paths.prepare()
            current = node(env=(("Alpha", "one"),))

            work = runtime.paths.run_work(RUN_ID) / current.uid
            with (
                mock.patch("core.runtime.shutil.rmtree", wraps=shutil.rmtree) as remove,
                mock.patch("asyncio.to_thread", wraps=asyncio.to_thread) as offload,
            ):
                await runtime.run(current)

            command, log_path = runner.calls[0]
            cas = runtime.store.paths_for(current)
            self.assertEqual(command.argv, current.command.argv)
            self.assertEqual(command.cwd, current.command.cwd)
            self.assertEqual(command.stdin, current.command.stdin)
            self.assertEqual(
                dict(command.env),
                {
                    "Alpha": "one",
                    "OBSERVER_BUILD_DIR": str(work),
                    "OBSERVER_OUT_DIR": str(cas.output),
                    "_MSPDBSRV_ENDPOINT_": f"observer_{current.uid}",
                },
            )
            remove.assert_called_once_with(work)
            offload.assert_awaited_once_with(remove, work)
            self.assertFalse(work.exists())
            self.assertFalse(work.parent.exists())
            self.assertEqual(log_path, cas.log)
            self.assertEqual(cas.log.read_bytes(), b"runner log")
            self.assertFalse(cas.touch.exists())

    async def test_long_node_name_does_not_enter_mutable_scratch_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            runner = FakeRunner()
            runtime = BuildRuntime(repository, RUN_ID, process_runner=runner)
            runtime.paths.prepare()
            current = node("restore-vcpkg-asan-x86-" + "dependency" * 10)

            await runtime.run(current)

            command, _log_path = runner.calls[0]
            work = Path(dict(command.env)["OBSERVER_BUILD_DIR"])
            self.assertEqual(work, runtime.paths.run_work(RUN_ID) / current.uid)
            self.assertNotIn(current.name, str(work))

    async def test_runtime_injects_unique_mspdbsrv_endpoint_per_uid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            runner = FakeRunner()
            runtime = BuildRuntime(repository, RUN_ID, process_runner=runner)
            runtime.paths.prepare()
            nodes = (node("build-renpy-x86-debug"), node("build-renpy-x64-debug"))

            for current in nodes:
                await runtime.run(current)

            endpoints = tuple(
                dict(command.env)["_MSPDBSRV_ENDPOINT_"] for command, _log in runner.calls
            )
            self.assertEqual(endpoints, tuple(f"observer_{current.uid}" for current in nodes))
            self.assertEqual(len(set(endpoints)), len(nodes))

    async def test_run_rejects_case_insensitive_runtime_environment_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            runner = FakeRunner()
            runtime = BuildRuntime(repository, RUN_ID, process_runner=runner)
            runtime.paths.prepare()

            with self.assertRaisesRegex(ValueError, "OBSERVER_OUT_DIR"):
                await runtime.run(node(env=(("observer_out_dir", "hostile"),)))

            self.assertEqual(runner.calls, [])

            with self.assertRaisesRegex(ValueError, "_MSPDBSRV_ENDPOINT_"):
                await runtime.run(node(env=(("_mspdbsrv_endpoint_", "shared"),)))

            self.assertEqual(runner.calls, [])

    async def test_executor_runs_and_publishes_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            runner = FakeRunner()
            runtime = BuildRuntime(repository, RUN_ID, process_runner=runner)
            current = node()
            graph = Graph((current,), (current.name,), {"cpu": 1})

            self.assertFalse(runtime.paths.output_root.exists())
            await runtime.executor(graph).run()

            self.assertEqual(len(runner.calls), 1)
            self.assertTrue(runtime.is_complete(current))
            self.assertEqual(runtime.store.paths_for(current).touch.stat().st_size, 0)

    async def test_cache_report_distinguishes_executed_and_restored_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            current = node()
            graph = Graph((current,), (current.name,), {"cpu": 1})

            cold_runner = FakeRunner()
            cold = BuildRuntime(repository, "cold-run", process_runner=cold_runner)
            await cold.executor(graph).run()
            cold_report = cold.cache_report()

            warm_runner = FakeRunner()
            warm = BuildRuntime(repository, "warm-run", process_runner=warm_runner)
            await warm.executor(graph).run()
            warm_report = warm.cache_report()

            self.assertEqual(cold_report["schema"], 1)
            self.assertEqual(
                cold_report["summary"],
                {"executed": 1, "failed": 0, "hit": 0, "incomplete": 0},
            )
            self.assertEqual(cold_report["nodes"][0]["name"], current.name)
            self.assertEqual(cold_report["nodes"][0]["uid"], current.uid)
            self.assertEqual(cold_report["nodes"][0]["state"], "executed")
            self.assertGreaterEqual(cold_report["nodes"][0]["duration_ms"], 0)
            self.assertEqual(cold.live_uids(), (current.uid,))

            self.assertEqual(
                warm_report["summary"],
                {"executed": 0, "failed": 0, "hit": 1, "incomplete": 0},
            )
            self.assertEqual(warm_report["nodes"], [
                {
                    "duration_ms": 0,
                    "name": current.name,
                    "state": "hit",
                    "uid": current.uid,
                }
            ])
            self.assertEqual(warm.live_uids(), (current.uid,))
            self.assertEqual(len(cold_runner.calls), 1)
            self.assertEqual(warm_runner.calls, [])

    async def test_warm_cached_target_retains_and_reports_its_complete_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            dependency = node("dependency")
            target = node("target", inputs=(dependency.name,))
            graph = Graph((dependency, target), (target.name,), {"cpu": 1})

            cold = BuildRuntime(repository, "cold-run", process_runner=FakeRunner())
            await cold.executor(graph).run()
            warm = BuildRuntime(repository, "warm-run", process_runner=FakeRunner())
            await warm.executor(graph).run()

            self.assertEqual(
                warm.live_uids(graph), tuple(sorted((dependency.uid, target.uid)))
            )
            self.assertEqual(
                warm.cache_report(graph)["summary"],
                {"executed": 0, "failed": 0, "hit": 2, "incomplete": 0},
            )

    async def test_cache_report_excludes_failed_nodes_from_live_uids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            current = node()
            graph = Graph((current,), (current.name,), {"cpu": 1})
            runtime = BuildRuntime(
                repository, "failed-run", process_runner=FakeRunner(23)
            )

            with self.assertRaises(ExceptionGroup):
                await runtime.executor(graph).run()

            self.assertEqual(runtime.cache_report()["summary"], {
                "executed": 0,
                "failed": 1,
                "hit": 0,
                "incomplete": 0,
            })
            self.assertEqual(runtime.cache_report()["nodes"][0]["state"], "failed")
            self.assertEqual(runtime.live_uids(), ())

    async def test_cache_report_marks_observed_unpublished_nodes_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            runtime = BuildRuntime(repository, "pending-run", process_runner=FakeRunner())
            current = node()

            self.assertFalse(runtime.is_complete(current))

            self.assertEqual(runtime.cache_report()["summary"], {
                "executed": 0,
                "failed": 0,
                "hit": 0,
                "incomplete": 1,
            })
            self.assertEqual(runtime.cache_report()["nodes"][0]["state"], "incomplete")
            self.assertEqual(runtime.live_uids(), ())

    async def test_executor_holds_per_run_lease_without_serializing_distinct_runs(self) -> None:
        class ConcurrentRunner:
            entered = 0
            both = asyncio.Event()
            release = asyncio.Event()

            async def run(self, _command: Command, *, log) -> int:
                type(self).entered += 1
                if type(self).entered == 2:
                    type(self).both.set()
                await type(self).release.wait()
                return 0

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            first = BuildRuntime(repository, "run-one", process_runner=ConcurrentRunner())
            second = BuildRuntime(repository, "run-two", process_runner=ConcurrentRunner())
            graphs = tuple(
                Graph((current,), (current.name,), {"cpu": 1})
                for current in (node("first"), node("second"))
            )
            tasks = tuple(
                asyncio.create_task(runtime.executor(graph).run())
                for runtime, graph in zip((first, second), graphs, strict=True)
            )
            await asyncio.wait_for(ConcurrentRunner.both.wait(), timeout=2)

            for run_id in ("run-one", "run-two"):
                with self.assertRaises(Timeout), FileLock(
                    first.paths.lease(run_id), timeout=0, fallback_to_soft=False,
                    preserve_lock_file=True,
                ):
                    pass
            with FileLock(first.paths.coordination_lock(), timeout=0, fallback_to_soft=False,
                          preserve_lock_file=True):
                pass

            ConcurrentRunner.release.set()
            await asyncio.gather(*tasks)
            for run_id in ("run-one", "run-two"):
                with FileLock(first.paths.lease(run_id), timeout=0, fallback_to_soft=False,
                              preserve_lock_file=True):
                    pass

    async def test_session_reuses_one_lease_across_executors_and_blocks_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            runtime = BuildRuntime(repository, RUN_ID, process_runner=FakeRunner())
            graphs = tuple(
                Graph((current,), (current.name,), {"cpu": 1})
                for current in (node("discovery"), node("final"))
            )

            async with runtime.session():
                self.assertEqual(runtime.owned_run_id, RUN_ID)
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    async with runtime.session():
                        pass
                await runtime.executor(graphs[0]).run()
                with self.assertRaises(CleanError):
                    await asyncio.to_thread(clean, repository)
                await runtime.executor(graphs[1]).run()
                with self.assertRaises(Timeout), FileLock(
                    runtime.paths.lease(RUN_ID), timeout=0, fallback_to_soft=False,
                    preserve_lock_file=True,
                ):
                    pass

            self.assertIsNone(runtime.owned_run_id)
            with FileLock(
                runtime.paths.lease(RUN_ID), timeout=0, fallback_to_soft=False,
                preserve_lock_file=True,
            ):
                pass

    async def test_nonzero_exit_leaves_entry_incomplete_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            runtime = BuildRuntime(repository, RUN_ID, process_runner=FakeRunner(23))
            runtime.paths.prepare()
            current = node()

            with self.assertRaisesRegex(ProcessFailed, "23.*analyze-renpy.pickle"):
                await runtime.run(current)

            cas = runtime.store.paths_for(current)
            work = runtime.paths.run_work(RUN_ID) / current.uid
            self.assertTrue(cas.entry.is_dir())
            self.assertTrue(work.is_dir())
            self.assertFalse(cas.touch.exists())
            self.assertFalse(runtime.is_complete(current))

    async def test_cancellation_preserves_scratch(self) -> None:
        class CancelledRunner:
            async def run(self, _command: Command, *, log) -> int:
                raise asyncio.CancelledError

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            runtime = BuildRuntime(repository, RUN_ID, process_runner=CancelledRunner())
            runtime.paths.prepare()
            current = node()
            work = runtime.paths.run_work(RUN_ID) / current.uid

            with self.assertRaises(asyncio.CancelledError):
                await runtime.run(current)

            self.assertTrue(work.is_dir())

    async def test_reparse_scratch_is_rejected_instead_of_removed(self) -> None:
        reparse = False

        class ReplacingRunner:
            async def run(self, _command: Command, *, log) -> int:
                nonlocal reparse
                reparse = True
                return 0

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            runtime = BuildRuntime(repository, RUN_ID, process_runner=ReplacingRunner())
            runtime.paths.prepare()
            current = node()
            work = runtime.paths.run_work(RUN_ID) / current.uid

            with (
                mock.patch("core.paths._is_reparse", side_effect=lambda path: reparse and path == work),
                mock.patch("core.runtime.shutil.rmtree") as remove,
                self.assertRaisesRegex(ValueError, "reparse point"),
            ):
                await runtime.run(current)

            remove.assert_not_called()
            self.assertTrue(work.is_dir())

    async def test_quarantine_survives_successful_scratch_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            runtime = BuildRuntime(repository, RUN_ID, process_runner=FakeRunner())
            runtime.paths.prepare()
            current = node()
            old = runtime.store.paths_for(current)
            old.output.mkdir(parents=True)
            (old.output / "partial.obj").write_bytes(b"partial")

            await runtime.run(current)

            quarantine = runtime.paths.run_work(RUN_ID) / "quarantine" / old.entry.name
            self.assertEqual((quarantine / "out/partial.obj").read_bytes(), b"partial")
            self.assertFalse((runtime.paths.run_work(RUN_ID) / current.uid).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

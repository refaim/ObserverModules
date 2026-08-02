"""Minimal bridge from graph execution to the repository-local Windows CAS."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
import shutil

from filelock import AsyncFileLock

from core.execute import Executor
from core.graph import Command, Graph, Node
from core.paths import BuildPaths
from core.store import CasStore
from core.windows_process import WindowsProcessRunner


class ProcessFailed(RuntimeError):
    """A build node process returned a nonzero exit code."""


class _LeasedExecutor:
    def __init__(self, runtime: BuildRuntime, executor: Executor) -> None:
        self._runtime = runtime
        self._executor = executor

    async def run(self) -> None:
        coordination = self._runtime._lock(self._runtime.paths.coordination_lock())
        lease = self._runtime._lock(self._runtime.paths.lease(self._runtime._run_id))
        async with coordination:
            self._runtime.paths.prepare()
            await lease.acquire()
        try:
            await self._executor.run()
        finally:
            await lease.release()


class BuildRuntime:
    """Wire the generic executor to locks, CAS paths, and process execution."""

    def __init__(
        self,
        repository: Path | str,
        run_id: str,
        process_runner: WindowsProcessRunner = WindowsProcessRunner(),
    ) -> None:
        self.paths = BuildPaths(repository)
        self.store = CasStore(self.paths, run_id)
        self._run_id = run_id
        self._runner = process_runner

    def is_complete(self, current: Node) -> bool:
        return self.store.is_complete(current)

    @staticmethod
    def _lock(path: Path) -> AsyncFileLock:
        return AsyncFileLock(
            path,
            timeout=-1,
            poll_interval=0.05,
            fallback_to_soft=False,
            preserve_lock_file=True,
        )

    def lock(self, current: Node) -> AsyncFileLock:
        return self._lock(self.paths.lock(current.uid))

    async def run(self, current: Node) -> None:
        reserved = ("OBSERVER_OUT_DIR", "OBSERVER_BUILD_DIR")
        existing = {key.casefold() for key, _value in current.command.env}
        for key in reserved:
            if key.casefold() in existing:
                raise ValueError(f"command environment conflicts with {key}")

        cas = self.store.prepare_entry(current)
        run_root = self.paths.run_work(self._run_id)
        work = self.paths.require_confined(
            run_root / f"{current.uid}-{current.name}", self.paths.work_root
        )
        work.mkdir(exist_ok=False)
        command = Command(
            current.command.argv,
            env=current.command.env
            + (
                ("OBSERVER_OUT_DIR", str(cas.output)),
                ("OBSERVER_BUILD_DIR", str(work)),
            ),
            cwd=current.command.cwd,
            stdin=current.command.stdin,
        )
        with cas.log.open("r+b") as log:
            exit_code = await self._runner.run(command, log=log)
        if exit_code != 0:
            raise ProcessFailed(f"process exited {exit_code} for node {current.name}")
        scratch = self.paths.require_confined(work, self.paths.work_root)
        await asyncio.to_thread(shutil.rmtree, scratch)
        with suppress(OSError):
            run_root.rmdir()

    def publish(self, current: Node) -> None:
        self.store.mark_complete(current)

    def executor(self, graph: Graph) -> _LeasedExecutor:
        return _LeasedExecutor(
            self,
            Executor(
                graph,
                is_complete=self.is_complete,
                acquire_lock=self.lock,
                runner=self.run,
                publish=self.publish,
            ),
        )

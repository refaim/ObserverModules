"""Minimal bridge from graph execution to the repository-local Windows CAS."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
import shutil
import time

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
        if self._runtime.owned_run_id is not None:
            await self._executor.run()
            return
        async with self._runtime.session():
            await self._executor.run()


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
        self._observed: dict[str, Node] = {}
        self._durations_ms: dict[str, int] = {}
        self._session_lease: AsyncFileLock | None = None

    @property
    def owned_run_id(self) -> str | None:
        return self._run_id if self._session_lease is not None else None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        """Hold this runtime's run lease across a complete public operation."""

        if self._session_lease is not None:
            raise RuntimeError("build runtime session is already active")
        coordination = self._lock(self.paths.coordination_lock())
        lease = self._lock(self.paths.lease(self._run_id))
        async with coordination:
            self.paths.prepare()
            await lease.acquire()
        self._session_lease = lease
        try:
            yield
        finally:
            self._session_lease = None
            await lease.release()

    def is_complete(self, current: Node) -> bool:
        complete = self.store.is_complete(current)
        self._observed.setdefault(current.uid, current)
        return complete

    def _known_nodes(self, graph: Graph | None) -> dict[str, Node]:
        known = dict(self._observed)
        if graph is not None:
            for current in graph.nodes:
                known.setdefault(current.uid, current)
        return known

    def live_uids(self, graph: Graph | None = None) -> tuple[str, ...]:
        """Return known graph nodes that have a published completion marker."""

        return tuple(sorted(
            uid for uid, current in self._known_nodes(graph).items()
            if self.store.is_complete(current)
        ))

    def cache_report(self, graph: Graph | None = None) -> dict[str, object]:
        """Describe deterministic node identities and their result in this process."""

        nodes: list[dict[str, object]] = []
        summary = {"executed": 0, "failed": 0, "hit": 0, "incomplete": 0}
        for uid, current in sorted(
            self._known_nodes(graph).items(), key=lambda item: (item[1].name, item[0])
        ):
            complete = self.store.is_complete(current)
            if uid in self._durations_ms:
                state = "executed" if complete else "failed"
            elif complete:
                state = "hit"
            else:
                state = "incomplete"
            summary[state] += 1
            nodes.append({
                "duration_ms": self._durations_ms.get(uid, 0),
                "name": current.name,
                "state": state,
                "uid": uid,
            })
        return {"schema": 1, "summary": summary, "nodes": nodes}

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
        self._observed.setdefault(current.uid, current)
        started = time.perf_counter()
        try:
            await self._run_node(current)
        finally:
            self._durations_ms[current.uid] = max(
                0, round((time.perf_counter() - started) * 1000)
            )

    async def _run_node(self, current: Node) -> None:
        reserved = ("OBSERVER_OUT_DIR", "OBSERVER_BUILD_DIR", "_MSPDBSRV_ENDPOINT_")
        existing = {key.casefold() for key, _value in current.command.env}
        for key in reserved:
            if key.casefold() in existing:
                raise ValueError(f"command environment conflicts with {key}")

        cas = self.store.prepare_entry(current)
        run_root = self.paths.run_work(self._run_id)
        work = self.paths.require_confined(run_root / current.uid, self.paths.work_root)
        work.mkdir(exist_ok=False)
        command = Command(
            current.command.argv,
            env=current.command.env
            + (
                ("OBSERVER_OUT_DIR", str(cas.output)),
                ("OBSERVER_BUILD_DIR", str(work)),
                ("_MSPDBSRV_ENDPOINT_", f"observer_{current.uid}"),
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

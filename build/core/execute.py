"""Demand execution adapted from pg83/ix (MIT) at commit 66726a9."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import AsyncContextManager

from core.graph import Graph, GraphError, Node


class ExecutionError(RuntimeError):
    """A node did not publish a complete cache entry."""


CompletionPredicate = Callable[[Node], bool]
Runner = Callable[[Node], Awaitable[None]]
Publisher = Callable[[Node], None]
LockFactory = Callable[[Node], AsyncContextManager[object]]


@asynccontextmanager
async def _unlocked(_node: Node):
    yield None


class Executor:
    """Execute demanded ancestors once under named and shared-slot limits."""

    def __init__(
        self,
        graph: Graph,
        *,
        is_complete: CompletionPredicate,
        runner: Runner,
        publish: Publisher,
        acquire_lock: LockFactory = _unlocked,
    ) -> None:
        self._graph = graph
        self._is_complete_hook = is_complete
        self._runner = runner
        self._publish = publish
        self._acquire_lock = acquire_lock
        self._visited: set[str] = set()
        self._failed: dict[str, Exception] = {}
        self._blocked: set[str] = set()
        self._locks = {node.name: asyncio.Lock() for node in graph.nodes}
        self._pools = {
            name: asyncio.Semaphore(capacity) for name, capacity in graph.pools.items()
        }
        self._global = self._pools.get("slot")
        self._used = False

    async def run(self, targets: tuple[str, ...] | None = None) -> None:
        if self._used:
            raise ExecutionError("an Executor is single-use")
        self._used = True
        requested = self._graph.targets if targets is None else tuple(targets)
        if not requested:
            raise GraphError("explicit target set must not be empty")
        await self._visit_many(tuple(self._graph.node(name) for name in requested))
        if self._failed:
            failures = tuple(sorted(self._failed.items()))
            names = ", ".join(name for name, _error in failures)
            group = ExceptionGroup(
                f"{len(failures)} node(s) failed: {names}",
                tuple(error for _name, error in failures),
            )
            group.failed_nodes = tuple(name for name, _error in failures)  # type: ignore[attr-defined]
            raise group

    async def _visit(self, current: Node) -> None:
        async with self._locks[current.name]:
            if (
                current.name in self._visited
                or current.name in self._failed
                or current.name in self._blocked
            ):
                return
            try:
                if self._is_complete(current):
                    self._visited.add(current.name)
                    return

                async with self._acquire_lock(current):
                    if self._is_complete(current):
                        self._visited.add(current.name)
                        return
                    dependencies = self._graph.dependencies_of(current.name)
                    await self._visit_many(dependencies)
                    if any(dependency.name not in self._visited for dependency in dependencies):
                        self._blocked.add(current.name)
                        return
                    async with self._capacity(current):
                        await self._runner(current)
                    self._publish(current)
                    if not self._is_complete(current):
                        raise ExecutionError(
                            f"node {current.name!r} returned without complete output"
                        )
                    self._visited.add(current.name)
            except Exception as error:
                self._failed[current.name] = error

    @asynccontextmanager
    async def _capacity(self, current: Node):
        pool = self._pools[current.pool]
        async with pool:
            if self._global is None or pool is self._global:
                yield
            else:
                async with self._global:
                    yield

    def _is_complete(self, current: Node) -> bool:
        result = self._is_complete_hook(current)
        if not isinstance(result, bool):
            raise ExecutionError(
                f"completion predicate for node {current.name!r} did not return bool"
            )
        return result

    async def _visit_many(self, nodes: tuple[Node, ...]) -> None:
        async with asyncio.TaskGroup() as tasks:
            for current in nodes:
                tasks.create_task(self._visit(current))

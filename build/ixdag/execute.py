# Copyright (c) pg83 contributors
# SPDX-License-Identifier: MIT
#
# Derived from pg83/ix core/execute.py (MIT):
# https://github.com/pg83/ix/blob/main/core/execute.py
#
# The adaptation preserves IX's demand-driven asyncio visitor, per-node lock,
# named semaphore pools, immutable output directories, completion marker, and
# trash-before-retry behavior. Windows changes are intentionally confined to
# paths and process creation.

"""Small, Windows-capable, IX-derived content-addressed DAG executor.

The default runner waits for and can terminate only its direct child. Reliable
Windows process-tree cancellation remains blocked on a Job Object runner; the
``runner`` injection seam exists so that support does not affect DAG semantics.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import uuid


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_OBJECT_ID = re.compile(r"[0-9a-f]{64}\Z")
_RESERVED_ENV = frozenset(("IX_NODE", "IX_OUT", "IX_POOL_CAPACITY"))


class GraphError(ValueError):
    """The graph or its filesystem boundary is invalid."""


class NodeExecutionError(RuntimeError):
    """A node command did not complete successfully."""


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    cwd: str = "."
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Node:
    name: str
    object_id: str
    deps: tuple[str, ...] = ()
    pool: str = "cpu"
    commands: tuple[Command, ...] = ()


@dataclass(frozen=True)
class Graph:
    nodes: tuple[Node, ...]
    targets: tuple[str, ...]
    pools: Mapping[str, int]


@dataclass(frozen=True)
class ProcessRequest:
    node_name: str
    pool: str
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    log_path: Path


@dataclass(frozen=True)
class NodeResult:
    status: str
    output_dir: Path
    log_path: Path


ProcessRunner = Callable[[ProcessRequest], Awaitable[int]]


@dataclass
class _VisitState:
    lock: asyncio.Lock
    result: NodeResult | None = None


async def run_process(request: ProcessRequest) -> int:
    """Run literal argv without a shell and merge output into the node log.

    Cancellation terminates the direct child and waits for it. A future Windows
    Job Object runner can be injected for guaranteed descendant termination.
    """

    request.log_path.parent.mkdir(parents=True, exist_ok=True)
    with request.log_path.open("ab", buffering=0) as stream:
        process = await asyncio.create_subprocess_exec(
            *request.argv,
            cwd=str(request.cwd),
            env=dict(request.env),
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        try:
            return await process.wait()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.terminate()
                await process.wait()
            raise


class Executor:
    """Execute only target ancestors using the core pg83/ix visit algorithm."""

    def __init__(
        self,
        graph: Graph,
        workspace: Path,
        state_root: Path,
        *,
        runner: ProcessRunner = run_process,
    ) -> None:
        self.graph = graph
        self.workspace = workspace.resolve(strict=True)
        self.state_root = state_root.resolve(strict=False)
        if self.state_root == self.workspace or not self.state_root.is_relative_to(self.workspace):
            raise GraphError("state root must be a strict child of workspace")

        self.runner = runner
        self.nodes = self._validate_graph(graph)
        self.pool_sizes = dict(graph.pools)
        self.pools = {name: asyncio.Semaphore(size) for name, size in self.pool_sizes.items()}
        self.visits = {
            name: _VisitState(lock=asyncio.Lock())
            for name in self.nodes
        }
        self.objects_root = self.state_root / "objects"
        self.logs_root = self.state_root / "logs"
        self.trash_root = self.state_root / "trash"
        for path in (self.objects_root, self.logs_root, self.trash_root):
            path.mkdir(parents=True, exist_ok=True)

    async def run(self) -> dict[str, NodeResult]:
        await self._visit_many(self.graph.targets)
        return {
            name: state.result
            for name, state in self.visits.items()
            if state.result is not None
        }

    def output_dir(self, node_name: str) -> Path:
        node = self.nodes[node_name]
        return self.objects_root / node.object_id[:2] / node.object_id

    async def _visit(self, name: str) -> NodeResult:
        state = self.visits[name]
        async with state.lock:
            if state.result is not None:
                return state.result

            node = self.nodes[name]
            if self._is_complete(node):
                state.result = self._result(node, "cached", f"CACHE {name}\n")
                return state.result

            await self._visit_many(node.deps)
            async with self.pools[node.pool]:
                state.result = await self._execute(node)
                return state.result

    async def _visit_many(self, names: tuple[str, ...]) -> None:
        tasks = [asyncio.create_task(self._visit(name)) for name in names]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _execute(self, node: Node) -> NodeResult:
        out_dir = self.output_dir(node.name)
        log_path = self._log_path(node)
        self._prepare_dir(out_dir)
        log_path.write_text(f"ENTER {node.name}\n", encoding="utf-8")

        try:
            for command in node.commands:
                request = ProcessRequest(
                    node_name=node.name,
                    pool=node.pool,
                    argv=command.argv,
                    cwd=self._command_cwd(command),
                    env=self._command_env(node, command, out_dir),
                    log_path=log_path,
                )
                return_code = await self.runner(request)
                if return_code != 0:
                    raise NodeExecutionError(
                        f"node {node.name!r} failed with exit code {return_code}; "
                        f"see {log_path}"
                    )

            self._publish_marker(node, out_dir)
            self._append_log(log_path, f"LEAVE {node.name}\n")
            return NodeResult("executed", out_dir, log_path)
        except asyncio.CancelledError:
            self._append_log(log_path, f"CANCEL {node.name}\n")
            self._move_to_trash(out_dir)
            raise
        except Exception as error:
            self._append_log(log_path, f"ERROR {node.name}: {error}\n")
            self._move_to_trash(out_dir)
            if isinstance(error, NodeExecutionError):
                raise
            raise NodeExecutionError(f"node {node.name!r} failed; see {log_path}") from error

    def _validate_graph(self, graph: Graph) -> dict[str, Node]:
        if not graph.nodes:
            raise GraphError("graph must contain nodes")
        nodes: dict[str, Node] = {}
        object_ids: set[str] = set()
        for node in graph.nodes:
            if not _NAME.fullmatch(node.name):
                raise GraphError(f"invalid node name: {node.name!r}")
            if node.name in nodes:
                raise GraphError(f"duplicate node: {node.name}")
            if not _OBJECT_ID.fullmatch(node.object_id):
                raise GraphError(f"node {node.name!r} has invalid object id")
            if node.object_id in object_ids:
                raise GraphError(f"duplicate object id: {node.object_id}")
            self._validate_commands(node)
            nodes[node.name] = node
            object_ids.add(node.object_id)

        self._validate_references(graph, nodes)
        self._validate_cycles(nodes)
        return nodes

    def _validate_commands(self, node: Node) -> None:
        for command in node.commands:
            if not command.argv or any(not isinstance(arg, str) or "\0" in arg for arg in command.argv):
                raise GraphError(f"node {node.name!r} has invalid argv")
            keys = [key for key, _value in command.env]
            if len(keys) != len(set(keys)):
                raise GraphError(f"node {node.name!r} has duplicate environment keys")
            if _RESERVED_ENV.intersection(keys):
                raise GraphError(f"node {node.name!r} overrides reserved environment")
            self._command_cwd(command)

    def _validate_references(self, graph: Graph, nodes: Mapping[str, Node]) -> None:
        if not graph.targets:
            raise GraphError("graph must contain targets")
        unknown_targets = sorted(set(graph.targets).difference(nodes))
        if unknown_targets:
            raise GraphError(f"unknown targets: {', '.join(unknown_targets)}")
        for name, size in graph.pools.items():
            if not _NAME.fullmatch(name) or isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                raise GraphError(f"invalid pool: {name!r}")
        for node in nodes.values():
            unknown = sorted(set(node.deps).difference(nodes))
            if unknown:
                raise GraphError(f"node {node.name!r} has unknown dependencies: {', '.join(unknown)}")
            if node.pool not in graph.pools:
                raise GraphError(f"node {node.name!r} uses unknown pool {node.pool!r}")

    @staticmethod
    def _validate_cycles(nodes: Mapping[str, Node]) -> None:
        active: list[str] = []
        complete: set[str] = set()

        def visit(name: str) -> None:
            if name in complete:
                return
            if name in active:
                start = active.index(name)
                raise GraphError("cycle: " + " -> ".join((*active[start:], name)))
            active.append(name)
            for dependency in nodes[name].deps:
                visit(dependency)
            active.pop()
            complete.add(name)

        for name in nodes:
            visit(name)

    def _command_cwd(self, command: Command) -> Path:
        candidate = Path(command.cwd)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.workspace):
            raise GraphError("command cwd must stay within workspace")
        if not resolved.is_dir():
            raise GraphError(f"command cwd is not a directory: {resolved}")
        return resolved

    def _command_env(self, node: Node, command: Command, out_dir: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.update(command.env)
        env.update(
            {
                "IX_NODE": node.name,
                "IX_OUT": str(out_dir),
                "IX_POOL_CAPACITY": str(self.pool_sizes[node.pool]),
            }
        )
        return env

    def _is_complete(self, node: Node) -> bool:
        out_dir = self.output_dir(node.name)
        if self._is_link(out_dir) or not out_dir.is_dir():
            return False
        marker = out_dir / ".complete"
        try:
            return marker.is_file() and marker.read_text(encoding="utf-8") == self._marker_text(node)
        except OSError:
            return False

    def _prepare_dir(self, path: Path) -> None:
        self._move_to_trash(path)
        path.mkdir(parents=True, exist_ok=False)

    def _move_to_trash(self, path: Path) -> None:
        if not os.path.lexists(path):
            return
        destination = self.trash_root / f"{path.name}-{uuid.uuid4().hex}"
        os.replace(path, destination)

    @staticmethod
    def _is_link(path: Path) -> bool:
        is_junction = getattr(path, "is_junction", lambda: False)
        return path.is_symlink() or is_junction()

    @staticmethod
    def _marker_text(node: Node) -> str:
        return json.dumps(
            {"object_id": node.object_id, "schema": 1},
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"

    def _publish_marker(self, node: Node, out_dir: Path) -> None:
        temporary = out_dir / f".complete-{uuid.uuid4().hex}.tmp"
        temporary.write_text(self._marker_text(node), encoding="utf-8")
        os.replace(temporary, out_dir / ".complete")

    def _log_path(self, node: Node) -> Path:
        return self.logs_root / f"{node.name}-{node.object_id[:12]}.log"

    def _result(self, node: Node, status: str, message: str) -> NodeResult:
        log_path = self._log_path(node)
        log_path.write_text(message, encoding="utf-8")
        return NodeResult(status, self.output_dir(node.name), log_path)

    @staticmethod
    def _append_log(path: Path, message: str) -> None:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(message)

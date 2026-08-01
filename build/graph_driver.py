#!/usr/bin/env python3
"""Stdlib-only DAG runner for local Observer build and verification work."""

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
import glob
import hashlib
import heapq
from itertools import product
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import threading
import time
from typing import Callable, Mapping, Sequence, TextIO
import uuid


SCHEMA = 2
NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VARIABLE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
GOOD = frozenset({"succeeded", "cached"})
BAD = frozenset({"failed", "blocked", "cancelled"})
FAILURE_POLICIES = frozenset({"continue", "fail-fast"})


class GraphValidationError(ValueError):
    pass


class CycleError(GraphValidationError):
    pass


@dataclass(frozen=True)
class Node:
    name: str
    deps: tuple[str, ...]
    run_after: tuple[str, ...]
    resources: tuple[tuple[str, int], ...]
    argv: tuple[str, ...]
    inputs: tuple[str, ...]
    writes: tuple[str, ...]
    outputs: tuple[str, ...]
    fingerprint_tokens: tuple[str, ...]
    cacheable: bool

    @property
    def prerequisites(self) -> tuple[str, ...]:
        return self.deps + self.run_after


@dataclass(frozen=True)
class PlannedNode:
    node: Node
    fingerprint: str


@dataclass(frozen=True)
class NodeResult:
    name: str
    status: str
    return_code: int | None
    duration_seconds: float
    log_path: Path
    fingerprint: str
    detail: str = ""
    output_manifest: tuple[Mapping[str, str], ...] = ()


@dataclass(frozen=True)
class RunSummary:
    plan: tuple[PlannedNode, ...]
    results: Mapping[str, NodeResult]
    duration_seconds: float

    @property
    def succeeded(self) -> bool:
        return all(result.status in GOOD for result in self.results.values())


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise GraphValidationError(f"{label} must be an array of strings")
    return tuple(value)


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise GraphValidationError(f"invalid {label}: {value!r}")
    return value


def _variable_name(value: object, label: str) -> str:
    if not isinstance(value, str) or not VARIABLE_NAME.fullmatch(value):
        raise GraphValidationError(f"invalid {label}: {value!r}")
    return value


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GraphValidationError(f"{label} must be a positive integer")
    return value


def _relative(value: str, label: str, *, patterns: bool) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or path.parts[0].endswith(":")
        or (not patterns and glob.has_magic(value))
    ):
        raise GraphValidationError(f"unsafe {label}: {value!r}")


def _inside(root: Path, path: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(root), os.path.normcase(path))) == os.path.normcase(root)
    except ValueError:
        return False


def _path(root: Path, relative: str) -> Path:
    result = root.joinpath(*PurePosixPath(relative).parts).resolve()
    if not _inside(root, result):
        raise GraphValidationError(f"path resolves outside the workspace: {relative!r}")
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return path.is_symlink() or bool(attributes & 0x400)


class _WorkspaceRunLock:
    """One non-blocking OS lock serializes graph runs that share a workspace."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.handle = None

    def __enter__(self):
        lock_directory = _path(self.workspace, ".artifacts/graph")
        lock_directory.mkdir(parents=True, exist_ok=True)
        lock_path = lock_directory / ".workspace-run.lock"
        if _is_reparse_point(lock_path):
            raise GraphValidationError(f"workspace run lock is a reparse point: {lock_path}")
        handle = lock_path.open("a+b", buffering=0)
        if lock_path.stat().st_size == 0:
            handle.write(b"\0")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise GraphValidationError(
                f"a graph run is already running in workspace {self.workspace}"
            ) from error
        self.handle = handle
        return self

    def __exit__(self, exception_type, _exception, _traceback):
        if self.handle is None:
            return False
        unlock_error = None
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            unlock_error = error
        finally:
            self.handle.close()
            self.handle = None
        if unlock_error is not None and exception_type is None:
            raise GraphValidationError("failed to release the workspace graph run lock") from unlock_error
        return False


class Graph:
    def __init__(self, name, workspace, resources, nodes, targets, failure_policy):
        self.name = name
        self.workspace = workspace
        self.resources = resources
        self.nodes = nodes
        self.targets = targets
        self.failure_policy = failure_policy

    @classmethod
    def from_mapping(cls, name: str, mapping: object, workspace: Path) -> "Graph":
        graph_name = _name(name, "graph name")
        root = Path(workspace).resolve()
        if not isinstance(mapping, dict) or not root.is_dir():
            raise GraphValidationError("graph mapping and existing workspace are required")
        if "pools" in mapping:
            raise GraphValidationError("legacy pools are not accepted by schema 2")
        allowed_graph_keys = {"failure_policy", "resources", "targets", "nodes"}
        unexpected_graph_keys = set(mapping) - allowed_graph_keys
        if unexpected_graph_keys:
            raise GraphValidationError(f"unknown graph fields: {sorted(unexpected_graph_keys)}")

        raw_resources = mapping.get("resources")
        if not isinstance(raw_resources, dict) or not raw_resources:
            raise GraphValidationError("named resources are required")
        resources: dict[str, int] = {}
        for key, capacity in raw_resources.items():
            resource = _name(key, "resource name")
            resources[resource] = _positive_integer(capacity, f"capacity for {resource}")

        failure_policy = mapping.get("failure_policy", "continue")
        if failure_policy not in FAILURE_POLICIES:
            raise GraphValidationError("failure_policy must be continue or fail-fast")

        raw_nodes = mapping.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise GraphValidationError("nodes are required")
        nodes: dict[str, Node] = {}
        allowed_node_keys = {
            "name",
            "deps",
            "run_after",
            "resources",
            "argv",
            "inputs",
            "writes",
            "outputs",
            "fingerprint",
            "cacheable",
        }
        for raw in raw_nodes:
            if not isinstance(raw, dict):
                raise GraphValidationError("every node must be an object")
            if "pool" in raw:
                raise GraphValidationError("legacy pool is not accepted by schema 2")
            unexpected = set(raw) - allowed_node_keys
            if unexpected:
                raise GraphValidationError(f"unknown node fields: {sorted(unexpected)}")
            current = _name(raw.get("name"), "node name")
            if current in nodes:
                raise GraphValidationError(f"duplicate node: {current}")

            deps = _strings(raw.get("deps"), f"deps for {current}")
            run_after = _strings(raw.get("run_after"), f"run_after for {current}")
            argv = _strings(raw.get("argv"), f"argv for {current}")
            inputs = _strings(raw.get("inputs"), f"inputs for {current}")
            writes = _strings(raw.get("writes"), f"writes for {current}")
            outputs = _strings(raw.get("outputs"), f"outputs for {current}")
            tokens = _strings(raw.get("fingerprint"), f"fingerprint for {current}")
            cacheable = raw.get("cacheable")
            raw_demands = raw.get("resources")
            if not argv or any(not arg for arg in argv):
                raise GraphValidationError(f"non-empty argv is required for {current}")
            if len(deps) != len(set(deps)) or len(run_after) != len(set(run_after)):
                raise GraphValidationError(f"duplicate prerequisite for {current}")
            overlap = set(deps) & set(run_after)
            if overlap:
                raise GraphValidationError(
                    f"prerequisite cannot be both deps and run_after for {current}: {sorted(overlap)}"
                )
            if not isinstance(raw_demands, dict) or not raw_demands:
                raise GraphValidationError(f"resources for {current} must be a non-empty object")
            demands: dict[str, int] = {}
            for key, raw_demand in raw_demands.items():
                resource = _name(key, f"resource for {current}")
                if resource not in resources:
                    raise GraphValidationError(f"unknown resource {resource!r} for {current}")
                demand = _positive_integer(raw_demand, f"demand for {current}/{resource}")
                if demand > resources[resource]:
                    raise GraphValidationError(
                        f"resource demand for {current}/{resource} exceeds capacity {resources[resource]}"
                    )
                demands[resource] = demand
            if not isinstance(cacheable, bool):
                raise GraphValidationError(f"cacheable must be boolean for {current}")
            if cacheable and not outputs:
                raise GraphValidationError(f"cacheable node {current!r} requires explicit outputs")
            if len(writes) != len(set(writes)) or len(outputs) != len(set(outputs)):
                raise GraphValidationError(f"duplicate write or output path for {current}")
            for item in inputs:
                _relative(item, f"input for {current}", patterns=True)
            for item in writes:
                _relative(item, f"write for {current}", patterns=False)
                _path(root, item)
            for item in outputs:
                _relative(item, f"output for {current}", patterns=False)
                output_path = _path(root, item)
                if not any(_inside(_path(root, write), output_path) for write in writes):
                    raise GraphValidationError(
                        f"output for {current} is not below a declared write root: {item!r}"
                    )
            nodes[current] = Node(
                current,
                tuple(sorted(deps)),
                tuple(sorted(run_after)),
                tuple(sorted(demands.items())),
                argv,
                tuple(sorted(inputs)),
                tuple(sorted(writes)),
                tuple(sorted(outputs)),
                tuple(sorted(tokens)),
                cacheable,
            )

        for current in nodes.values():
            unknown = set(current.prerequisites) - nodes.keys()
            if unknown:
                raise GraphValidationError(f"unknown prerequisites for {current.name}: {sorted(unknown)}")
        targets = _strings(mapping.get("targets"), "targets")
        if not targets or len(targets) != len(set(targets)) or set(targets) - nodes.keys():
            raise GraphValidationError("targets must be unique, non-empty, and known")
        return cls(graph_name, root, resources, nodes, tuple(sorted(targets)), failure_policy)

    def _closure(self, targets: Sequence[str]) -> set[str]:
        if set(targets) - self.nodes.keys():
            raise GraphValidationError("unknown target")
        result, pending = set(), list(targets)
        while pending:
            current = pending.pop()
            if current not in result:
                result.add(current)
                pending.extend(self.nodes[current].prerequisites)
        return result

    def _cycle(self, remaining: set[str]) -> list[str]:
        state: dict[str, int] = {}
        stack: list[str] = []
        positions: dict[str, int] = {}

        def visit(current):
            state[current], positions[current] = 1, len(stack)
            stack.append(current)
            for dependency in self.nodes[current].prerequisites:
                if dependency not in remaining:
                    continue
                if not state.get(dependency):
                    found = visit(dependency)
                    if found:
                        return found
                elif state[dependency] == 1:
                    return stack[positions[dependency] :] + [dependency]
            stack.pop()
            positions.pop(current)
            state[current] = 2
            return None

        for current in sorted(remaining):
            if not state.get(current) and (found := visit(current)):
                return found
        return sorted(remaining)

    def _order(self, targets: Sequence[str]) -> list[str]:
        closure = self._closure(targets)
        indegree = {
            name: sum(dependency in closure for dependency in self.nodes[name].prerequisites)
            for name in closure
        }
        children = {name: [] for name in closure}
        for name in closure:
            for dependency in self.nodes[name].prerequisites:
                if dependency in closure:
                    children[dependency].append(name)
        ready = [name for name, count in indegree.items() if not count]
        heapq.heapify(ready)
        ordered: list[str] = []
        while ready:
            current = heapq.heappop(ready)
            ordered.append(current)
            for child in sorted(children[current]):
                indegree[child] -= 1
                if not indegree[child]:
                    heapq.heappush(ready, child)
        if len(ordered) != len(closure):
            cycle = self._cycle(closure - set(ordered))
            raise CycleError(f"cycle detected: {' -> '.join(cycle)}")
        return ordered

    def _validate_write_conflicts(self, ordered: Sequence[str]) -> None:
        ancestors: dict[str, set[str]] = {}
        for name in ordered:
            current: set[str] = set()
            for dependency in self.nodes[name].prerequisites:
                if dependency in ancestors:
                    current.add(dependency)
                    current.update(ancestors[dependency])
            ancestors[name] = current

        for left_index, left_name in enumerate(ordered):
            left = self.nodes[left_name]
            left_resources = dict(left.resources)
            for right_name in ordered[left_index + 1 :]:
                right = self.nodes[right_name]
                if left_name in ancestors[right_name] or right_name in ancestors[left_name]:
                    continue
                right_resources = dict(right.resources)
                has_lock = any(
                    self.resources[resource] == 1 and resource in right_resources
                    for resource in left_resources
                )
                if has_lock:
                    continue
                for left_write in left.writes:
                    left_path = _path(self.workspace, left_write)
                    for right_write in right.writes:
                        right_path = _path(self.workspace, right_write)
                        if _inside(left_path, right_path) or _inside(right_path, left_path):
                            raise GraphValidationError(
                                f"overlapping writes require dependency order or a shared capacity-one resource: "
                                f"{left_name}:{left_write!r}, {right_name}:{right_write!r}"
                            )

    def _input_records(
        self,
        node: Node,
        digests: dict[Path, str],
    ) -> list[tuple[str, str]]:
        records: dict[str, str] = {}
        missing: list[tuple[str, str]] = []
        for pattern in node.inputs:
            matches = sorted(path for path in self.workspace.glob(pattern) if path.is_file())
            if not matches:
                missing.append((pattern, "missing"))
            for match in matches:
                resolved = match.resolve()
                if not _inside(self.workspace, resolved):
                    raise GraphValidationError(f"input escapes workspace: {match}")
                if resolved not in digests:
                    digests[resolved] = _sha256_file(resolved)
                records[match.relative_to(self.workspace).as_posix()] = digests[resolved]
        return sorted(records.items()) + missing

    def _planned_node(
        self,
        name: str,
        fingerprints: Mapping[str, str],
        digests: dict[Path, str],
    ) -> PlannedNode:
        node = self.nodes[name]
        payload = {
            "schema": SCHEMA,
            "name": name,
            "argv": node.argv,
            "resources": dict(node.resources),
            "cacheable": node.cacheable,
            "writes": node.writes,
            "outputs": node.outputs,
            "tokens": node.fingerprint_tokens,
            "deps": {dep: fingerprints[dep] for dep in node.deps},
            "run_after": {dep: fingerprints[dep] for dep in node.run_after},
            "inputs": self._input_records(node, digests),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return PlannedNode(node, hashlib.sha256(encoded).hexdigest())

    def execution_order(self, targets: Sequence[str] | None = None) -> tuple[str, ...]:
        ordered = self._order(targets or self.targets)
        self._validate_write_conflicts(ordered)
        return tuple(ordered)

    def plan(self, targets: Sequence[str] | None = None) -> tuple[PlannedNode, ...]:
        fingerprints: dict[str, str] = {}
        digests: dict[Path, str] = {}
        result: list[PlannedNode] = []
        for name in self.execution_order(targets):
            item = self._planned_node(name, fingerprints, digests)
            fingerprints[name] = item.fingerprint
            result.append(item)
        return tuple(result)


Executor = Callable[[Node, Path, TextIO, threading.Event], int]


def execute_subprocess(
    node: Node,
    workspace: Path,
    stream: TextIO,
    _cancellation: threading.Event,
) -> int:
    return subprocess.run(
        list(node.argv),
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        stdout=stream,
        stderr=subprocess.STDOUT,
        check=False,
        shell=False,
    ).returncode


class GraphRunner:
    def __init__(
        self,
        graph: Graph,
        logs_dir: Path,
        state_dir: Path,
        *,
        max_workers: int | None = None,
        executor: Executor = execute_subprocess,
    ):
        self.graph, self.executor = graph, executor
        self.logs_root, self.state_dir = Path(logs_dir).resolve(), Path(state_dir).resolve()
        if not _inside(graph.workspace, self.logs_root) or not _inside(graph.workspace, self.state_dir):
            raise GraphValidationError("log and state paths must stay inside the workspace")
        if max_workers is None:
            self.max_workers = sum(graph.resources.values())
        else:
            self.max_workers = max_workers
        if isinstance(self.max_workers, bool) or not isinstance(self.max_workers, int) or self.max_workers < 1:
            raise GraphValidationError("max_workers must be positive")

    def _new_logs_dir(self) -> Path:
        self.logs_root.mkdir(parents=True, exist_ok=True)
        for _ in range(10):
            run_id = f"run-{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex}"
            target = self.logs_root / run_id
            try:
                target.mkdir()
            except FileExistsError:
                continue
            return target
        raise RuntimeError("could not allocate a unique graph log directory")

    @staticmethod
    def _log(logs_dir: Path, node: Node) -> Path:
        return logs_dir / f"{node.name}.log"

    def _state(self, node: Node) -> Path:
        return self.state_dir / f"{node.name}.json"

    def _tree_digest(self, root: Path) -> str:
        digest = hashlib.sha256()
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            directories.sort()
            files.sort()
            for name in directories:
                entry = current_path / name
                if _is_reparse_point(entry):
                    raise GraphValidationError(f"output tree contains a reparse point: {entry}")
                relative = entry.relative_to(root).as_posix()
                digest.update(f"D\0{relative}\0".encode())
            for name in files:
                entry = current_path / name
                if _is_reparse_point(entry):
                    raise GraphValidationError(f"output tree contains a reparse point: {entry}")
                relative = entry.relative_to(root).as_posix()
                digest.update(f"F\0{relative}\0{_sha256_file(entry)}\0".encode())
        return digest.hexdigest()

    def _output_manifest(self, node: Node) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        for relative in node.outputs:
            raw_path = self.graph.workspace.joinpath(*PurePosixPath(relative).parts)
            if _is_reparse_point(raw_path):
                raise GraphValidationError(f"output is a reparse point: {relative!r}")
            path = _path(self.graph.workspace, relative)
            if path.is_file():
                records.append({"kind": "file", "path": relative, "sha256": _sha256_file(path)})
            elif path.is_dir():
                records.append({"kind": "directory", "path": relative, "sha256": self._tree_digest(path)})
            else:
                raise FileNotFoundError(relative)
        return records

    def _cached(self, item: PlannedNode) -> bool:
        if not item.node.cacheable:
            return False
        try:
            state = json.loads(self._state(item.node).read_text(encoding="utf-8"))
            manifest = self._output_manifest(item.node)
        except (OSError, json.JSONDecodeError, GraphValidationError):
            return False
        return state == {
            "fingerprint": item.fingerprint,
            "outputs": manifest,
            "schema": SCHEMA,
        }

    def _invalidate(self, item: PlannedNode) -> None:
        if item.node.cacheable:
            self._state(item.node).unlink(missing_ok=True)

    def _save(
        self,
        item: PlannedNode,
        manifest: Sequence[Mapping[str, str]],
    ) -> None:
        target = self._state(item.node)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "fingerprint": item.fingerprint,
                        "outputs": list(manifest),
                        "schema": SCHEMA,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _record(self, item: PlannedNode, logs_dir: Path, status: str, detail: str = "") -> NodeResult:
        log = self._log(logs_dir, item.node)
        log.write_text(f"[{status}] {detail}\n", encoding="utf-8")
        return NodeResult(item.node.name, status, None, 0.0, log, item.fingerprint, detail)

    def _execute(
        self,
        item: PlannedNode,
        logs_dir: Path,
        cancellation: threading.Event,
    ) -> NodeResult:
        started, node, log = time.perf_counter(), item.node, self._log(logs_dir, item.node)
        code: int | None = None
        detail = ""
        manifest: tuple[Mapping[str, str], ...] = ()
        try:
            with log.open("w", encoding="utf-8") as stream:
                stream.write(f"fingerprint={item.fingerprint}\nargv={json.dumps(node.argv)}\n")
                stream.flush()
                code = self.executor(node, self.graph.workspace, stream, cancellation)
                if isinstance(code, bool) or not isinstance(code, int):
                    raise TypeError("executor must return an integer")
        except Exception as error:
            detail = f"executor error: {type(error).__name__}: {error}"
            with log.open("a", encoding="utf-8") as stream:
                stream.write(detail + "\n")
        if code == 0 and not detail and node.cacheable:
            try:
                manifest = tuple(self._output_manifest(node))
            except Exception as error:
                detail = f"output manifest error: {type(error).__name__}: {error}"
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(detail + "\n")
        if cancellation.is_set():
            status = "cancelled"
            detail = detail or "cancelled after fail-fast failure"
        else:
            status = "succeeded" if code == 0 and not detail else "failed"
        return NodeResult(
            node.name,
            status,
            code,
            time.perf_counter() - started,
            log,
            item.fingerprint,
            detail,
            manifest,
        )

    def run(self, targets: Sequence[str] | None = None) -> RunSummary:
        with _WorkspaceRunLock(self.graph.workspace):
            return self._run(targets)

    def _run(self, targets: Sequence[str] | None = None) -> RunSummary:
        started = time.perf_counter()
        ordered = self.graph.execution_order(targets)
        order_index = {name: index for index, name in enumerate(ordered)}
        items: dict[str, PlannedNode] = {}
        fingerprints: dict[str, str] = {}
        digests: dict[Path, str] = {}
        results: dict[str, NodeResult] = {}
        used = {resource: 0 for resource in self.graph.resources}
        running: dict[object, PlannedNode] = {}
        checking: dict[object, PlannedNode] = {}
        remaining = {
            name: len(self.graph.nodes[name].prerequisites)
            for name in ordered
        }
        children = {name: [] for name in ordered}
        for name in ordered:
            for dependency in self.graph.nodes[name].prerequisites:
                children[dependency].append(name)
        for dependents in children.values():
            dependents.sort(key=order_index.__getitem__)
        ready = deque((name, False) for name in ordered if remaining[name] == 0)
        logs_dir = self._new_logs_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        aborting = False
        cancellation = threading.Event()

        def ensure_item(name: str) -> PlannedNode:
            if name not in items:
                item = self.graph._planned_node(name, fingerprints, digests)
                items[name] = item
                fingerprints[name] = item.fingerprint
            return items[name]

        def placeholder(name: str) -> PlannedNode:
            return items.get(name, PlannedNode(self.graph.nodes[name], ""))

        def finish(name: str, result: NodeResult) -> None:
            results[name] = result
            for child in children[name]:
                remaining[child] -= 1
                if remaining[child] == 0 and child not in results:
                    ready.append((child, False))

        def fits(item: PlannedNode) -> bool:
            if len(running) + len(checking) >= self.max_workers:
                return False
            return all(
                used[resource] + demand <= self.graph.resources[resource]
                for resource, demand in item.node.resources
            )

        def reserve(item: PlannedNode, direction: int) -> None:
            for resource, demand in item.node.resources:
                used[resource] += direction * demand

        def invalidate_digests(node: Node) -> None:
            roots = tuple(_path(self.graph.workspace, write) for write in node.writes)
            for cached_path in tuple(digests):
                if any(_inside(root, cached_path) for root in roots):
                    digests.pop(cached_path)

        def state_failure(item: PlannedNode, result: NodeResult, error: Exception) -> NodeResult:
            detail = f"cache state error: {type(error).__name__}: {error}"
            try:
                self._invalidate(item)
            except OSError as invalidation_error:
                detail += f"; state invalidation error: {invalidation_error}"
            with result.log_path.open("a", encoding="utf-8") as stream:
                stream.write(detail + "\n")
            return replace(result, status="failed", detail=detail, output_manifest=())

        with (
            ThreadPoolExecutor(max_workers=self.max_workers) as workers,
            ThreadPoolExecutor(max_workers=self.max_workers) as cache_workers,
        ):
            while ready or running or checking:
                dispatch_progress = False
                for _ in range(len(ready)):
                    name, cache_checked = ready.popleft()
                    item = ensure_item(name)
                    if aborting:
                        finish(name, self._record(item, logs_dir, "cancelled", "fail-fast policy"))
                        dispatch_progress = True
                        continue
                    failed = sorted(
                        dependency
                        for dependency in item.node.deps
                        if results[dependency].status in BAD
                    )
                    if failed:
                        finish(
                            name,
                            self._record(item, logs_dir, "blocked", f"dependency failure: {', '.join(failed)}"),
                        )
                        dispatch_progress = True
                        continue
                    prerequisites_cached = all(
                        results[dependency].status == "cached"
                        for dependency in item.node.prerequisites
                    )
                    if prerequisites_cached and item.node.cacheable and not cache_checked:
                        if fits(item):
                            reserve(item, 1)
                            checking[cache_workers.submit(self._cached, item)] = item
                            dispatch_progress = True
                        else:
                            ready.append((name, cache_checked))
                        continue
                    if fits(item):
                        try:
                            self._invalidate(item)
                        except OSError as error:
                            finish(
                                name,
                                self._record(
                                    item,
                                    logs_dir,
                                    "failed",
                                    f"cache state invalidation error: {type(error).__name__}: {error}",
                                ),
                            )
                            dispatch_progress = True
                            continue
                        reserve(item, 1)
                        future = workers.submit(self._execute, item, logs_dir, cancellation)
                        running[future] = item
                        dispatch_progress = True
                    else:
                        ready.append((name, cache_checked))

                if running or checking:
                    done, _ = wait(tuple(running) + tuple(checking), return_when=FIRST_COMPLETED)
                    for future in sorted(
                        (current for current in done if current in checking),
                        key=lambda current: order_index[checking[current].node.name],
                    ):
                        item = checking.pop(future)
                        reserve(item, -1)
                        if item.node.name in results:
                            continue
                        if future.result():
                            finish(
                                item.node.name,
                                self._record(item, logs_dir, "cached", "fingerprint and outputs match"),
                            )
                        else:
                            ready.append((item.node.name, True))
                    failed_now = False
                    for future in sorted(
                        (current for current in done if current in running),
                        key=lambda current: order_index[running[current].node.name],
                    ):
                        item = running.pop(future)
                        reserve(item, -1)
                        result = future.result()
                        invalidate_digests(item.node)
                        if result.status == "succeeded" and item.node.cacheable:
                            try:
                                self._save(item, result.output_manifest)
                            except Exception as error:
                                result = state_failure(item, result, error)
                        finish(item.node.name, result)
                        failed_now = failed_now or result.status == "failed"
                    if failed_now and self.graph.failure_policy == "fail-fast":
                        aborting = True
                        cancellation.set()
                        running_names = {item.node.name for item in running.values()}
                        for name in ordered:
                            if name not in results and name not in running_names:
                                results[name] = self._record(
                                    placeholder(name),
                                    logs_dir,
                                    "cancelled",
                                    "fail-fast policy",
                                )
                        ready.clear()
                elif ready and not dispatch_progress:
                    raise RuntimeError("validated scheduler made no progress")

        for name in ordered:
            item = ensure_item(name)
            if results[name].fingerprint != item.fingerprint:
                results[name] = replace(results[name], fingerprint=item.fingerprint)
        plan = tuple(items[name] for name in ordered)
        return RunSummary(plan, results, time.perf_counter() - started)


def _expand(value, variables):
    if isinstance(value, str):
        def replace(match):
            try:
                return variables[match.group(1)]
            except KeyError as error:
                raise GraphValidationError(f"unknown variable: {error.args[0]}") from error

        return VARIABLE.sub(replace, value)
    if isinstance(value, list):
        return [_expand(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, variables) for key, item in value.items()}
    return value


def _expand_nodes(raw_nodes: object, variables: Mapping[str, str]) -> list[object]:
    if not isinstance(raw_nodes, list):
        raise GraphValidationError("nodes are required")
    expanded_nodes: list[object] = []
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            raise GraphValidationError("every node must be an object")
        matrix = raw_node.get("matrix")
        if matrix is None:
            expanded_nodes.append(_expand(raw_node, variables))
            continue
        if not isinstance(matrix, dict) or set(matrix) != {"axes", "exclude"}:
            raise GraphValidationError("matrix must contain exactly axes and exclude")
        raw_axes = matrix["axes"]
        if not isinstance(raw_axes, dict) or not raw_axes:
            raise GraphValidationError("matrix axes must be a non-empty object")
        axes: dict[str, tuple[str, ...]] = {}
        for raw_name, raw_values in raw_axes.items():
            axis = _variable_name(raw_name, "matrix axis")
            if axis in variables:
                raise GraphValidationError(f"matrix axis {axis!r} collides with graph variable")
            values = _strings(_expand(raw_values, variables), f"values for matrix axis {axis}")
            if not values or any(not value for value in values) or len(values) != len(set(values)):
                raise GraphValidationError(f"matrix axis {axis!r} needs unique non-empty values")
            axes[axis] = tuple(sorted(values))

        raw_excludes = matrix["exclude"]
        if not isinstance(raw_excludes, list) or any(not isinstance(item, dict) for item in raw_excludes):
            raise GraphValidationError("matrix exclude must be an array of exact assignments")
        axis_names = tuple(sorted(axes))
        excludes: set[tuple[str, ...]] = set()
        for raw_exclude in raw_excludes:
            expanded_exclude = _expand(raw_exclude, variables)
            if set(expanded_exclude) != set(axis_names):
                raise GraphValidationError("matrix exclude must be an exact assignment of every axis")
            assignment: list[str] = []
            for axis in axis_names:
                value = expanded_exclude[axis]
                if not isinstance(value, str) or value not in axes[axis]:
                    raise GraphValidationError("matrix exclude must be an exact known assignment")
                assignment.append(value)
            key = tuple(assignment)
            if key in excludes:
                raise GraphValidationError("duplicate matrix exclude assignment")
            excludes.add(key)

        template = dict(raw_node)
        template.pop("matrix")
        for values in product(*(axes[axis] for axis in axis_names)):
            if values in excludes:
                continue
            matrix_variables = {**variables, **dict(zip(axis_names, values, strict=True))}
            expanded_nodes.append(_expand(template, matrix_variables))
    return expanded_nodes


def load_graph_profile(profile_path, graph_name, workspace, overrides=None):
    try:
        profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GraphValidationError(f"cannot read profile: {error}") from error
    if not isinstance(profile, dict) or profile.get("schema") != SCHEMA:
        raise GraphValidationError(f"profile schema must be {SCHEMA}")
    selected = graph_name or profile.get("default_graph")
    try:
        raw = profile["graphs"][selected]
    except (KeyError, TypeError) as error:
        raise GraphValidationError(f"unknown graph: {selected!r}") from error
    if not isinstance(raw, dict):
        raise GraphValidationError("selected graph must be an object")
    declared_variables = raw.get("variables", {})
    if not isinstance(declared_variables, dict) or any(
        not isinstance(value, str) for value in declared_variables.values()
    ):
        raise GraphValidationError("variables must map names to strings")
    for key in declared_variables:
        _variable_name(key, "variable name")
    if "python" in declared_variables:
        raise GraphValidationError("python is a reserved variable")
    variables = {"python": str(Path(sys.executable).resolve()), **declared_variables}
    for key, value in (overrides or {}).items():
        if key not in declared_variables:
            raise GraphValidationError(f"undeclared override: {key}")
        variables[key] = value

    normalized = {
        key: _expand(value, variables)
        for key, value in raw.items()
        if key not in {"variables", "nodes"}
    }
    normalized["nodes"] = _expand_nodes(raw.get("nodes"), variables)
    return Graph.from_mapping(selected, normalized, Path(workspace))


def plan_as_json(plan):
    rows = [
        {
            "name": item.node.name,
            "deps": item.node.deps,
            "run_after": item.node.run_after,
            "resources": dict(item.node.resources),
            "argv": item.node.argv,
            "inputs": item.node.inputs,
            "writes": item.node.writes,
            "outputs": item.node.outputs,
            "cacheable": item.node.cacheable,
            "fingerprint": item.fingerprint,
        }
        for item in plan
    ]
    return json.dumps(rows, indent=2, sort_keys=True) + "\n"


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "run"):
        child = commands.add_parser(command)
        child.add_argument("--profile", type=Path, default=Path(__file__).with_name("graph_profiles.json"))
        child.add_argument("--graph")
        child.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parent.parent)
        child.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
        child.add_argument("--target", action="append", default=[])
    commands.choices["plan"].add_argument("--json", action="store_true")
    commands.choices["run"].add_argument("--jobs", type=int)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        overrides = {}
        for item in args.set:
            name, separator, value = item.partition("=")
            if not separator or not name or name in overrides:
                raise GraphValidationError(f"invalid --set: {item!r}")
            overrides[name] = value
        graph = load_graph_profile(args.profile, args.graph, args.workspace, overrides)
        targets = args.target or None
        if args.command == "plan":
            plan = graph.plan(targets)
            if args.json:
                print(plan_as_json(plan), end="")
            else:
                for index, item in enumerate(plan, 1):
                    policy = "cacheable" if item.node.cacheable else "always-run"
                    resources = ",".join(f"{name}={demand}" for name, demand in item.node.resources)
                    print(
                        f"{index:02d} {item.node.name} resources={resources} "
                        f"{policy} fingerprint={item.fingerprint[:16]}"
                    )
            return 0
        base = graph.workspace / ".artifacts" / "graph" / graph.name
        summary = GraphRunner(graph, base / "logs", base / "state", max_workers=args.jobs).run(targets)
        for item in summary.plan:
            result = summary.results[item.node.name]
            print(f"{result.status:9} {result.name} {result.duration_seconds:.3f}s log={result.log_path}")
        print(f"total {summary.duration_seconds:.3f}s")
        return 0 if summary.succeeded else 1
    except GraphValidationError as error:
        print(f"graph error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

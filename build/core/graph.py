"""Small named DAG model adapted from pg83/ix (MIT) at commit 66726a9."""

from __future__ import annotations

from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
import re
from types import MappingProxyType
from typing import Mapping


class GraphError(ValueError):
    """The graph is incomplete, ambiguous, or cyclic."""


NODE_SLUG_MAX_LENGTH = 128
_SLUG = re.compile(rf"[a-z0-9][a-z0-9._-]{{0,{NODE_SLUG_MAX_LENGTH - 1}}}")
_MD5_UID = re.compile(r"[0-9a-f]{32}")
_RESULT_ID = re.compile(r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*")
_RESULT_KIND = re.compile(r"[a-z][a-z0-9._-]*")
_MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*")


def _text(value: object, description: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise GraphError(f"{description} must be a non-empty string without NUL")
    return value


@dataclass(frozen=True, slots=True)
class Command:
    """One literal process invocation; no field is shell-translated."""

    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    cwd: str | None = None
    stdin: bytes = b""

    def __post_init__(self) -> None:
        argv = tuple(self.argv)
        if not argv:
            raise GraphError("command argv must not be empty")
        for argument in argv:
            if not isinstance(argument, str) or "\0" in argument:
                raise GraphError("command argument must be a string without NUL")

        try:
            env = tuple((key, value) for key, value in self.env)
        except (TypeError, ValueError) as error:
            raise GraphError("command environment must contain key/value pairs") from error
        for key, value in env:
            _text(key, "environment key")
            if not isinstance(value, str) or "\0" in value:
                raise GraphError("environment value must be a string without NUL")
        folded_keys = [key.casefold() for key, _value in env]
        if len(folded_keys) != len(set(folded_keys)):
            raise GraphError("command environment contains duplicate keys")
        if self.cwd is not None:
            _text(self.cwd, "command cwd")
        if type(self.stdin) is not bytes:
            raise GraphError("command stdin must be exact bytes")

        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "env", tuple(sorted(env, key=lambda pair: pair[0].casefold())))


@dataclass(frozen=True, slots=True)
class Result:
    """One stable logical result at a portable path below the node output."""

    id: str
    kind: str
    media_type: str
    relative_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or _RESULT_ID.fullmatch(self.id) is None:
            raise GraphError("result id must be a lowercase slash-separated identifier")
        if not isinstance(self.kind, str) or _RESULT_KIND.fullmatch(self.kind) is None:
            raise GraphError("result kind must be a lowercase identifier")
        if not isinstance(self.media_type, str) or _MEDIA_TYPE.fullmatch(self.media_type) is None:
            raise GraphError("result media type must be a canonical lowercase MIME type")
        if not isinstance(self.relative_path, str):
            raise GraphError("result path must be a portable relative path")
        parts = self.relative_path.split("/")
        if (
            not self.relative_path
            or "\0" in self.relative_path
            or "\\" in self.relative_path
            or ":" in self.relative_path
            or self.relative_path.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise GraphError("result path must be a portable relative path")


@dataclass(frozen=True, slots=True)
class Node:
    """One cacheable command whose inputs name its direct dependency nodes."""

    name: str
    uid: str
    pool: str
    command: Command
    inputs: tuple[str, ...] = ()
    results: tuple[Result, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SLUG.fullmatch(self.name) is None:
            raise GraphError(
                "node name must be a lowercase readable slug of at most "
                f"{NODE_SLUG_MAX_LENGTH} ASCII characters"
            )
        if not isinstance(self.uid, str) or _MD5_UID.fullmatch(self.uid) is None:
            raise GraphError("node uid must be a 32-character lowercase MD5")
        _text(self.pool, "node pool")
        if not isinstance(self.command, Command):
            raise GraphError("node command must be a Command")
        inputs = tuple(self.inputs)
        for dependency in inputs:
            _text(dependency, "dependency name")
        if len(inputs) != len(set(inputs)):
            raise GraphError(f"node {self.name!r} contains duplicate dependencies")
        results = tuple(self.results)
        if any(not isinstance(result, Result) for result in results):
            raise GraphError("node results must be Result instances")
        result_ids = tuple(result.id for result in results)
        if len(result_ids) != len(set(result_ids)):
            raise GraphError(f"node {self.name!r} contains duplicate result ids")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "results", results)


@dataclass(frozen=True, slots=True)
class Graph:
    """Validated DAG with readable node names and stable logical result ids."""

    nodes: tuple[Node, ...]
    targets: tuple[str, ...]
    pools: Mapping[str, int]
    _by_name: Mapping[str, Node] = field(init=False, repr=False, compare=False)
    _by_result: Mapping[str, tuple[Node, Result]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        nodes = tuple(self.nodes)
        targets = tuple(self.targets)
        pools = dict(self.pools)
        if not nodes:
            raise GraphError("graph must contain at least one node")
        if not targets:
            raise GraphError("graph must contain at least one target")

        by_name: dict[str, Node] = {}
        by_result: dict[str, tuple[Node, Result]] = {}
        for current in nodes:
            if not isinstance(current, Node):
                raise GraphError("graph nodes must be Node instances")
            if current.name in by_name:
                raise GraphError(f"duplicate node name: {current.name}")
            by_name[current.name] = current
            for result in current.results:
                if result.id in by_result:
                    raise GraphError(f"duplicate result id: {result.id}")
                by_result[result.id] = (current, result)

        for name, capacity in pools.items():
            _text(name, "pool name")
            if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
                raise GraphError(f"invalid pool capacity for {name!r}: {capacity!r}")
        for current in nodes:
            if current.pool not in pools:
                raise GraphError(f"node {current.name!r} uses unknown pool {current.pool!r}")
            for dependency in current.inputs:
                if dependency not in by_name:
                    raise GraphError(
                        f"node {current.name!r} has unknown dependency {dependency!r}"
                    )

        for target in targets:
            _text(target, "target node name")
            if target not in by_name:
                raise GraphError(f"unknown target node {target!r}")
        if len(targets) != len(set(targets)):
            raise GraphError("graph contains duplicate targets")

        dependencies = {
            name: by_name[name].inputs for name in sorted(by_name)
        }
        try:
            TopologicalSorter(dependencies).prepare()
        except CycleError as error:
            cycle = error.args[1] if len(error.args) > 1 else ()
            raise GraphError("dependency cycle: " + " -> ".join(cycle)) from error

        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "pools", MappingProxyType(pools))
        object.__setattr__(self, "_by_name", MappingProxyType(by_name))
        object.__setattr__(self, "_by_result", MappingProxyType(by_result))

    def node(self, name: str) -> Node:
        try:
            return self._by_name[name]
        except KeyError as error:
            raise GraphError(f"unknown node {name!r}") from error

    def dependencies_of(self, name: str) -> tuple[Node, ...]:
        return tuple(self.node(dependency) for dependency in self.node(name).inputs)

    def result(self, result_id: str) -> tuple[Node, Result]:
        try:
            return self._by_result[result_id]
        except KeyError as error:
            raise GraphError(f"unknown result {result_id!r}") from error


def merge_graphs(*graphs: Graph) -> Graph:
    """Union independent graphs while deduplicating byte-identical shared nodes."""

    if not graphs:
        raise GraphError("graph merge requires at least one graph")
    nodes: dict[str, Node] = {}
    targets: dict[str, None] = {}
    pools: dict[str, int] = {}
    for graph in graphs:
        for current in graph.nodes:
            previous = nodes.get(current.name)
            if previous is not None and previous != current:
                raise GraphError(f"conflicting node definition: {current.name}")
            nodes.setdefault(current.name, current)
        targets.update((name, None) for name in graph.targets)
        for name, capacity in graph.pools.items():
            if name in pools and pools[name] != capacity:
                raise GraphError(f"conflicting pool capacity for {name}")
            pools[name] = capacity
    return Graph(tuple(nodes.values()), tuple(targets), pools)

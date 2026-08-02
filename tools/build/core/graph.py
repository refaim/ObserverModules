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
class Node:
    """One cacheable command whose inputs name its direct dependency nodes."""

    name: str
    uid: str
    pool: str
    command: Command
    inputs: tuple[str, ...] = ()

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
        object.__setattr__(self, "inputs", inputs)


@dataclass(frozen=True, slots=True)
class Graph:
    """Validated DAG keyed only by readable node names."""

    nodes: tuple[Node, ...]
    targets: tuple[str, ...]
    pools: Mapping[str, int]
    _by_name: Mapping[str, Node] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        nodes = tuple(self.nodes)
        targets = tuple(self.targets)
        pools = dict(self.pools)
        if not nodes:
            raise GraphError("graph must contain at least one node")
        if not targets:
            raise GraphError("graph must contain at least one target")

        by_name: dict[str, Node] = {}
        for current in nodes:
            if not isinstance(current, Node):
                raise GraphError("graph nodes must be Node instances")
            if current.name in by_name:
                raise GraphError(f"duplicate node name: {current.name}")
            by_name[current.name] = current

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

    def node(self, name: str) -> Node:
        try:
            return self._by_name[name]
        except KeyError as error:
            raise GraphError(f"unknown node {name!r}") from error

    def dependencies_of(self, name: str) -> tuple[Node, ...]:
        return tuple(self.node(dependency) for dependency in self.node(name).inputs)


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

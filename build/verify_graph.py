#!/usr/bin/env python3
"""Compose and optionally execute the typed x64 verification micro-DAG."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Mapping

from build import dynamic_graph, graph_driver, native_graph


ANALYSIS_SCOPE = "analysis"
FULL_SCOPE = "full"
SCOPES = (ANALYSIS_SCOPE, FULL_SCOPE)

RESOURCE_CAPACITIES = {
    "archive-io": 2,
    "binskim": 1,
    "clang-tidy": 4,
    "cpu": 12,
    "dumpbin": 2,
    "fuzz-runtime": 4,
    "fuzz-writer-x64-pickle": 1,
    "fuzz-writer-x64-renpy": 1,
    "fuzz-writer-x64-rpgmaker": 1,
    "fuzz-writer-x64-zanzarah": 1,
    "memory-gib": 16,
    "msvc-analysis": 4,
    "native-msbuild": 2,
    "package-init": 1,
    "runtime-smoke": 2,
    "sarif": 4,
    "test-run": 2,
    "umdh-capture": 1,
    "umdh-diff": 2,
    "umdh-session": 1,
}

FULL_TARGETS = (
    "analysis-gate",
    "fuzz-timed-x64-pickle",
    "fuzz-timed-x64-renpy",
    "fuzz-timed-x64-rpgmaker",
    "fuzz-timed-x64-zanzarah",
    "leaks-aggregate",
    "package-evidence",
    "run-tests-asan",
    "run-tests-coverage",
    "run-tests-debug",
    "run-tests-release",
    "run-tests-ubsan",
)


class VerifyGraphError(ValueError):
    """Raised when typed graph sections cannot be composed without ambiguity."""


@dataclass(frozen=True)
class GraphComposition:
    name: str
    scope: str
    mapping: Mapping[str, object]
    runnable: bool
    unwired_sections: tuple[str, ...]
    external_dependencies: tuple[str, ...]


def _leaf_argv(command: str, *arguments: str) -> list[str]:
    return [
        "pwsh",
        "-NoLogo",
        "-NoProfile",
        "-File",
        "build.ps1",
        command,
        *arguments,
    ]


def _root_nodes() -> list[dict[str, object]]:
    common_inputs = [
        "build.ps1",
        "build/**/*.ps1",
        "build/**/*.props",
        "build/**/*.proj",
        "build/**/*.vcxproj",
        "vcpkg.json",
    ]
    return [
        {
            "name": "doctor",
            "deps": [],
            "run_after": [],
            "resources": {"cpu": 1},
            "argv": _leaf_argv("doctor"),
            "inputs": common_inputs,
            "writes": [],
            "outputs": [],
            "fingerprint": ["contract=verify-root-v1", "node=doctor"],
            "cacheable": False,
        },
        {
            "name": "restore",
            "deps": ["doctor"],
            "run_after": [],
            "resources": {"cpu": 2, "memory-gib": 2},
            "argv": _leaf_argv("restore", "-Arch", "x64", "-RestoreFlavor", "all"),
            "inputs": [
                *common_inputs,
                "vcpkg-configuration.json",
                "build/vcpkg/triplets/*.cmake",
            ],
            "writes": [".artifacts/vcpkg_installed"],
            "outputs": [],
            "fingerprint": ["contract=verify-root-v1", "node=restore", "flavors=default+asan"],
            "cacheable": False,
        },
        {
            "name": "source-checks",
            "deps": ["restore"],
            "run_after": [],
            "resources": {"cpu": 4, "memory-gib": 4},
            "argv": _leaf_argv(
                "source-checks", "-Arch", "x64", "-SkipDependencyRestore"
            ),
            "inputs": [".clang-format", ".clang-tidy", *common_inputs, "src/**/*.cpp", "src/**/*.h"],
            "writes": [
                ".artifacts/reports/cppcheck",
                ".artifacts/reports/psscriptanalyzer",
            ],
            "outputs": [],
            "fingerprint": ["contract=verify-root-v1", "node=source-checks"],
            "cacheable": False,
        },
    ]


def _validate_unique_nodes(nodes: list[dict[str, object]]) -> set[str]:
    names = [node.get("name") for node in nodes]
    if any(not isinstance(name, str) or not name for name in names):
        raise VerifyGraphError("every composed node requires a non-empty string name")
    unique = set(names)
    if len(unique) != len(names):
        duplicates = sorted(name for name in unique if names.count(name) > 1)
        raise VerifyGraphError(f"composed graph contains duplicate nodes: {duplicates}")
    return unique


def _validate_resources(nodes: list[dict[str, object]]) -> None:
    for node in nodes:
        claims = node.get("resources")
        if not isinstance(claims, dict) or not claims:
            raise VerifyGraphError(f"node has no resource claims: {node.get('name')}")
        for resource, demand in claims.items():
            capacity = RESOURCE_CAPACITIES.get(resource)
            if capacity is None:
                raise VerifyGraphError(
                    f"undeclared resource {resource!r} in {node.get('name')}"
                )
            if isinstance(demand, bool) or not isinstance(demand, int) or not 1 <= demand <= capacity:
                raise VerifyGraphError(
                    f"invalid demand for {node.get('name')}/{resource}: {demand!r}"
                )


def compose_x64_graph(workspace: Path, *, scope: str, run_id: str = "local") -> GraphComposition:
    """Compose normalized schema-v2 records and expose only honestly runnable scopes."""

    if scope not in SCOPES:
        raise VerifyGraphError(f"unknown scope: {scope!r}")
    root = Path(workspace).resolve()
    manifest = native_graph.load_manifest(root)
    native_nodes = native_graph.expand_x64_nodes(manifest)
    nodes = [*_root_nodes(), *native_nodes]
    external_dependencies: tuple[str, ...] = ()

    if scope == FULL_SCOPE:
        dynamic = dynamic_graph.build_dynamic_topology(
            architectures=("x64",),
            host_architecture="x64",
            leak_windows=3,
            run_id=run_id,
        )
        external_dependencies = tuple(dynamic["external_nodes"])
        native_names = _validate_unique_nodes(nodes)
        missing = sorted(set(external_dependencies) - native_names)
        if missing:
            raise VerifyGraphError(
                f"dynamic graph has unsatisfied external dependencies: {missing}"
            )
        nodes.extend(dynamic["nodes"])

    names = _validate_unique_nodes(nodes)
    targets = ["analysis-gate"] if scope == ANALYSIS_SCOPE else list(FULL_TARGETS)
    unknown_targets = sorted(set(targets) - names)
    if unknown_targets:
        raise VerifyGraphError(f"scope has unknown targets: {unknown_targets}")
    _validate_resources(nodes)

    mapping: dict[str, object] = {
        "resources": dict(RESOURCE_CAPACITIES),
        "failure_policy": "continue",
        "targets": targets,
        "nodes": nodes,
    }
    # Let the production runner validate dependencies, paths, cycles, and write ownership.
    graph = graph_driver.Graph.from_mapping(f"observer-x64-{scope}", mapping, root)
    order = graph._order(graph.targets)
    graph._validate_write_conflicts(order)
    runnable = scope == ANALYSIS_SCOPE
    unwired = () if runnable else ("dynamic",)
    return GraphComposition(
        name=f"observer-x64-{scope}",
        scope=scope,
        mapping=mapping,
        runnable=runnable,
        unwired_sections=unwired,
        external_dependencies=external_dependencies,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "run"):
        child = subcommands.add_parser(command)
        child.add_argument("--scope", choices=SCOPES, default=ANALYSIS_SCOPE)
        child.add_argument(
            "--workspace", type=Path, default=Path(__file__).resolve().parent.parent
        )
        child.add_argument("--run-id", default="local")
    subcommands.choices["plan"].add_argument("--json", action="store_true")
    subcommands.choices["run"].add_argument("--jobs", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        composition = compose_x64_graph(
            args.workspace, scope=args.scope, run_id=args.run_id
        )
        graph = graph_driver.Graph.from_mapping(
            composition.name, composition.mapping, args.workspace
        )
        if args.command == "plan":
            plan = graph.plan()
            if args.json:
                print(graph_driver.plan_as_json(plan), end="")
            else:
                status = "runnable" if composition.runnable else "plan-only"
                print(f"scope={composition.scope} status={status} nodes={len(plan)}")
                for index, item in enumerate(plan, 1):
                    print(f"{index:03d} {item.node.name}")
            return 0
        if not composition.runnable:
            sections = ", ".join(composition.unwired_sections)
            raise VerifyGraphError(
                f"scope {composition.scope!r} is plan-only; unwired sections: {sections}"
            )
        base = graph.workspace / ".artifacts" / "graph" / graph.name
        summary = graph_driver.GraphRunner(
            graph, base / "logs", base / "state", max_workers=args.jobs
        ).run()
        for item in summary.plan:
            result = summary.results[item.node.name]
            print(
                f"{result.status:9} {result.name} {result.duration_seconds:.3f}s "
                f"log={result.log_path}"
            )
        print(f"total {summary.duration_seconds:.3f}s")
        return 0 if summary.succeeded else 1
    except (VerifyGraphError, graph_driver.GraphValidationError) as error:
        print(f"verify graph error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

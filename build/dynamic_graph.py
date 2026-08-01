"""Deterministic schema-v2 records for fine-grained dynamic verification leaves."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath
import re

from build.native_graph import (
    FUZZ_TARGET_NAMES as FUZZ_TARGETS,
    build_project_node_name,
    fuzz_build_node_name,
)


ARCHITECTURES = ("x86", "x64", "arm64")
MODULES = ("renpy", "rpgmaker", "zanzarah")
LEAK_MODES = ("operations", "lifecycle")
LEAK_SCENARIOS = (
    "small-success",
    "malformed",
    "cancellation",
    "read-failure",
    "write-failure",
    "large-metadata",
    "sparse-metadata",
)

_NODE_FIELDS = {
    "name",
    "deps",
    "run_after",
    "argv",
    "resources",
    "inputs",
    "writes",
    "outputs",
    "fingerprint",
    "cacheable",
}
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DynamicTopologyError(ValueError):
    """Raised when normalized dynamic topology is unsafe or inconsistent."""


def _claim(name: str, units: int = 1, _scope: str = "task") -> tuple[str, int]:
    return name, units


def _node(
    name: str,
    kind: str,
    *,
    deps: Iterable[str],
    argv: Sequence[str],
    resources: Iterable[tuple[str, int]],
    writes: Iterable[str],
    outputs: Iterable[str],
    cacheable: bool,
    inputs: Iterable[str] = ("build/lib/dynamic-graph-leaves.ps1",),
) -> dict[str, object]:
    resource_items = list(resources)
    if len({name for name, _ in resource_items}) != len(resource_items):
        raise DynamicTopologyError(f"duplicate resource claim in {name}")
    return {
        "name": name,
        "deps": sorted(deps),
        "run_after": [],
        "argv": list(argv),
        "resources": dict(sorted(resource_items)),
        "inputs": sorted(inputs),
        "writes": sorted(writes),
        "outputs": sorted(outputs),
        "fingerprint": ["contract=dynamic-microdag-v1", f"kind={kind}", f"node={name}"],
        "cacheable": cacheable,
    }


def _leaf_argv(leaf: str, *arguments: str) -> list[str]:
    return [
        "pwsh",
        "-NoLogo",
        "-NoProfile",
        "-File",
        "build/lib/dynamic-graph-leaves.ps1",
        "-Leaf",
        leaf,
        *arguments,
    ]


def _release_build_dependency(architecture: str, project: str) -> str:
    if architecture == "x64":
        return build_project_node_name("Release", project)
    return f"release-{architecture}-{project}"


def _add_fuzz(nodes: list[dict[str, object]], run_id: str) -> None:
    for target in FUZZ_TARGETS:
        persistent_base = f".artifacts/fuzz/x64/{target}"
        run_base = f".artifacts/fuzz-runs/{run_id}/x64/{target}"
        build_name = fuzz_build_node_name(target)
        replay_name = f"fuzz-seed-replay-x64-{target}"
        timed_name = f"fuzz-timed-x64-{target}"
        nodes.append(
            _node(
                replay_name,
                "fuzz-seed-replay",
                deps=(build_name,),
                argv=_leaf_argv(
                    "fuzz",
                    "-Phase",
                    "seed-replay",
                    "-Architecture",
                    "x64",
                    "-TargetName",
                    target,
                    "-RunId",
                    run_id,
                ),
                resources=(_claim("cpu", 1), _claim("memory-gib", 2), _claim(f"fuzz-writer-x64-{target}")),
                writes=(
                    f"{run_base}/seed-replay",
                    f"{run_base}/seed-replay/result.json",
                    f"{run_base}/seed-replay/artifacts",
                ),
                outputs=(f"{run_base}/seed-replay/result.json",),
                cacheable=False,
            )
        )
        nodes.append(
            _node(
                timed_name,
                "fuzz-timed",
                deps=(replay_name,),
                argv=_leaf_argv(
                    "fuzz",
                    "-Phase",
                    "timed",
                    "-Architecture",
                    "x64",
                    "-TargetName",
                    target,
                    "-RunId",
                    run_id,
                ),
                resources=(
                    _claim("cpu", 1),
                    _claim("memory-gib", 2),
                    _claim("fuzz-runtime", 1),
                    _claim(f"fuzz-writer-x64-{target}"),
                ),
                writes=(
                    f"{persistent_base}/corpus",
                    f"{run_base}/timed",
                    f"{run_base}/timed/result.json",
                    f"{run_base}/timed/artifacts",
                ),
                outputs=(f"{run_base}/timed/result.json",),
                cacheable=False,
            )
        )


def _add_leaks(nodes: list[dict[str, object]], leak_windows: int) -> None:
    audit_deps = tuple(f"audit-x64-{module}" for module in MODULES)
    nodes.append(
        _node(
            "leak-setup",
            "leak-setup",
            deps=(*audit_deps, _release_build_dependency("x64", "leak-probe")),
            argv=_leaf_argv("leak", "-Phase", "setup", "-Architecture", "x64"),
            resources=(_claim("cpu", 1),),
            writes=(".artifacts/reports/leaks/x64/release-binaries.json",),
            outputs=(".artifacts/reports/leaks/x64/release-binaries.json",),
            cacheable=False,
        )
    )
    judges: list[str] = []
    for mode in LEAK_MODES:
        for scenario in LEAK_SCENARIOS:
            stem = f"leak-{mode}-{scenario}"
            preflight_name = f"{stem}-preflight"
            capture_name = f"{stem}-capture"
            capture_root = f".artifacts/reports/leaks/x64/captures/{mode}/{scenario}"
            preflight_output = f".artifacts/reports/leaks/x64/preflight/{mode}/{scenario}.json"
            capture_output = f"{capture_root}/capture.json"
            common = (
                "-Mode",
                mode,
                "-Scenario",
                scenario,
                "-Architecture",
                "x64",
            )
            nodes.append(
                _node(
                    preflight_name,
                    "leak-preflight",
                    deps=("leak-setup",),
                    argv=_leaf_argv("leak", "-Phase", "preflight", *common),
                    resources=(_claim("cpu", 1), _claim("memory-gib", 1)),
                    writes=(preflight_output,),
                    outputs=(preflight_output,),
                    cacheable=False,
                )
            )
            nodes.append(
                _node(
                    capture_name,
                    "leak-capture",
                    deps=(preflight_name,),
                    argv=_leaf_argv("leak", "-Phase", "capture", *common),
                    resources=(
                        _claim("cpu", 1),
                        _claim("memory-gib", 1),
                        _claim("umdh-session", 1),
                        _claim("umdh-capture", 1, "invocation"),
                    ),
                    writes=(capture_root, capture_output),
                    outputs=(capture_output,),
                    cacheable=False,
                )
            )
            diff_names: list[str] = []
            for index in range(1, leak_windows):
                label = f"window-{index}"
                diff_name = f"{stem}-diff-{label}"
                output = f".artifacts/reports/leaks/x64/diffs/{mode}/{scenario}/{label}.json"
                nodes.append(
                    _node(
                        diff_name,
                        "leak-diff",
                        deps=(capture_name,),
                        argv=_leaf_argv("leak", "-Phase", "diff", *common, "-Label", label),
                        resources=(_claim("cpu", 1), _claim("umdh-diff", 1)),
                        writes=(output,),
                        outputs=(output,),
                        cacheable=False,
                    )
                )
                diff_names.append(diff_name)
            overall_name = f"{stem}-diff-overall"
            overall_output = f".artifacts/reports/leaks/x64/diffs/{mode}/{scenario}/overall.json"
            nodes.append(
                _node(
                    overall_name,
                    "leak-diff",
                    deps=(capture_name,),
                    argv=_leaf_argv("leak", "-Phase", "diff", *common, "-Label", "overall"),
                    resources=(_claim("cpu", 1), _claim("umdh-diff", 1)),
                    writes=(overall_output,),
                    outputs=(overall_output,),
                    cacheable=False,
                )
            )
            diff_names.append(overall_name)
            judge_name = f"{stem}-judge"
            judge_output = f".artifacts/reports/leaks/x64/summaries/{mode}/{scenario}.json"
            nodes.append(
                _node(
                    judge_name,
                    "leak-judge",
                    deps=diff_names,
                    argv=_leaf_argv("leak", "-Phase", "judge", *common),
                    resources=(_claim("cpu", 1),),
                    writes=(judge_output,),
                    outputs=(judge_output,),
                    cacheable=False,
                )
            )
            judges.append(judge_name)
    nodes.append(
        _node(
            "leaks-aggregate",
            "leaks-aggregate",
            deps=judges,
            argv=_leaf_argv("leak", "-Phase", "aggregate", "-Architecture", "x64"),
            resources=(_claim("cpu", 1),),
            writes=(".artifacts/reports/leaks/x64/summary.json",),
            outputs=(".artifacts/reports/leaks/x64/summary.json",),
            cacheable=False,
        )
    )


def _add_audits(nodes: list[dict[str, object]], architectures: Sequence[str]) -> None:
    for architecture in architectures:
        for module in MODULES:
            stem = f"audit-{architecture}-{module}"
            release = _release_build_dependency(architecture, module)
            dumpbin = f"{stem}-dumpbin"
            binskim = f"{stem}-binskim"
            dumpbin_output = f".artifacts/audit/{architecture}/{module}/dumpbin.json"
            binskim_output = f".artifacts/audit/{architecture}/{module}/binskim.sarif"
            summary_output = f".artifacts/audit/{architecture}/{module}/summary.json"
            nodes.append(
                _node(
                    dumpbin,
                    "audit-dumpbin",
                    deps=(release,),
                    argv=_leaf_argv(
                        "audit", "-Tool", "dumpbin", "-Architecture", architecture, "-ModuleName", module
                    ),
                    resources=(_claim("cpu", 1), _claim("dumpbin", 1)),
                    writes=(dumpbin_output,),
                    outputs=(dumpbin_output,),
                    cacheable=False,
                )
            )
            nodes.append(
                _node(
                    binskim,
                    "audit-binskim",
                    deps=(release,),
                    argv=_leaf_argv(
                        "audit", "-Tool", "binskim", "-Architecture", architecture, "-ModuleName", module
                    ),
                    resources=(_claim("cpu", 1), _claim("memory-gib", 1), _claim("binskim", 1)),
                    writes=(binskim_output,),
                    outputs=(binskim_output,),
                    cacheable=False,
                )
            )
            nodes.append(
                _node(
                    stem,
                    "audit-module",
                    deps=(dumpbin, binskim),
                    argv=_leaf_argv(
                        "audit", "-Tool", "aggregate", "-Architecture", architecture, "-ModuleName", module
                    ),
                    resources=(_claim("cpu", 1),),
                    writes=(summary_output,),
                    outputs=(summary_output,),
                    cacheable=False,
                )
            )


def _add_packages(
    nodes: list[dict[str, object]], architectures: Sequence[str], host_architecture: str, run_id: str
) -> None:
    root = f".artifacts/package-runs/{run_id}"
    init_output = f"{root}/init.json"
    nodes.append(
        _node(
            "package-init",
            "package-init",
            deps=(),
            argv=_leaf_argv("package", "-Phase", "init", "-RunId", run_id),
            resources=(_claim("cpu", 1), _claim("package-init", 1)),
            writes=(init_output,),
            outputs=(init_output,),
            cacheable=False,
        )
    )
    evidence_inputs: list[str] = []
    runnable = {"x86", "x64"} if host_architecture == "x64" else {host_architecture}
    for architecture in architectures:
        for module in MODULES:
            stem = f"package-{architecture}-{module}"
            create_name = f"{stem}-create"
            content_name = f"{stem}-content"
            runtime_name = f"{stem}-runtime"
            archive = f"{root}/archives/{architecture}/{module}.zip"
            content_fragment = f"{root}/evidence/content/{architecture}/{module}.json"
            runtime_fragment = f"{root}/evidence/runtime/{architecture}/{module}.json"
            nodes.append(
                _node(
                    create_name,
                    "package-create",
                    deps=("package-init", f"audit-{architecture}-{module}"),
                    argv=_leaf_argv(
                        "package",
                        "-Phase",
                        "create-module",
                        "-RunId",
                        run_id,
                        "-Architecture",
                        architecture,
                        "-ModuleName",
                        module,
                    ),
                    resources=(_claim("cpu", 1), _claim("archive-io", 1)),
                    writes=(f"{root}/stage/{architecture}/{module}", archive),
                    outputs=(archive,),
                    cacheable=False,
                )
            )
            nodes.append(
                _node(
                    content_name,
                    "package-content-smoke",
                    deps=(create_name,),
                    argv=_leaf_argv(
                        "package",
                        "-Phase",
                        "content-module",
                        "-RunId",
                        run_id,
                        "-Architecture",
                        architecture,
                        "-ModuleName",
                        module,
                    ),
                    resources=(_claim("cpu", 1), _claim("archive-io", 1)),
                    writes=(f"{root}/extracted/{architecture}/{module}", content_fragment),
                    outputs=(content_fragment,),
                    cacheable=False,
                )
            )
            runtime_kind = "package-runtime-smoke" if architecture in runnable else "package-runtime-deferred"
            runtime_deps = [content_name]
            if runtime_kind == "package-runtime-smoke":
                runtime_deps.append(_release_build_dependency(architecture, "tests"))
            nodes.append(
                _node(
                    runtime_name,
                    runtime_kind,
                    deps=runtime_deps,
                    argv=_leaf_argv(
                        "package",
                        "-Phase",
                        "runtime-module" if runtime_kind == "package-runtime-smoke" else "defer-runtime",
                        "-RunId",
                        run_id,
                        "-Architecture",
                        architecture,
                        "-ModuleName",
                        module,
                    ),
                    resources=(_claim("cpu", 1), _claim("runtime-smoke", 1)),
                    writes=(f"{root}/reports/{architecture}/{module}.xml", runtime_fragment),
                    outputs=(runtime_fragment,),
                    cacheable=False,
                )
            )
            evidence_inputs.append(runtime_name)
        symbols_name = f"symbols-{architecture}-create"
        symbols_content_name = f"symbols-{architecture}-content"
        symbols_archive = f"{root}/archives/{architecture}/symbols.zip"
        symbols_fragment = f"{root}/evidence/symbols/{architecture}.json"
        nodes.append(
            _node(
                symbols_name,
                "symbols-create",
                deps=(
                    "package-init",
                    *(_release_build_dependency(architecture, module) for module in MODULES),
                ),
                argv=_leaf_argv(
                    "package", "-Phase", "create-symbols", "-RunId", run_id, "-Architecture", architecture
                ),
                resources=(_claim("cpu", 1), _claim("archive-io", 1)),
                writes=(f"{root}/stage/{architecture}/symbols", symbols_archive),
                outputs=(symbols_archive,),
                cacheable=False,
            )
        )
        nodes.append(
            _node(
                symbols_content_name,
                "symbols-content-smoke",
                deps=(symbols_name,),
                argv=_leaf_argv(
                    "package", "-Phase", "content-symbols", "-RunId", run_id, "-Architecture", architecture
                ),
                resources=(_claim("cpu", 1), _claim("archive-io", 1)),
                writes=(f"{root}/extracted/{architecture}/symbols", symbols_fragment),
                outputs=(symbols_fragment,),
                cacheable=False,
            )
        )
        evidence_inputs.append(symbols_content_name)
    evidence = f"{root}/package-smoke-evidence.json"
    nodes.append(
        _node(
            "package-evidence",
            "package-evidence",
            deps=evidence_inputs,
            argv=_leaf_argv("package", "-Phase", "aggregate", "-RunId", run_id),
            resources=(_claim("cpu", 1),),
            writes=(evidence,),
            outputs=(evidence,),
            cacheable=False,
        )
    )


def build_dynamic_topology(
    *,
    architectures: Sequence[str] = ARCHITECTURES,
    host_architecture: str = "x64",
    leak_windows: int = 3,
    run_id: str,
) -> dict[str, object]:
    """Build deterministic records; execution is deliberately owned by leaf adapters."""

    selected_architectures = tuple(architectures)
    if (
        not selected_architectures
        or len(set(selected_architectures)) != len(selected_architectures)
        or any(architecture not in ARCHITECTURES for architecture in selected_architectures)
    ):
        raise DynamicTopologyError("architectures must be a non-empty unique subset of x86, x64, and arm64")
    if host_architecture not in ARCHITECTURES:
        raise DynamicTopologyError("unsupported host architecture")
    if isinstance(leak_windows, bool) or not 3 <= leak_windows <= 10:
        raise DynamicTopologyError("leak_windows must be from 3 to 10")
    if not _RUN_ID.fullmatch(run_id):
        raise DynamicTopologyError("run_id must be a safe 1-128 character path component")

    nodes: list[dict[str, object]] = []
    _add_fuzz(nodes, run_id)
    _add_audits(nodes, selected_architectures)
    _add_leaks(nodes, leak_windows)
    _add_packages(nodes, selected_architectures, host_architecture, run_id)
    external_nodes = {
        _release_build_dependency("x64", "leak-probe"),
        *(fuzz_build_node_name(target) for target in FUZZ_TARGETS),
        *(
            _release_build_dependency(architecture, module)
            for architecture in selected_architectures
            for module in MODULES
        ),
        *(
            _release_build_dependency(architecture, "tests")
            for architecture in selected_architectures
            if architecture in ({"x86", "x64"} if host_architecture == "x64" else {host_architecture})
        ),
    }
    result = {
        "schema": 2,
        "external_nodes": sorted(external_nodes),
        "nodes": sorted(nodes, key=lambda node: str(node["name"])),
    }
    validate_dynamic_topology(result)
    return result


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _paths_overlap(first: str, second: str) -> bool:
    first_parts = tuple(part.casefold() for part in PurePosixPath(first).parts)
    second_parts = tuple(part.casefold() for part in PurePosixPath(second).parts)
    common = min(len(first_parts), len(second_parts))
    return first_parts[:common] == second_parts[:common]


def validate_dynamic_topology(topology: dict[str, object]) -> None:
    """Reject unsafe paths, duplicate ownership, unknown dependencies, and cycles."""

    if topology.get("schema") != 2:
        raise DynamicTopologyError("dynamic topology schema must be 2")
    nodes = topology.get("nodes")
    external_nodes = topology.get("external_nodes")
    if not isinstance(nodes, list) or not isinstance(external_nodes, list):
        raise DynamicTopologyError("nodes and external_nodes must be lists")
    if (
        not all(isinstance(name, str) and name for name in external_nodes)
        or len(set(external_nodes)) != len(external_nodes)
    ):
        raise DynamicTopologyError("external_nodes must contain unique non-empty names")
    names: set[str] = set()
    write_owners: dict[str, str] = {}
    output_owners: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict) or set(node) != _NODE_FIELDS:
            raise DynamicTopologyError("every node must use the normalized schema-v2 fields")
        name = node["name"]
        if not isinstance(name, str) or not name or name in names:
            raise DynamicTopologyError(f"duplicate or invalid node name: {name!r}")
        names.add(name)
        if not isinstance(node["cacheable"], bool):
            raise DynamicTopologyError(f"cacheable must be boolean: {name}")
        resources = node["resources"]
        if not isinstance(resources, dict) or not resources:
            raise DynamicTopologyError(f"node has no resource claims: {name}")
        for resource_name, units in resources.items():
            if (
                not isinstance(resource_name, str)
                or not resource_name
                or isinstance(units, bool)
                or not isinstance(units, int)
                or units < 1
            ):
                raise DynamicTopologyError(f"invalid resource claim: {name}")
        for field, owners in (("writes", write_owners), ("outputs", output_owners)):
            values = node[field]
            if not isinstance(values, list):
                raise DynamicTopologyError(f"{field} must be a list: {name}")
            for value in values:
                if not _safe_relative_path(value):
                    raise DynamicTopologyError(f"unsafe {field} path in {name}: {value!r}")
                if value in owners:
                    raise DynamicTopologyError(f"{field} path has multiple owners: {value}")
                if field == "writes":
                    for owned_path, owner in write_owners.items():
                        if owner != name and _paths_overlap(owned_path, value):
                            raise DynamicTopologyError(
                                f"write paths overlap between {owner} and {name}: {owned_path}, {value}"
                            )
                owners[value] = name
        if any(
            not any(_paths_overlap(write, output) for write in node["writes"])
            for output in node["outputs"]
        ):
            raise DynamicTopologyError(f"every output must be below a declared write root: {name}")
    external_names = set(external_nodes)
    if names & external_names:
        raise DynamicTopologyError("external_nodes must not collide with generated node names")
    known = names | external_names
    children: dict[str, list[str]] = {name: [] for name in names}
    indegree = {name: 0 for name in names}
    for node in nodes:
        name = str(node["name"])
        deps = node["deps"]
        run_after = node["run_after"]
        argv = node["argv"]
        inputs = node["inputs"]
        fingerprint = node["fingerprint"]
        if (
            not isinstance(deps, list)
            or not isinstance(run_after, list)
            or set(deps) & set(run_after)
            or not isinstance(inputs, list)
            or not isinstance(fingerprint, list)
            or not isinstance(argv, list)
            or not argv
            or not all(
            isinstance(argument, str) and argument for argument in argv
            )
            or not all(isinstance(value, str) for value in (*inputs, *fingerprint))
        ):
            raise DynamicTopologyError(f"invalid deps or argv: {name}")
        for dependency in (*deps, *run_after):
            if dependency not in known:
                raise DynamicTopologyError(f"unknown dependency {dependency!r} in {name}")
            if dependency in names:
                children[dependency].append(name)
                indegree[name] += 1
    ready = sorted(name for name, count in indegree.items() if count == 0)
    visited = 0
    while ready:
        current = ready.pop(0)
        visited += 1
        for child in sorted(children[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if visited != len(names):
        raise DynamicTopologyError("dynamic topology contains a dependency cycle")

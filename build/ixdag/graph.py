"""A compact, typed, content-addressed Observer micro-DAG.

The descriptor/signature split follows the useful core of pg83/ix's MIT-licensed
``core/sign.py`` and ``core/gg.py``.  Observer paths, Windows argv and validation
are intentionally new and much narrower than IX's package/realm implementation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re


_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_GLOB = re.compile(r"[*?\[\]]")
Renderer = Callable[[Mapping[str, object]], "Node"]


class GraphError(ValueError):
    """Raised when a descriptor graph is ambiguous or unsafe."""


def _relative_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if not value or path.is_absolute() or ".." in path.parts:
        raise GraphError(f"path must be repository-relative: {value!r}")
    if _GLOB.search(value):
        raise GraphError(f"exact paths cannot contain glob syntax: {value!r}")
    return path.as_posix()


def _unique(values: Sequence[str], what: str) -> tuple[str, ...]:
    result = tuple(values)
    if len(result) != len(set(result)):
        raise GraphError(f"duplicate {what}")
    return result


@dataclass(frozen=True)
class TranslationUnit:
    project: str
    source: str
    slug: str

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.project) or not _NAME.fullmatch(self.slug):
            raise GraphError("translation-unit project and slug must be safe names")
        object.__setattr__(self, "source", _relative_path(self.source))


@dataclass(frozen=True)
class Project:
    name: str
    project_file: str
    configurations: tuple[str, ...]
    translation_units: tuple[TranslationUnit, ...]

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise GraphError(f"unsafe project name: {self.name!r}")
        object.__setattr__(self, "project_file", _relative_path(self.project_file))
        object.__setattr__(
            self, "configurations", _unique(self.configurations, "project configuration")
        )
        if not self.configurations or not self.translation_units:
            raise GraphError(f"project {self.name!r} has an empty matrix axis")
        if any(unit.project != self.name for unit in self.translation_units):
            raise GraphError(f"translation unit belongs to another project: {self.name}")
        _unique(tuple(unit.slug for unit in self.translation_units), "translation-unit slug")


@dataclass(frozen=True)
class ObserverMatrix:
    shared_inputs: tuple[str, ...]
    projects: tuple[Project, ...]
    test_configurations: tuple[str, ...]
    fuzz_targets: tuple[str, ...]
    fuzz_seed_inputs: Mapping[str, tuple[str, ...]]
    leak_modes: tuple[str, ...]
    leak_scenarios: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "shared_inputs",
            _unique(tuple(_relative_path(path) for path in self.shared_inputs), "shared input"),
        )
        _unique(tuple(project.name for project in self.projects), "project name")
        for axis_name, values in (
            ("test configuration", self.test_configurations),
            ("fuzz target", self.fuzz_targets),
            ("leak mode", self.leak_modes),
            ("leak scenario", self.leak_scenarios),
        ):
            if not values or any(not _NAME.fullmatch(value.lower()) for value in values):
                raise GraphError(f"invalid or empty {axis_name} axis")
            _unique(values, axis_name)
        if set(self.fuzz_seed_inputs) != set(self.fuzz_targets):
            raise GraphError("fuzz seed inputs must exactly cover the fuzz-target axis")
        normalized_seeds = {
            target: _unique(
                tuple(_relative_path(path) for path in self.fuzz_seed_inputs[target]),
                f"{target} fuzz seed",
            )
            for target in self.fuzz_targets
        }
        if any(not paths for paths in normalized_seeds.values()):
            raise GraphError("every fuzz target needs at least one exact seed input")
        object.__setattr__(self, "fuzz_seed_inputs", normalized_seeds)


@dataclass(frozen=True)
class Node:
    name: str
    deps: tuple[str, ...]
    pool: str
    argv: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    cacheable: bool = True

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise GraphError(f"unsafe node name: {self.name!r}")
        if not _NAME.fullmatch(self.pool):
            raise GraphError(f"unsafe pool name: {self.pool!r}")
        object.__setattr__(self, "deps", tuple(sorted(_unique(self.deps, "dependency"))))
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(
            self, "inputs", tuple(sorted(_unique(tuple(_relative_path(p) for p in self.inputs), "input")))
        )
        object.__setattr__(
            self,
            "outputs",
            tuple(sorted(_unique(tuple(_relative_path(p) for p in self.outputs), "output"))),
        )
        if not self.argv or not self.outputs:
            raise GraphError(f"node {self.name!r} needs argv and at least one output")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "Node":
        return cls(
            name=str(value["name"]),
            deps=tuple(str(item) for item in value["deps"]),  # type: ignore[union-attr]
            pool=str(value["pool"]),
            argv=tuple(str(item) for item in value["argv"]),  # type: ignore[union-attr]
            inputs=tuple(str(item) for item in value["inputs"]),  # type: ignore[union-attr]
            outputs=tuple(str(item) for item in value["outputs"]),  # type: ignore[union-attr]
            cacheable=bool(value.get("cacheable", True)),
        )

    def mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "deps": list(self.deps),
            "pool": self.pool,
            "argv": list(self.argv),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "cacheable": self.cacheable,
        }


def direct_renderer(descriptor: Mapping[str, object]) -> Node:
    """Test/bootstrap renderer; production uses the equivalent Jinja descriptor."""

    return Node.from_mapping(descriptor)


def jinja_renderer(descriptor: Mapping[str, object]) -> Node:
    """Render only repetitive JSON emission, keeping topology in typed Python."""

    try:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
    except ImportError as error:
        raise GraphError("Jinja2 is required to render ixdag descriptors") from error
    template_root = Path(__file__).with_name("templates")
    environment = Environment(
        loader=FileSystemLoader(template_root),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    rendered = environment.get_template("node.json.j2").render(descriptor=descriptor)
    value = json.loads(rendered)
    if not isinstance(value, dict):
        raise GraphError("node template did not emit a JSON object")
    return Node.from_mapping(value)


@dataclass(frozen=True)
class Graph:
    nodes: tuple[Node, ...]
    targets: tuple[str, ...]

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.nodes, key=lambda node: node.name))
        object.__setattr__(self, "nodes", ordered)
        names = _unique(tuple(node.name for node in ordered), "node name")
        known = set(names)
        object.__setattr__(self, "targets", tuple(sorted(_unique(self.targets, "target"))))
        if not self.targets or any(target not in known for target in self.targets):
            raise GraphError("targets must name nodes in the graph")
        owners: dict[str, str] = {}
        for node in ordered:
            for output in node.outputs:
                if output in owners:
                    raise GraphError(f"duplicate output owner: {output}")
                owners[output] = node.name
        for node in ordered:
            if any(dep not in known or dep == node.name for dep in node.deps):
                raise GraphError(f"unknown or self dependency in {node.name}")
            for input_path in node.inputs:
                owner = owners.get(input_path)
                if owner is not None and owner not in node.deps:
                    raise GraphError(
                        f"generated input {input_path!r} is not a direct dependency of {node.name}"
                    )
        self._topological_names()

    def _topological_names(self) -> tuple[str, ...]:
        by_name = {node.name: node for node in self.nodes}
        visiting: set[str] = set()
        visited: set[str] = set()
        result: list[str] = []

        def visit(name: str) -> None:
            if name in visiting:
                raise GraphError(f"dependency cycle at {name}")
            if name in visited:
                return
            visiting.add(name)
            for dependency in by_name[name].deps:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
            result.append(name)

        for node in self.nodes:
            visit(node.name)
        return tuple(result)

    def descriptors(self, workspace: Path) -> dict[str, dict[str, object]]:
        """Seal nodes with source bytes and upstream UIDs, like IX store identities."""

        root = workspace.resolve()
        by_name = {node.name: node for node in self.nodes}
        owners = {output: node.name for node in self.nodes for output in node.outputs}
        sealed: dict[str, dict[str, object]] = {}
        for name in self._topological_names():
            node = by_name[name]
            content: list[dict[str, str]] = []
            for input_path in node.inputs:
                if owner := owners.get(input_path):
                    content.append({"path": input_path, "producer": sealed[owner]["uid"]})  # type: ignore[dict-item]
                    continue
                path = root.joinpath(*PurePosixPath(input_path).parts)
                if not path.is_file():
                    raise GraphError(f"exact source input does not exist: {input_path}")
                content.append({"path": input_path, "sha256": sha256(path.read_bytes()).hexdigest()})
            descriptor = node.mapping()
            payload = {"schema": "observer-ixdag-v1", "node": descriptor, "content": content}
            uid = sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            sealed[name] = {**descriptor, "uid": uid}
        return {name: sealed[name] for name in sorted(sealed)}


def _argv(action: str, *arguments: str) -> tuple[str, ...]:
    return ("pwsh", "-NoLogo", "-NoProfile", "-File", "build.ps1", action, *arguments)


def _binary_output(configuration: str, project: str) -> str:
    extension = ".exe" if project in {"tests", "leak-probe"} or project.startswith("fuzz-") else ".so"
    return f".artifacts/bin/x64/{configuration}/{project}{extension}"


def _build_name(configuration: str, project: str) -> str:
    if configuration.lower() == "fuzz" and project.startswith("fuzz-"):
        return f"build-{project}"
    return f"build-{configuration.lower()}-{project}"


def build_observer_microdag(
    matrix: ObserverMatrix, *, renderer: Renderer = jinja_renderer
) -> Graph:
    """Expand all Observer axes into small independently schedulable leaves."""

    nodes: list[Node] = []
    outputs: dict[str, tuple[str, ...]] = {}

    def add(
        name: str,
        deps: Sequence[str],
        pool: str,
        argv: Sequence[str],
        sources: Sequence[str],
        output: str,
        *,
        cacheable: bool = True,
    ) -> str:
        dependencies = tuple(sorted(deps))
        generated = tuple(path for dependency in dependencies for path in outputs[dependency])
        descriptor: dict[str, object] = {
            "name": name,
            "deps": dependencies,
            "pool": pool,
            "argv": tuple(argv),
            "inputs": tuple(dict.fromkeys((*sources, *generated))),
            "outputs": (output,),
            "cacheable": cacheable,
        }
        node = renderer(descriptor)
        nodes.append(node)
        outputs[name] = node.outputs
        return name

    source = add(
        "source-checks",
        (),
        "checks",
        _argv("source-checks"),
        matrix.shared_inputs,
        ".artifacts/reports/source-checks.json",
        cacheable=False,
    )
    build_names: dict[tuple[str, str], str] = {}
    for project in matrix.projects:
        project_sources = (project.project_file, *(unit.source for unit in project.translation_units), *matrix.shared_inputs)
        for configuration in project.configurations:
            name = _build_name(configuration, project.name)
            build_names[(configuration.lower(), project.name)] = add(
                name,
                (source,),
                "msbuild",
                _argv(
                    "graph-leaf",
                    "-GraphLeafAction",
                    "build-project",
                    "-Arch",
                    "x64",
                    "-Config",
                    configuration,
                    "-Project",
                    project.name,
                ),
                project_sources,
                _binary_output(configuration, project.name),
            )

    test_runs: list[str] = []
    for configuration in matrix.test_configurations:
        key = configuration.lower()
        dependencies = tuple(
            build_names[(key, project.name)]
            for project in matrix.projects
            if (key, project.name) in build_names
            and project.name != "leak-probe"
            and not project.name.startswith("fuzz-")
        )
        test_runs.append(
            add(
                f"run-tests-{key}",
                dependencies,
                "tests",
                _argv("test", "-Arch", "x64", "-Config", configuration),
                (),
                f".artifacts/reports/tests/x64-{key}.xml",
                cacheable=False,
            )
        )

    backend_merges: list[str] = []
    for backend, pool in (("msvc", "msvc-analysis"), ("tidy", "clang-tidy")):
        normalizers: list[str] = []
        for project in matrix.projects:
            for unit in project.translation_units:
                stem = f"{backend}-{project.name}-{unit.slug}"
                raw = f".artifacts/analysis/{backend}/x64/{project.name}/{unit.slug}.raw"
                analyze = add(
                    f"analyze-{stem}",
                    (source,),
                    pool,
                    _argv("graph-leaf", "-GraphLeafAction", f"analyze-{backend}-unit", "-Project", project.name, "-SelectedFile", unit.source),
                    (project.project_file, unit.source, *matrix.shared_inputs),
                    raw,
                )
                normalizers.append(
                    add(
                        f"normalize-{stem}",
                        (analyze,),
                        "sarif",
                        _argv("graph-leaf", "-GraphLeafAction", f"normalize-{backend}-sarif", "-Input", raw),
                        (),
                        f".artifacts/reports/{backend}/x64/units/{project.name}-{unit.slug}.sarif",
                    )
                )
        backend_merges.append(
            add(
                f"merge-{backend}-sarif",
                normalizers,
                "sarif",
                _argv("graph-leaf", "-GraphLeafAction", "merge-sarif", "-Backend", backend),
                (),
                f".artifacts/reports/{backend}/x64/{backend}.sarif",
            )
        )
    analysis_gate = add(
        "analysis-gate",
        backend_merges,
        "sarif",
        _argv("graph-leaf", "-GraphLeafAction", "analysis-gate"),
        (),
        ".artifacts/reports/analysis/x64/gate.json",
    )

    fuzz_runs: list[str] = []
    for target in matrix.fuzz_targets:
        build = build_names[("fuzz", f"fuzz-{target}")]
        replay = add(
            f"fuzz-replay-{target}",
            (build,),
            "fuzz",
            _argv("fuzz", "-Target", target, "-Phase", "replay"),
            matrix.fuzz_seed_inputs[target],
            f".artifacts/reports/fuzz/{target}/replay.json",
            cacheable=False,
        )
        fuzz_runs.append(
            add(
                f"fuzz-timed-{target}",
                (build, replay),
                "fuzz",
                _argv("fuzz", "-Target", target, "-Phase", "timed"),
                (),
                f".artifacts/reports/fuzz/{target}/timed.json",
                cacheable=False,
            )
        )
    fuzz_gate = add(
        "fuzz-gate",
        fuzz_runs,
        "gate",
        _argv("fuzz", "-Phase", "aggregate"),
        (),
        ".artifacts/reports/fuzz/gate.json",
        cacheable=False,
    )

    leak_probe = build_names[("release", "leak-probe")]
    leak_cases = [
        add(
            f"leak-case-{mode}-{scenario}",
            (leak_probe,),
            "umdh",
            _argv("leaks", "-Mode", mode, "-Scenario", scenario),
            (),
            f".artifacts/reports/leaks/{mode}/{scenario}.json",
            cacheable=False,
        )
        for mode in matrix.leak_modes
        for scenario in matrix.leak_scenarios
    ]
    leaks_gate = add(
        "leaks-gate",
        leak_cases,
        "gate",
        _argv("leaks", "-Phase", "aggregate"),
        (),
        ".artifacts/reports/leaks/gate.json",
        cacheable=False,
    )
    verify = add(
        "verify",
        (analysis_gate, fuzz_gate, leaks_gate, *test_runs),
        "gate",
        _argv("verify", "-Phase", "aggregate"),
        (),
        ".artifacts/reports/verify/gate.json",
        cacheable=False,
    )
    return Graph(tuple(nodes), (verify,))

"""Compiler-derived, translation-unit-grained analysis graphs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from core.graph import Graph, Node, Result
from core.node import NodeFactory
from core.paths import BuildPaths
from core.quality_tools import ResolvedTool, resolve_llvm
from core.toolchain import MsvcToolchain
from graphs.common import (
    COMMON_PROJECT_INPUTS, PLATFORMS, project_path, python_action, recipe_factory,
    restore_node, tool_environment,
)


_BUILD_ROOT = Path(__file__).resolve().parents[1]
_MSBUILD_NS = "{http://schemas.microsoft.com/developer/msbuild/2003}"


@dataclass(frozen=True, slots=True)
class MsbuildProject:
    name: str
    path: Path
    inputs: tuple[str, ...]
    sources: tuple[Path, ...]


def _relative(repository: Path, path: Path) -> str:
    return path.resolve(strict=True).relative_to(repository).as_posix()


def _platform(architecture: str) -> str:
    try:
        return PLATFORMS[architecture]
    except KeyError as error:
        raise ValueError(f"unsupported architecture: {architecture}") from error


def project_inventory(
    repository: Path, names: tuple[str, ...] | None = None, *,
    include_link_inputs: bool = True,
) -> tuple[MsbuildProject, ...]:
    """Parse each selected project once into immutable signed inputs and sources."""

    root = repository.resolve(strict=True)
    paths = (tuple(sorted((root / "build/projects").glob("*.vcxproj"))) if names is None
             else tuple(root / "build/projects" / f"{name}.vcxproj" for name in names))
    projects = []
    for project in paths:
        document = ET.parse(project).getroot()
        inputs = list(COMMON_PROJECT_INPUTS) + [project.relative_to(root).as_posix()]
        if include_link_inputs:
            inputs.extend(
                project_path(root, item.text.strip(), project) for item in
                document.iter(f"{_MSBUILD_NS}ModuleDefinitionFile") if item.text
            )
        sources = tuple(
            root / project_path(root, item.get("Include", ""), project) for item in
            document.iter(f"{_MSBUILD_NS}ClCompile") if item.get("Include")
        )
        projects.append(MsbuildProject(
            project.stem, project, tuple(dict.fromkeys(inputs)), sources
        ))
    return tuple(projects)


def _projects(repository: Path) -> tuple[tuple[str, Path, Path], ...]:
    try:
        projects = project_inventory(repository, include_link_inputs=False)
    except ValueError as error:
        message = str(error).replace("unsupported project input", "unsupported ClCompile path", 1)
        raise ValueError(message) from error
    return tuple(
        (project.name, project.path, source) for project in projects
        for source in project.sources
    )


def _unit(repository: Path, source: Path) -> str:
    return _relative(repository / "src", source).removesuffix(".cpp").replace("/", ".")


def dependency_node_name(
    repository: Path, architecture: str, project_name: str, source: Path,
    qualifier: str = "",
) -> str:
    prefix = f"{architecture}-{qualifier}" if qualifier else architecture
    return f"discover-dependencies-{prefix}-{project_name}-{_unit(repository, source)}"


def _project_files(
    repository: Path, project_name: str, project: Path, source: Path,
    extra: tuple[str, ...] = (),
) -> dict[str, bytes]:
    inputs = list(COMMON_PROJECT_INPUTS) + [
        _relative(repository, project), _relative(repository, source)
    ]
    if project_name.startswith("fuzz-"):
        inputs.append("build/ObserverFuzz.props")
    inputs.extend(extra)
    return {path: (repository / path).read_bytes() for path in dict.fromkeys(inputs)}


def _compile_variables(
    toolchain: MsvcToolchain, project: Path, source: Path, restore_output: Path,
    configuration: str, platform: str,
) -> dict[str, object]:
    return {
        "pwsh": str(toolchain.pwsh), "msbuild": str(toolchain.msbuild),
        "project": str(project), "target": "ClCompile",
        "configuration": configuration, "platform": platform, "msbuild_args": [],
        "source": str(source), "vcpkg_root": str(toolchain.vcpkg_root),
        "vcpkg_installed": str(restore_output),
    }


def _manifest_index(
    repository: Path, requested: Mapping[str, str]
) -> dict[str, bytes]:
    paths, manifests = BuildPaths(repository), {}
    for name, uid in requested.items():
        cas = paths.cas(uid)
        manifest = paths.require_confined(
            cas.output / "dependencies.json", paths.cas_root
        )
        if cas.touch.is_file() and not cas.touch.stat().st_size and manifest.is_file():
            manifests[name] = manifest.read_bytes()
    return manifests


def dependency_inputs(
    repository: Path, restore_output: Path, source: Path, content: bytes
) -> dict[str, bytes]:
    """Validate one MSVC manifest and sign only project/package dependency bytes."""

    try:
        data = json.loads(content)["Data"]
        reported_source, includes = data["Source"], data["Includes"]
        if not isinstance(reported_source, str) or not isinstance(includes, list) or not all(
            isinstance(item, str) for item in includes
        ):
            raise TypeError
        reported = Path(reported_source).resolve(strict=True)
        dependencies = [Path(item) for item in includes]
        if not all(path.is_absolute() for path in dependencies):
            raise TypeError
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, OSError) as error:
        raise ValueError(f"invalid MSVC dependency manifest for {source}") from error
    expected = source.resolve(strict=True)
    if reported != expected:
        raise ValueError(f"MSVC dependency manifest source mismatch for {source}")

    files = {"compiler/dependencies.json": content}
    try:
        for candidate in dict.fromkeys((expected, *dependencies)):
            if candidate.is_relative_to(repository / "src"):
                path = candidate.resolve(strict=True)
                name = _relative(repository, path)
            elif candidate.is_relative_to(restore_output):
                path = candidate.resolve(strict=True)
                name = "vcpkg/" + path.relative_to(restore_output).as_posix()
            else:
                continue
            files[name] = path.read_bytes()
    except OSError as error:
        raise ValueError(f"invalid MSVC dependency manifest for {source}") from error
    return files


def project_build(
    repository: Path, factory: NodeFactory, discovery: Graph,
    manifests: Mapping[str, bytes], restore_output: Path, project: MsbuildProject,
    architecture: str, qualifier: str, template: str, variables: dict[str, object],
    *, identity: dict[str, str] | None = None, config: dict[str, str],
) -> Node:
    """Create one build from the project's exact signed bytes and TU predecessors."""

    files = {path: (repository / path).read_bytes() for path in project.inputs}
    dependencies = []
    for source in project.sources:
        name = dependency_node_name(
            repository, architecture, project.name, source, qualifier
        )
        dependencies.append(discovery.node(name))
        try:
            manifest = manifests[name]
        except KeyError as error:
            raise ValueError(f"missing dependency manifest: {name}") from error
        files.update(dependency_inputs(repository, restore_output, source, manifest))
    return factory.make(
        template, f"build-{project.name}-{architecture}-{qualifier}", "slot", variables,
        files=files, dependencies=tuple(dependencies), identity=identity, config=config,
    )


def _dependency_node(
    repository: Path, toolchain: MsvcToolchain, factory: NodeFactory, restore: Node,
    unit: tuple[str, Path, Path], namespace: str, architecture: str, platform: str,
    configuration: str | None, qualifier: str, previous: Mapping[str, bytes],
) -> Node:
    project_name, project, source = unit
    name = dependency_node_name(
        repository, architecture, project_name, source, qualifier
    )
    restore_output = BuildPaths(repository).cas(restore.uid).output
    files = _project_files(repository, project_name, project, source)
    prior = previous.get(name)
    if prior is not None:
        files.update(dependency_inputs(repository, restore_output, source, prior))
    variables = _compile_variables(
        toolchain, project, source, restore_output,
        configuration or ("Release" if project_name == "leak-probe" else "Debug"),
        platform,
    )
    return factory.make(
        "source-dependencies.ps1",
        name,
        "slot",
        variables,
        files=files,
        dependencies=(restore,),
        config={"architecture": architecture, "source_namespace": namespace},
    )


def dependency_discovery_slice(
    repository: Path, toolchain: MsvcToolchain, *,
    project_names: tuple[str, ...] | None = None, configuration: str | None = None,
    restore_flavor: str = "", name_qualifier: str = "", jobs: int = 2,
    architectures: tuple[str, ...] = ("x64",),
) -> Graph:
    """Create cacheable per-TU MSVC ``/sourceDependencies`` nodes."""

    root = repository.resolve(strict=True)
    factory = recipe_factory(root, dict(toolchain.identity), tool_environment(toolchain))
    namespace = "\n".join(
        _relative(root, path)
        for path in sorted(path for path in (root / "src").rglob("*") if path.is_file())
    )
    projects = tuple(
        unit for unit in _projects(root) if project_names is None or unit[0] in project_names
    )
    def create(previous: Mapping[str, bytes]) -> Graph:
        nodes, targets = [], []
        for architecture in architectures:
            platform = _platform(architecture)
            restore = restore_node(
                root, toolchain, factory, architecture, flavor=restore_flavor
            )
            nodes.append(restore)
            for unit in projects:
                project_name = unit[0]
                if project_name == "leak-probe" and architecture != "x64":
                    continue
                node = _dependency_node(
                    root, toolchain, factory, restore, unit, namespace, architecture,
                    platform, configuration, name_qualifier, previous,
                )
                nodes.append(node)
                targets.append(node.name)
        return Graph(tuple(nodes), tuple(targets), {"restore": 1, "slot": jobs})

    base = create({})
    previous = _manifest_index(
        root, {name: base.node(name).uid for name in base.targets}
    )
    return create(previous) if previous else base


def _exact_identity(prefix: str, tool: ResolvedTool) -> dict[str, str]:
    return {f"{prefix}.{key}": value for key, value in tool.identity}


def clang_dependency_discovery_slice(
    repository: Path, toolchain: MsvcToolchain, *,
    project_names: tuple[str, ...] | None = None, configuration: str,
    restore_flavor: str = "", name_qualifier: str = "", jobs: int = 2,
    architectures: tuple[str, ...] = ("x64",),
) -> Graph:
    """Capture actual clang-cl commands and resolve their exact header graph."""

    root = repository.resolve(strict=True)
    environment = tool_environment(toolchain)
    base_identity = dict(toolchain.identity)
    clang = resolve_llvm(toolchain, "clang-cl")
    scanner = resolve_llvm(toolchain, "clang-scan-deps")
    clang_factory = recipe_factory(
        root,
        base_identity | _exact_identity("clang_cl", clang),
        environment,
    )
    scan_factory = recipe_factory(root, base_identity, environment)
    namespace = "\n".join(
        _relative(root, path)
        for path in sorted(path for path in (root / "src").rglob("*") if path.is_file())
    )
    projects = tuple(
        unit for unit in _projects(root) if project_names is None or unit[0] in project_names
    )
    paths = BuildPaths(root)

    def create(previous: Mapping[str, bytes]) -> Graph:
        nodes: list[Node] = []
        targets: list[str] = []
        for architecture in architectures:
            platform = _platform(architecture)
            restore = restore_node(
                root, toolchain, clang_factory, architecture, flavor=restore_flavor
            )
            nodes.append(restore)
            restore_output = paths.cas(restore.uid).output
            for project_name, project, source in projects:
                if project_name == "leak-probe" and architecture != "x64":
                    continue
                name = dependency_node_name(
                    root, architecture, project_name, source, name_qualifier
                )
                files = _project_files(root, project_name, project, source)
                prior = previous.get(name)
                if prior is not None:
                    files.update(dependency_inputs(root, restore_output, source, prior))
                capture = clang_factory.make(
                    "clang-command.ps1",
                    name.replace("discover-dependencies-", "capture-clang-command-", 1),
                    "slot",
                    _compile_variables(
                        toolchain,
                        project,
                        source,
                        restore_output,
                        configuration,
                        platform,
                    ) | {"llvm_dir": str(toolchain.llvm_dir)},
                    files=files,
                    dependencies=(restore,),
                    config={"architecture": architecture, "source_namespace": namespace},
                )
                command_file = paths.cas(capture.uid).output / "compile-command.json"
                scan = python_action(
                    scan_factory,
                    name,
                    "core.clang_dependencies",
                    (
                        "scan",
                        str(source),
                        str(command_file),
                        str(scanner.path),
                        str(clang.path),
                    ),
                    (capture,),
                    pool="slot",
                    environment=environment,
                    identity=_exact_identity("clang_scan_deps", scanner),
                    config={"architecture": architecture, "source_namespace": namespace},
                )
                nodes.extend((capture, scan))
                targets.append(scan.name)
        return Graph(tuple(nodes), tuple(targets), {"restore": 1, "slot": jobs})

    base = create({})
    previous = _manifest_index(
        root, {name: base.node(name).uid for name in base.targets}
    )
    return create(previous) if previous else base


def analysis_discovery_slice(
    repository: Path, toolchain: MsvcToolchain, jobs: int = 2,
    architectures: tuple[str, ...] = ("x64",),
) -> Graph:
    return dependency_discovery_slice(
        repository, toolchain, jobs=jobs, architectures=architectures
    )


def load_dependency_manifests(repository: Path, discovery: Graph) -> dict[str, bytes]:
    """Load manifests only from completed discovery nodes' exact CAS entries."""

    paths, manifests = BuildPaths(repository.resolve(strict=True)), {}
    for name in discovery.targets:
        node = discovery.node(name)
        cas = paths.cas(node.uid)
        manifest = paths.require_confined(
            cas.output / "dependencies.json", paths.cas_root
        )
        if not cas.touch.is_file() or cas.touch.stat().st_size or not manifest.is_file():
            raise FileNotFoundError(f"dependency discovery is incomplete: {name}")
        manifests[name] = manifest.read_bytes()
    return manifests


def _raw(
    repository: Path, toolchain: MsvcToolchain, factory: NodeFactory, discovery: Node,
    restore_output: Path, unit: tuple[str, Path, Path], backend: str,
    dependencies: Mapping[str, bytes],
    architecture: str, platform: str,
) -> Node:
    project_name, project, source = unit
    slug, template, extra = {
        "msvc": ("msvc", "msvc-analyze.ps1", ("build/ObserverNativeAnalysis.ruleset",)),
        "clang-tidy": ("tidy", "clang-tidy.ps1", (".clang-tidy",)),
    }[backend]
    files = _project_files(repository, project_name, project, source, extra)
    files.update(dependencies)
    variables = _compile_variables(
        toolchain, project, source, restore_output,
        "Release" if project_name == "leak-probe" else "Debug",
        platform,
    ) | {"project_name": project_name, "llvm_dir": str(toolchain.llvm_dir)}
    return factory.make(
        template,
        f"analyze-{slug}-{architecture}-{project_name}-{_unit(repository, source)}",
        "slot",
        variables,
        files=files,
        dependencies=(discovery,),
        config={"architecture": architecture, "backend": backend},
    )


def analysis_slice(
    repository: Path, toolchain: MsvcToolchain, *, discovery: Graph,
    manifests: Mapping[str, bytes], jobs: int = 2,
    architectures: tuple[str, ...] = ("x64",),
) -> Graph:
    """Analyze compiler-discovered TU inputs, normalize, merge, and gate."""

    root = repository.resolve(strict=True)
    factory = recipe_factory(root, dict(toolchain.identity), tool_environment(toolchain))
    paths = BuildPaths(root)

    def output(node: Node) -> Path:
        return paths.cas(node.uid).output

    nodes, targets, projects = list(discovery.nodes), [], _projects(root)
    for architecture in architectures:
        platform = _platform(architecture)
        restore = discovery.node(f"restore-vcpkg-{architecture}")
        normalized = []
        for unit in projects:
            project_name, _project, source = unit
            if project_name == "leak-probe" and architecture != "x64":
                continue
            unit_name = _unit(root, source)
            suffix = f"{architecture}-{project_name}-{unit_name}"
            discovery_name = dependency_node_name(root, architecture, project_name, source)
            discovered = discovery.node(discovery_name)
            try:
                manifest = manifests[discovery_name]
            except KeyError as error:
                raise ValueError(f"missing dependency manifest: {discovery_name}") from error
            dependencies = dependency_inputs(root, output(restore), source, manifest)
            raw_nodes = tuple(
                (backend, _raw(
                    root, toolchain, factory, discovered, output(restore), unit, backend,
                    dependencies, architecture, platform,
                ))
                for backend in ("msvc", "clang-tidy")
            )
            nodes.extend(raw for _backend, raw in raw_nodes)
            for backend, raw in raw_nodes:
                action = "normalize-msvc" if backend == "msvc" else "convert-tidy"
                source_input = output(raw) / (
                    f"{project_name}.sarif" if backend == "msvc" else "obj"
                )
                automation = (
                    f"{'msvc-analyze' if backend == 'msvc' else 'clang-tidy'}/"
                    f"{architecture}/{project_name}/{unit_name}/"
                )
                arguments = (
                    (action, str(source_input), automation)
                    if backend == "msvc"
                    else (action, str(root), str(source_input), automation)
                )
                normal = python_action(
                    factory,
                    f"normalize-{'msvc' if backend == 'msvc' else 'tidy'}-{suffix}",
                    "core.sarif",
                    arguments,
                    (raw,),
                    pool="misc",
                )
                nodes.append(normal)
                normalized.append(normal)
        merged = python_action(
            factory,
            f"merge-analysis-{architecture}",
            "core.sarif",
            ("merge", *(str(output(node) / "renpy.sarif") for node in normalized)),
            tuple(normalized),
            pool="misc",
            results=(Result(
                f"reports/sarif/{architecture}/analysis.sarif",
                "sarif",
                "application/sarif+json",
                "analysis.sarif",
            ),),
        )
        gate = python_action(
            factory,
            f"analysis-{architecture}",
            "core.sarif",
            ("gate", str(output(merged) / "analysis.sarif")),
            (merged,),
            pool="misc",
        )
        nodes.extend((merged, gate))
        targets.append(gate.name)
    return Graph(tuple(nodes), tuple(targets), {"misc": jobs, "restore": 1, "slot": jobs})

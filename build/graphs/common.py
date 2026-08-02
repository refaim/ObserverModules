"""Shared recipe plumbing for repository build graphs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from core.graph import Graph, Node
from core.node import NodeFactory
from core.paths import BuildPaths


BUILD_ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ("renpy", "rpgmaker", "zanzarah", "tests")
BINARIES = {**{name: f"{name}.so" for name in PROJECTS[:-1]}, "tests": "tests.exe"}
PLATFORMS = {"x86": "Win32", "x64": "x64", "arm64": "ARM64"}
_MSBUILD_NS = "{http://schemas.microsoft.com/developer/msbuild/2003}"
_PROJECT_PREFIX = "$(RepositoryRoot)"
COMMON_PROJECT_INPUTS = (
    "build/ObserverProjectConfigurations.props",
    "build/ObserverConfiguration.props",
    "build/ObserverProject.props",
)


def require_positive_integers(values: Iterable[object], message: str) -> None:
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        raise ValueError(message)


def require_tool(path: Path, name: str) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise FileNotFoundError(f"{name} is not a file: {resolved}")
    return resolved


def prefixed_identity(prefix: str, path: Path, values: Mapping[str, str]) -> dict[str, str]:
    return {f"{prefix}.{key}": value for key, value in values.items()} | {
        f"{prefix}.path": str(path)
    }


def extend_pools(upstream: Graph, additions: Mapping[str, int]) -> dict[str, int]:
    pools = dict(upstream.pools)
    for name, capacity in additions.items():
        if name in pools and pools[name] != capacity:
            raise ValueError(f"conflicting pool capacity for {name}")
        pools[name] = capacity
    return pools


def produced_path(paths: BuildPaths, upstream: Graph, producer: Node, candidate: Path, message: str) -> Path:
    try:
        if upstream.node(producer.name) != producer:
            raise ValueError("producer does not match upstream")
        output = paths.cas(producer.uid, producer.name).output
        current = paths.require_confined(candidate, paths.cas_root)
    except ValueError as error:
        raise ValueError(message) from error
    if current == output or not current.is_relative_to(output):
        raise ValueError(message)
    return current


def require_ancestor(upstream: Graph, producer: Node, ancestor: str, message: str) -> None:
    pending, seen = list(producer.inputs), set()
    while pending:
        name = pending.pop()
        if name == ancestor:
            return
        if name not in seen:
            seen.add(name)
            pending.extend(upstream.node(name).inputs)
    raise ValueError(message)


def required_audit_gates(upstream: Graph, producer: Node, architecture: str, module: str) -> tuple[Node, Node]:
    try:
        gates = tuple(upstream.node(f"audit-{kind}-{architecture}-{module}") for kind in ("pe", "binskim"))
    except ValueError as error:
        raise ValueError(f"missing audit gates for {architecture}/{module}") from error
    for gate in gates:
        require_ancestor(
            upstream, gate, producer.name,
            f"audit gates do not consume producer for {architecture}/{module}",
        )
    return gates


def project_path(repository: Path, value: str, project: Path) -> str:
    if not value.startswith(_PROJECT_PREFIX):
        raise ValueError(f"unsupported project input in {project}: {value}")
    path = repository / value.removeprefix(_PROJECT_PREFIX).replace("\\", "/")
    return path.resolve(strict=True).relative_to(repository).as_posix()


def project_inputs(repository: Path, project: Path) -> tuple[str, ...]:
    inputs = list(COMMON_PROJECT_INPUTS) + [project.relative_to(repository).as_posix()]
    for item in ET.parse(project).getroot().iter(f"{_MSBUILD_NS}ModuleDefinitionFile"):
        if item.text:
            inputs.append(project_path(repository, item.text.strip(), project))
    return tuple(dict.fromkeys(inputs))


def project_sources(repository: Path, project: Path) -> tuple[Path, ...]:
    return tuple(
        repository / project_path(repository, item.get("Include", ""), project)
        for item in ET.parse(project).getroot().iter(f"{_MSBUILD_NS}ClCompile")
        if item.get("Include")
    )


def tool_environment(toolchain: object, *, prepend_path: Path | None = None,
                     extra: tuple[tuple[str, str], ...] = ()) -> tuple[tuple[str, str], ...]:
    values = {key.casefold(): (key, value) for key, value in toolchain.environment}
    if hasattr(toolchain, "vcpkg_root"):
        values["vcpkg_root"] = ("VCPKG_ROOT", str(toolchain.vcpkg_root))
    if prepend_path is not None:
        current = values.get("path", ("PATH", ""))[1]
        values["path"] = ("PATH", str(prepend_path) + (os.pathsep + current if current else ""))
    values.update((key.casefold(), (key, value)) for key, value in extra)
    return tuple(values.values())


def _restore_identity(toolchain: object, vcpkg: Path) -> dict[str, str]:
    identity = {
        "pwsh": str(toolchain.pwsh),
        "vcpkg": str(vcpkg),
        "vcpkg_root": str(toolchain.vcpkg_root),
    }
    for name, path in (("pwsh", Path(toolchain.pwsh)), ("vcpkg", vcpkg)):
        if path.is_file():
            with path.open("rb") as stream:
                identity[f"{name}_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    return identity


def restore_node(repository: Path, toolchain: object, factory: NodeFactory, architecture: str,
                 *, flavor: str = "") -> Node:
    qualifier = f"-{flavor}" if flavor else ""
    triplet = f"observer-{architecture}-windows-static{qualifier}"
    inputs = ("vcpkg.json", f"build/vcpkg/triplets/{triplet}.cmake")
    vcpkg = Path(toolchain.vcpkg_root) / "vcpkg.exe"
    return factory.make(
        "vcpkg.ps1", f"restore-vcpkg{qualifier}-{architecture}", "restore", {
            "pwsh": str(toolchain.pwsh),
            "vcpkg": str(vcpkg),
            "repository": str(repository),
            "triplet": triplet,
        },
        files={path: (repository / path).read_bytes() for path in inputs},
        config={"action": "restore", "architecture": architecture, "triplet": triplet},
        identity=_restore_identity(toolchain, vcpkg),
        environment=tuple(sorted((key.upper(), value) for key, value in tool_environment(toolchain))),
        cwd=repository,
    )


def python_action(factory: NodeFactory, name: str, module: str, arguments: tuple[str, ...],
                  dependencies: tuple[Node, ...], *, pool: str,
                  environment: tuple[tuple[str, str], ...] = (), files: Mapping[str, bytes] | None = None,
                  identity: Mapping[str, str] | None = None,
                  config: Mapping[str, str] | None = None) -> Node:
    executable = str(Path(sys.executable).resolve())
    source = BUILD_ROOT.joinpath(*module.split(".")).with_suffix(".py")
    return factory.make(
        "argv.json", name, pool, {"argv": (executable, "-m", module) + arguments},
        files={source.relative_to(BUILD_ROOT.parent).as_posix(): source.read_bytes()} | dict(files or {}),
        dependencies=dependencies,
        identity=dict(identity or {}) | {"python": sys.version, "python_executable": executable},
        config={"action": arguments[0], "platform": "windows"} | dict(config or {}),
        environment=environment,
        cwd=BUILD_ROOT,
    )

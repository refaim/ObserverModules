"""Fine-grained repository source checks."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from core.graph import Graph, Node
from core.node import NodeFactory
from core.paths import BuildPaths
from core.render import TemplateRenderer
from core.source_tools import SourceTools
from graphs.common import python_action, restore_node, tool_environment


_BUILD_ROOT = Path(__file__).resolve().parents[1]
_CPP_SUFFIXES = {".cpp", ".h", ".hpp"}
_POWERSHELL_SUFFIXES = {".ps1", ".psm1"}
_CPPCHECK_ARCHITECTURES = {
    "x86": ("win32W", "_M_IX86=600"),
    "x64": ("win64", "_M_X64=100"),
    "arm64": ("win64", "_M_ARM64=1"),
}
_IGNORED_DIRECTORIES = {".git", ".artifacts", ".venv", "__pycache__", "out"}


def _relative(repository: Path, path: Path) -> str:
    return path.resolve(strict=True).relative_to(repository).as_posix()


def _slug(repository: Path, path: Path) -> str:
    return _relative(repository, path).lower().replace("/", ".")


def _repository_files(repository: Path) -> tuple[Path, ...]:
    files = []
    for directory, directories, names in repository.walk():
        directories[:] = sorted(set(directories) - _IGNORED_DIRECTORIES)
        files.extend(directory / name for name in names if not name.startswith(".coverage"))
    return tuple(sorted(files))


def source_checks(repository: Path, tools: SourceTools, jobs: int = 4,
                  architectures: Iterable[str] = _CPPCHECK_ARCHITECTURES) -> Graph:
    """Return independently cacheable format, analyzer, and contract checks."""

    selected_architectures = tuple(architectures)
    if (not selected_architectures or len(set(selected_architectures)) != len(selected_architectures) or
            any(item not in _CPPCHECK_ARCHITECTURES for item in selected_architectures)):
        raise ValueError("source architectures must be unique supported architecture names")
    root = repository.resolve(strict=True)
    renderer = TemplateRenderer(_BUILD_ROOT / "templates")
    identities = dict(tools.identity)
    restore_identity = {key: identities[key] for key in ("pwsh", "pwsh_version", "vcpkg_root")}
    factory = NodeFactory(renderer, root, {}, tools.environment)
    restore_factory = NodeFactory(renderer, root, restore_identity, tool_environment(tools))
    paths = BuildPaths(root)

    def tool_identity(*names: str) -> dict[str, str]:
        return {key: identities[key] for name in names for key in (name, f"{name}_version")}

    cpp_sources = tuple(sorted(
        path for path in (root / "src").rglob("*")
        if path.is_file() and path.suffix in _CPP_SUFFIXES
    ))
    powershell_sources = (root / "build.ps1",) + tuple(sorted(
        path for path in (root / "build").rglob("*")
        if path.is_file() and path.suffix in _POWERSHELL_SUFFIXES
    ))
    contracts = tuple(sorted((root / "build/tests").glob("*.Tests.ps1")))

    def leaf(
        template: str,
        name: str,
        variables: dict[str, object],
        inputs: tuple[Path, ...],
        *,
        dependencies: tuple[Node, ...] = (),
        identity: dict[str, str],
        config: dict[str, str],
    ) -> Node:
        return factory.make(template, name, "slot", variables,
            files={_relative(root, path): path.read_bytes() for path in inputs},
            dependencies=dependencies, identity=identity, config=config,
        )

    format_nodes = tuple(
        leaf(
            "clang-format.ps1",
            f"format-{_slug(root, source)}",
            {
                "pwsh": str(tools.pwsh),
                "clang_format": str(tools.clang_format),
                "source": str(source),
            },
            (root / ".clang-format", source),
            identity=tool_identity("pwsh", "clang_format"),
            config={"action": "format", "source": _relative(root, source)},
        )
        for source in cpp_sources
    )

    restore_nodes = []
    cppcheck_nodes = []
    for architecture in selected_architectures:
        platform, architecture_define = _CPPCHECK_ARCHITECTURES[architecture]
        triplet = f"observer-{architecture}-windows-static"
        restore = restore_node(root, tools, restore_factory, architecture)
        restore_nodes.append(restore)
        include_dir = paths.cas(restore.uid, restore.name).output / triplet / "include"
        cppcheck_nodes.append(
            leaf(
                "cppcheck.ps1",
                f"cppcheck-{architecture}",
                {
                    "pwsh": str(tools.pwsh),
                    "cppcheck": str(tools.cppcheck),
                    "repository": str(root),
                    "source_dir": str(root / "src"),
                    "include_dir": str(include_dir),
                    "platform": platform,
                    "architecture_define": architecture_define,
                    "automation_id": f"cppcheck/{architecture}/",
                },
                cpp_sources,
                dependencies=(restore,),
                identity=tool_identity("pwsh", "cppcheck"),
                config={"action": "cppcheck", "architecture": architecture},
            )
        )
    cppcheck_nodes = tuple(cppcheck_nodes)
    restore_nodes = tuple(restore_nodes)

    settings = root / "build/PSScriptAnalyzerSettings.psd1"
    pssa_nodes = tuple(
        leaf(
            "psscriptanalyzer.ps1",
            f"pssa-{_slug(root, source)}",
            {
                "pwsh": str(tools.pwsh),
                "psscriptanalyzer": str(tools.psscriptanalyzer),
                "repository": str(root),
                "source": str(source),
                "settings": str(settings),
                "automation_id": f"psscriptanalyzer/{_relative(root, source)}/",
            },
            (settings, source),
            identity=tool_identity("pwsh", "psscriptanalyzer"),
            config={"action": "psscriptanalyzer", "source": _relative(root, source)},
        )
        for source in powershell_sources
    )

    contract_inputs = _repository_files(root)
    contract_nodes = tuple(
        leaf(
            "contract.ps1",
            f"contract-{_slug(root, test)}",
            {
                "pwsh": str(tools.pwsh),
                "test": str(test),
            },
            contract_inputs,
            identity=tool_identity("pwsh"),
            config={"action": "contract", "test": _relative(root, test)},
        )
        for test in contracts
    )

    def output(current: Node) -> Path:
        return paths.cas(current.uid, current.name).output

    def report(current: Node) -> Path:
        name = "cppcheck.sarif" if current.name.startswith("cppcheck-") else "psscriptanalyzer.sarif"
        return output(current) / name

    finding_nodes = cppcheck_nodes + pssa_nodes
    merged = python_action(
        factory,
        "merge-source-findings",
        "core.sarif",
        ("merge", *(str(report(current)) for current in finding_nodes)),
        finding_nodes,
        pool="slot",
    )
    direct = format_nodes + contract_nodes
    gate = python_action(
        factory,
        "source-checks",
        "core.sarif",
        ("gate", str(output(merged) / "analysis.sarif")),
        (merged,) + direct,
        pool="slot",
    )
    nodes = restore_nodes + format_nodes + cppcheck_nodes + pssa_nodes + contract_nodes + (merged, gate)
    return Graph(nodes, (gate.name,), {"restore": 1, "slot": jobs})

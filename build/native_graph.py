"""Typed expansion for the fine-grained Windows native and analysis DAG.

The module only describes normalized graph nodes.  Process execution remains in the
outer graph driver, and all compile/link work remains in the checked-in MSBuild
projects.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import xml.etree.ElementTree as ET


MSBUILD_NAMESPACE = "http://schemas.microsoft.com/developer/msbuild/2003"
PROJECT_NAMES = (
    "renpy",
    "rpgmaker",
    "zanzarah",
    "tests",
    "fuzz-pickle",
    "fuzz-renpy",
    "fuzz-rpgmaker",
    "fuzz-zanzarah",
    "leak-probe",
)
RUNTIME_PROJECT_NAMES = ("renpy", "rpgmaker", "zanzarah", "tests")
FUZZ_PROJECT_NAMES = ("fuzz-pickle", "fuzz-renpy", "fuzz-rpgmaker", "fuzz-zanzarah")
FUZZ_TARGET_NAMES = tuple(name.removeprefix("fuzz-") for name in FUZZ_PROJECT_NAMES)
RUNTIME_CONFIGURATIONS = ("Debug", "Release", "Coverage", "ASan", "UBSan")


class NativeGraphError(ValueError):
    """Raised when the checked-in native project manifest is not reviewable."""


@dataclass(frozen=True)
class TranslationUnit:
    project: str
    source: PurePosixPath
    slug: str

    @property
    def key(self) -> str:
        return f"{self.project}:{self.source.as_posix()}"


@dataclass(frozen=True)
class Project:
    name: str
    project_file: PurePosixPath
    translation_units: tuple[TranslationUnit, ...]


@dataclass(frozen=True)
class Manifest:
    workspace: Path
    projects: tuple[Project, ...]

    @property
    def translation_units(self) -> tuple[TranslationUnit, ...]:
        return tuple(unit for project in self.projects for unit in project.translation_units)


def _translation_unit_slug(source: PurePosixPath) -> str:
    readable = source.as_posix().removeprefix("src/").removesuffix(".cpp")
    readable = re.sub(r"[^a-z0-9]+", "-", readable.lower()).strip("-")
    digest = sha256(source.as_posix().encode("utf-8")).hexdigest()[:8]
    return f"{readable}-{digest}"


def _repository_source(include: str, workspace: Path, project_name: str) -> PurePosixPath:
    prefix = "$(RepositoryRoot)"
    if not include.startswith(prefix):
        raise NativeGraphError(
            f"{project_name} ClCompile path must start with {prefix!r}: {include!r}"
        )
    source = PurePosixPath(include.removeprefix(prefix).replace("\\", "/"))
    if source.is_absolute() or ".." in source.parts or source.suffix.lower() != ".cpp":
        raise NativeGraphError(f"unsafe {project_name} translation unit path: {source}")
    if not (workspace / Path(*source.parts)).is_file():
        raise NativeGraphError(f"missing {project_name} translation unit: {source}")
    return source


def load_manifest(
    workspace: Path, *, project_names: tuple[str, ...] = PROJECT_NAMES
) -> Manifest:
    resolved_workspace = workspace.resolve()
    unknown = [name for name in project_names if name not in PROJECT_NAMES]
    if unknown:
        raise NativeGraphError(f"unknown project(s): {', '.join(unknown)}")
    if len(set(project_names)) != len(project_names):
        raise NativeGraphError("project manifest contains duplicate names")

    projects: list[Project] = []
    for project_name in project_names:
        project_path = resolved_workspace / "build" / "projects" / f"{project_name}.vcxproj"
        if not project_path.is_file():
            raise NativeGraphError(f"missing project: {project_path}")
        try:
            root = ET.parse(project_path).getroot()
        except ET.ParseError as error:
            raise NativeGraphError(f"invalid MSBuild XML in {project_path}: {error}") from error

        sources = tuple(
            _repository_source(element.attrib["Include"], resolved_workspace, project_name)
            for element in root.findall(f".//{{{MSBUILD_NAMESPACE}}}ClCompile")
            if "Include" in element.attrib
        )
        if not sources:
            raise NativeGraphError(f"project has no ClCompile items: {project_name}")
        if len(set(sources)) != len(sources):
            raise NativeGraphError(f"project has duplicate ClCompile items: {project_name}")
        units = tuple(
            TranslationUnit(project_name, source, _translation_unit_slug(source))
            for source in sources
        )
        projects.append(
            Project(
                project_name,
                PurePosixPath("build", "projects", f"{project_name}.vcxproj"),
                units,
            )
        )
    return Manifest(resolved_workspace, tuple(projects))


def _leaf_argv(action: str, *arguments: str) -> list[str]:
    return [
        "pwsh",
        "-NoLogo",
        "-NoProfile",
        "-File",
        "build.ps1",
        "graph-leaf",
        "-GraphLeafAction",
        action,
        *arguments,
    ]


def fuzz_build_node_name(target: str) -> str:
    """Return the single build-node identity shared with the dynamic fuzz graph."""

    if target not in FUZZ_TARGET_NAMES:
        raise NativeGraphError(f"unknown fuzz target: {target}")
    return f"build-fuzz-{target}"


def build_project_node_name(configuration: str, project_name: str) -> str:
    """Return the build-node identity shared by native and dynamic graph sections."""

    supported = (
        project_name in RUNTIME_PROJECT_NAMES
        and configuration in RUNTIME_CONFIGURATIONS
    ) or (project_name == "leak-probe" and configuration == "Release")
    if not supported:
        raise NativeGraphError(
            f"unsupported project build: {configuration}/{project_name}"
        )
    return f"build-{configuration.lower()}-{project_name}"


def _node(
    name: str,
    *,
    deps: list[str],
    resources: dict[str, int],
    argv: list[str],
    inputs: list[str],
    outputs: list[str],
    writes: list[str],
) -> dict[str, object]:
    return {
        "name": name,
        "deps": deps,
        "run_after": [],
        "resources": resources,
        "argv": argv,
        "inputs": inputs,
        "outputs": outputs,
        "writes": writes,
        "fingerprint": ["contract=native-microdag-v1", f"node={name}"],
        "cacheable": False,
    }


def _binary_writes(configuration: str, project_name: str) -> list[str]:
    root = f".artifacts/bin/x64/{configuration}"
    object_root = f".artifacts/obj/x64/{configuration}/{project_name}/"
    if project_name in ("tests", "leak-probe") or project_name.startswith("fuzz-"):
        return [object_root, f"{root}/{project_name}.exe", f"{root}/{project_name}.pdb"]
    return [
        object_root,
        f"{root}/{project_name}.so",
        f"{root}/{project_name}.pdb",
        f"{root}/{project_name}.lib",
        f"{root}/{project_name}.exp",
    ]


def _build_node(configuration: str, project_name: str) -> dict[str, object]:
    if configuration == "Fuzz":
        name = fuzz_build_node_name(project_name.removeprefix("fuzz-"))
    else:
        name = build_project_node_name(configuration, project_name)
    binary_extension = (
        ".exe"
        if project_name in ("tests", "leak-probe") or project_name.startswith("fuzz-")
        else ".so"
    )
    return _node(
        name,
        deps=["source-checks"],
        resources={"cpu": 4, "memory-gib": 2, "native-msbuild": 1},
        argv=_leaf_argv(
            "build-project",
            "-Arch",
            "x64",
            "-Config",
            configuration,
            "-Project",
            project_name,
        ),
        inputs=[
            "build/**/*.props",
            f"build/projects/{project_name}.vcxproj",
            "src/**/*.cpp",
            "src/**/*.h",
            "vcpkg.json",
        ],
        outputs=[f".artifacts/bin/x64/{configuration}/{project_name}{binary_extension}"],
        writes=_binary_writes(configuration, project_name),
    )


def _test_node(configuration: str) -> dict[str, object]:
    config_key = configuration.lower()
    report = f".artifacts/reports/tests/tests-x64-{config_key}.xml"
    return _node(
        f"run-tests-{config_key}",
        deps=sorted(f"build-{config_key}-{project}" for project in RUNTIME_PROJECT_NAMES),
        resources={"cpu": 1, "test-run": 1},
        argv=_leaf_argv("run-tests", "-Arch", "x64", "-Config", configuration),
        inputs=[f".artifacts/bin/x64/{configuration}/*"],
        outputs=[report],
        writes=[report],
    )


def _msvc_analysis_nodes(unit: TranslationUnit) -> tuple[dict[str, object], dict[str, object]]:
    configuration = "Release" if unit.project == "leak-probe" else "Debug"
    node_suffix = f"{unit.project}-{unit.slug}"
    scratch = f".artifacts/analysis/msvc/x64/{unit.project}/{unit.slug}/"
    raw = f"{scratch}{unit.project}.sarif"
    normalized = f".artifacts/reports/msvc/x64/units/{node_suffix}.sarif"
    analyze_name = f"analyze-msvc-{node_suffix}"
    analyze = _node(
        analyze_name,
        deps=["source-checks"],
        resources={"cpu": 1, "memory-gib": 2, "msvc-analysis": 1},
        argv=_leaf_argv(
            "analyze-msvc-project",
            "-Arch",
            "x64",
            "-Config",
            configuration,
            "-Project",
            unit.project,
            "-SelectedFile",
            unit.source.as_posix(),
            "-Unit",
            unit.slug,
        ),
        inputs=[
            "build/ObserverNativeAnalysis.ruleset",
            "build/**/*.props",
            f"build/projects/{unit.project}.vcxproj",
            unit.source.as_posix(),
        ],
        outputs=[raw],
        writes=[scratch],
    )
    normalize = _node(
        f"normalize-msvc-{node_suffix}",
        deps=[analyze_name],
        resources={"cpu": 1, "sarif": 1},
        argv=_leaf_argv(
            "normalize-msvc-sarif",
            "-Arch",
            "x64",
            "-Project",
            unit.project,
            "-Unit",
            unit.slug,
            "-Input",
            raw,
            "-Output",
            normalized,
        ),
        inputs=[raw],
        outputs=[normalized],
        writes=[normalized],
    )
    return analyze, normalize


def _tidy_analysis_nodes(unit: TranslationUnit) -> tuple[dict[str, object], dict[str, object]]:
    node_suffix = f"{unit.project}-{unit.slug}"
    scratch = f".artifacts/analysis/clang-tidy/x64/{unit.project}/{unit.slug}/"
    object_root = f"{scratch}obj/"
    raw_log = f"{object_root}{unit.project}.ClangTidy.log"
    normalized = f".artifacts/reports/clang-tidy/x64/units/{node_suffix}.sarif"
    analyze_name = f"analyze-tidy-{node_suffix}"
    analyze = _node(
        analyze_name,
        deps=["source-checks"],
        resources={"clang-tidy": 1, "cpu": 1},
        argv=_leaf_argv(
            "analyze-tidy-unit",
            "-Arch",
            "x64",
            "-Project",
            unit.project,
            "-SelectedFile",
            unit.source.as_posix(),
            "-Unit",
            unit.slug,
        ),
        inputs=[
            ".clang-tidy",
            "build/**/*.props",
            f"build/projects/{unit.project}.vcxproj",
            unit.source.as_posix(),
        ],
        outputs=[raw_log],
        writes=[scratch],
    )
    normalize = _node(
        f"normalize-tidy-{node_suffix}",
        deps=[analyze_name],
        resources={"cpu": 1, "sarif": 1},
        argv=_leaf_argv(
            "normalize-tidy-sarif",
            "-Arch",
            "x64",
            "-Project",
            unit.project,
            "-Unit",
            unit.slug,
            "-InputRoot",
            object_root,
            "-Output",
            normalized,
        ),
        inputs=[raw_log],
        outputs=[normalized],
        writes=[normalized],
    )
    return analyze, normalize


def expand_x64_nodes(manifest: Manifest) -> list[dict[str, object]]:
    """Return schema-v2 normalized records; profile-level roots are added by the caller."""

    nodes: list[dict[str, object]] = []
    for configuration in RUNTIME_CONFIGURATIONS:
        nodes.extend(_build_node(configuration, project) for project in RUNTIME_PROJECT_NAMES)
        nodes.append(_test_node(configuration))
    for project in FUZZ_PROJECT_NAMES:
        nodes.append(_build_node("Fuzz", project))
    nodes.append(_build_node("Release", "leak-probe"))

    msvc_normalizers: list[str] = []
    msvc_reports: list[str] = []
    for unit in manifest.translation_units:
        analyze, normalize = _msvc_analysis_nodes(unit)
        nodes.extend((analyze, normalize))
        msvc_normalizers.append(str(normalize["name"]))
        msvc_reports.append(str(normalize["outputs"][0]))

    tidy_normalizers: list[str] = []
    tidy_reports: list[str] = []
    for unit in manifest.translation_units:
        analyze, normalize = _tidy_analysis_nodes(unit)
        nodes.extend((analyze, normalize))
        tidy_normalizers.append(str(normalize["name"]))
        tidy_reports.append(str(normalize["outputs"][0]))

    msvc_merged = ".artifacts/reports/msvc/x64/msvc-analyze.sarif"
    tidy_merged = ".artifacts/reports/clang-tidy/x64/clang-tidy.sarif"
    nodes.append(
        _node(
            "merge-msvc-sarif",
            deps=msvc_normalizers,
            resources={"cpu": 1, "sarif": 1},
            argv=_leaf_argv(
                "merge-sarif",
                "-Arch",
                "x64",
                "-Backend",
                "msvc",
                "-InputPathsJson",
                json.dumps(msvc_reports, separators=(",", ":")),
                "-Output",
                msvc_merged,
            ),
            inputs=msvc_reports,
            outputs=[msvc_merged],
            writes=[msvc_merged],
        )
    )
    nodes.append(
        _node(
            "merge-tidy-sarif",
            deps=tidy_normalizers,
            resources={"cpu": 1, "sarif": 1},
            argv=_leaf_argv(
                "merge-sarif",
                "-Arch",
                "x64",
                "-Backend",
                "clang-tidy",
                "-InputPathsJson",
                json.dumps(tidy_reports, separators=(",", ":")),
                "-Output",
                tidy_merged,
            ),
            inputs=tidy_reports,
            outputs=[tidy_merged],
            writes=[tidy_merged],
        )
    )
    gate_report = ".artifacts/reports/analysis/x64/gate.json"
    nodes.append(
        _node(
            "analysis-gate",
            deps=["merge-msvc-sarif", "merge-tidy-sarif"],
            resources={"cpu": 1, "sarif": 1},
            argv=_leaf_argv(
                "analysis-gate",
                "-Arch",
                "x64",
                "-MsvcReport",
                msvc_merged,
                "-ClangTidyReport",
                tidy_merged,
            ),
            inputs=[msvc_merged, tidy_merged],
            outputs=[gate_report],
            writes=[gate_report],
        )
    )
    return nodes

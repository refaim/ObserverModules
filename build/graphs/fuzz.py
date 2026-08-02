"""Independent build, seed-replay, and bounded-run branches for every fuzzer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from core.graph import Graph, Node, Result
from core.paths import BuildPaths
from core.toolchain import MsvcToolchain
from graphs.analysis import (
    clang_dependency_discovery_slice,
    dependency_inputs,
    dependency_node_name,
)
from graphs.common import recipe_factory, tool_environment


_BUILD_ROOT = Path(__file__).resolve().parents[1]
_NS = "{http://schemas.microsoft.com/developer/msbuild/2003}"
_PREFIX = "$(RepositoryRoot)"
_TARGETS = {"pickle": 262144, "renpy": 1048576, "rpgmaker": 1048576, "zanzarah": 1048576}
FUZZ_TARGETS = tuple(_TARGETS)
_COMMON = (
    "build/ObserverProjectConfigurations.props",
    "build/ObserverConfiguration.props",
    "build/ObserverProject.props",
    "build/ObserverFuzz.props",
)
_ASAN_OPTIONS = "halt_on_error=1:alloc_dealloc_mismatch=1"
_UID = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class FuzzCorpusArtifact:
    """Reference one successfully published timed-run corpus by canonical CAS UID."""

    target: str
    producer_uid: str

    def __post_init__(self) -> None:
        if self.target not in _TARGETS:
            raise ValueError(f"unsupported fuzz corpus target: {self.target}")
        if not isinstance(self.producer_uid, str) or not _UID.fullmatch(self.producer_uid):
            raise ValueError("fuzz corpus producer UID must be a canonical MD5")


def fuzz_corpus_artifacts(graph: Graph) -> tuple[FuzzCorpusArtifact, ...]:
    return tuple(
        FuzzCorpusArtifact(target, graph.node(f"run-fuzz-x64-{target}").uid)
        for target in FUZZ_TARGETS if f"fuzz-x64-{target}" in graph.targets
    )


def _selected_targets(targets: tuple[str, ...]) -> tuple[str, ...]:
    if not targets:
        raise ValueError("fuzz targets must not be empty")
    selected = set()
    for target in targets:
        if target not in _TARGETS:
            raise ValueError(f"unsupported fuzz target: {target}")
        if target in selected:
            raise ValueError(f"duplicate fuzz target: {target}")
        selected.add(target)
    return targets


def _runtime(toolchain: MsvcToolchain) -> Path:
    candidates = sorted(toolchain.llvm_dir.glob("lib/clang/*/lib/windows"), reverse=True)
    if not candidates or not candidates[0].is_dir():
        raise FileNotFoundError(f"LLVM sanitizer runtimes not found below {toolchain.llvm_dir}")
    return candidates[0].resolve()


def fuzz_dependency_discovery_slice(
    repository: Path, toolchain: MsvcToolchain, *, jobs: int = 2,
    targets: tuple[str, ...] = FUZZ_TARGETS,
) -> Graph:
    """Discover Fuzz-configuration dependencies against the ASan triplet."""

    return clang_dependency_discovery_slice(
        repository, toolchain,
        project_names=tuple(f"fuzz-{target}" for target in _selected_targets(targets)),
        configuration="Fuzz", restore_flavor="asan", name_qualifier="fuzz", jobs=jobs,
    )


def _project_files(
    repository: Path, target: str, restore_output: Path, discovery: Graph,
    manifests: Mapping[str, bytes],
) -> tuple[dict[str, bytes], tuple[Node, ...]]:
    project = repository / f"build/projects/fuzz-{target}.vcxproj"
    paths = [repository / relative for relative in _COMMON] + [project]
    files = {}
    dependencies = []
    for item in ET.parse(project).getroot().iter(f"{_NS}ClCompile"):
        include = item.get("Include", "")
        if not include.startswith(_PREFIX):
            raise ValueError(f"unsupported ClCompile path in {project}: {include}")
        source = (repository / include.removeprefix(_PREFIX)).resolve(strict=True)
        name = dependency_node_name(repository, "x64", f"fuzz-{target}", source, "fuzz")
        dependencies.append(discovery.node(name))
        try:
            manifest = manifests[name]
        except KeyError as error:
            raise ValueError(f"missing dependency manifest: {name}") from error
        files.update(dependency_inputs(repository, restore_output, source, manifest))
    relative = (path.resolve(strict=True).relative_to(repository).as_posix() for path in paths)
    files.update({name: (repository / name).read_bytes() for name in dict.fromkeys(relative)})
    return files, tuple(dependencies)


def _seed_files(repository: Path, target: str) -> tuple[Path, dict[str, bytes]]:
    directory = repository / "src/fuzz/corpus" / target
    seeds = tuple(sorted(path for path in directory.iterdir() if path.is_file()))
    if not seeds:
        raise FileNotFoundError(f"no checked-in fuzzer seeds for {target}")
    return directory, {
        path.relative_to(repository).as_posix(): path.read_bytes() for path in seeds
    }


def _prior_corpora(
    repository: Path, artifacts: tuple[FuzzCorpusArtifact, ...]
) -> dict[str, tuple[FuzzCorpusArtifact, Path, dict[str, bytes]]]:
    indexed = {}
    for artifact in artifacts:
        if artifact.target in indexed:
            raise ValueError(f"duplicate prior corpus: {artifact.target}")
        indexed[artifact.target] = artifact

    paths, result = BuildPaths(repository), {}
    for target, artifact in indexed.items():
        node_name = f"run-fuzz-x64-{target}"
        cas = paths.cas(artifact.producer_uid)
        corpus = paths.require_confined(cas.output / "corpus", paths.cas_root)
        if not (
            cas.entry.is_dir()
            and cas.output.is_dir()
            and cas.log.is_file()
            and cas.touch.is_file()
            and cas.touch.stat().st_size == 0
            and corpus.is_dir()
        ):
            raise FileNotFoundError(f"prior fuzz corpus is not published: {target}")
        files = {}
        for path in sorted(corpus.iterdir()):
            confined = paths.require_confined(path, paths.cas_root)
            if not confined.is_file():
                raise ValueError(f"prior fuzz corpus contains a non-file: {confined}")
            files[f"prior/{target}/{confined.name}"] = confined.read_bytes()
        if not files:
            raise ValueError(f"prior fuzz corpus is empty: {target}")
        result[target] = (artifact, corpus, files)
    return result


def fuzz_graph(
    repository: Path, toolchain: MsvcToolchain, *, discovery: Graph | None,
    manifests: Mapping[str, bytes], run_nonce: str, seconds: int = 60,
    jobs: int = 2, fuzz_jobs: int = 2,
    architectures: tuple[str, ...] = ("x64",),
    prior_corpora: tuple[FuzzCorpusArtifact, ...] = (),
    targets: tuple[str, ...] = FUZZ_TARGETS,
) -> Graph:
    """Return the selected demand-independent x64 libFuzzer branches."""

    if architectures != ("x64",):
        raise ValueError("libFuzzer MSBuild contract is x64-only")
    if not isinstance(run_nonce, str) or not run_nonce or "\0" in run_nonce:
        raise ValueError("run nonce must be a non-empty string without NUL")
    if isinstance(seconds, bool) or seconds <= 0:
        raise ValueError("fuzz seconds must be positive")
    selected_targets = _selected_targets(targets)
    if discovery is None:
        raise ValueError("fuzz dependency discovery is required")
    root = repository.resolve(strict=True)
    prior = _prior_corpora(root, prior_corpora)
    runtime = _runtime(toolchain)
    paths = BuildPaths(root)
    identity = dict(toolchain.identity) | {"llvm_runtime": str(runtime)}
    factory = recipe_factory(root, identity, tool_environment(toolchain))

    restore = discovery.node("restore-vcpkg-asan-x64")
    nodes, targets = list(discovery.nodes), []
    restore_output = paths.cas(restore.uid).output

    for target in selected_targets:
        max_length = _TARGETS[target]
        executable_name = f"fuzz-{target}.exe"
        files, dependencies = _project_files(
            root, target, restore_output, discovery, manifests
        )
        build = factory.make(
            "fuzz-build.ps1",
            f"build-fuzz-x64-{target}",
            "build",
            {
                "pwsh": str(toolchain.pwsh), "msbuild": str(toolchain.msbuild),
                "project": str(root / f"build/projects/fuzz-{target}.vcxproj"),
                "target": "Build", "configuration": "Fuzz", "platform": "x64",
                "vcpkg_root": str(toolchain.vcpkg_root), "vcpkg_installed": str(restore_output),
                "llvm_dir": str(toolchain.llvm_dir), "llvm_runtime": str(runtime),
                "executable_name": executable_name,
            },
            files=files, dependencies=dependencies,
            config={"action": "build", "architecture": "x64", "target": target},
        )
        seed_dir, seed_files = _seed_files(root, target)
        fuzzer = paths.cas(build.uid).output / executable_name
        replay = factory.make(
            "fuzz-replay.ps1",
            f"replay-fuzz-x64-{target}",
            "fuzz",
            {
                "pwsh": str(toolchain.pwsh), "fuzzer": str(fuzzer),
                "seed_dir": str(seed_dir), "max_length": max_length,
            },
            files=seed_files, dependencies=(build,),
            config={"action": "replay", "target": target, "max_length": str(max_length),
                    "asan_options": _ASAN_OPTIONS},
            environment=tool_environment(
                toolchain,
                prepend_path=runtime,
                extra=(("ASAN_OPTIONS", _ASAN_OPTIONS),),
            ),
        )
        previous = prior.get(target)
        prior_uid, prior_path, prior_files = (
            ("", "", {})
            if previous is None
            else (previous[0].producer_uid, str(previous[1]), previous[2])
        )
        run = factory.make(
            "fuzz-run.ps1",
            f"run-fuzz-x64-{target}",
            "fuzz",
            {
                "pwsh": str(toolchain.pwsh), "fuzzer": str(fuzzer), "seed_dir": str(seed_dir),
                "max_length": max_length, "seconds": seconds, "prior_corpus": prior_path,
            },
            files=seed_files | prior_files, dependencies=(replay,),
            results=(
                Result(
                    f"reports/fuzz/x64/{target}/status.txt",
                    "fuzz", "text/plain", "status.txt",
                ),
                Result(
                    f"reports/fuzz/x64/{target}/corpus",
                    "corpus", "application/octet-stream", "corpus",
                ),
                Result(
                    f"reports/fuzz/x64/{target}/artifacts",
                    "evidence", "application/octet-stream", "artifacts",
                ),
            ),
            config={"action": "fuzz", "target": target, "max_length": str(max_length),
                    "seconds": str(seconds), "run_nonce": run_nonce,
                    "asan_options": _ASAN_OPTIONS, "prior_corpus_uid": prior_uid},
            environment=tool_environment(
                toolchain,
                prepend_path=runtime,
                extra=(("ASAN_OPTIONS", _ASAN_OPTIONS),),
            ),
        )
        gate = factory.make(
            "fuzz-gate.ps1",
            f"fuzz-x64-{target}",
            "fuzz",
            {"pwsh": str(toolchain.pwsh),
             "status": str(paths.cas(run.uid).output / "status.txt")},
            files={}, dependencies=(run,),
            config={"action": "fuzz-gate", "target": target},
        )
        nodes.extend((build, replay, run, gate))
        targets.append(gate.name)
    return Graph(
        tuple(nodes), tuple(targets),
        {"build": jobs, "fuzz": fuzz_jobs, "restore": 1, "slot": jobs},
    )

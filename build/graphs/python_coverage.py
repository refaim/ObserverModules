"""Content-addressed Python line-and-branch coverage gate."""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.graph import Graph, Result
from graphs.common import BUILD_ROOT, python_action, recipe_factory


def _inputs(repository: Path, build_root: Path) -> dict[str, bytes]:
    paths = [
        build_root / name
        for name in ("driver.py", "main.py", "pyproject.toml", "uv.lock")
    ]
    for directory in ("core", "graphs", "tests"):
        paths.extend(sorted((build_root / directory).rglob("*.py")))
    return {path.relative_to(repository).as_posix(): path.read_bytes() for path in paths}


def _tool_digest(build_root: Path, executable: Path) -> str:
    package = (build_root / ".venv/Lib/site-packages/coverage").resolve(strict=True)
    if not package.is_dir():
        raise FileNotFoundError(f"project coverage package is not a directory: {package}")
    paths = [executable] + [path for path in sorted(package.rglob("*"))
                            if path.is_file() and "__pycache__" not in path.parts]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(build_root).as_posix().encode() + b"\0")
        with path.open("rb") as stream:
            digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()


def python_coverage_graph(repository: Path) -> Graph:
    """Build one demand gate using only the repository-local pinned coverage executable."""

    root = repository.resolve(strict=True)
    build_root = root / "build"
    coverage = (build_root / ".venv/Scripts/coverage.exe").resolve(strict=True)
    if not coverage.is_file():
        raise FileNotFoundError(f"project coverage executable is not a file: {coverage}")
    digest = _tool_digest(build_root, coverage)
    factory = recipe_factory(BUILD_ROOT, {})
    gate = python_action(
        factory, "python-coverage", "core.python_coverage", (str(coverage), str(build_root)), (),
        pool="python-coverage", files=_inputs(root, build_root),
        identity={"coverage.path": str(coverage), "coverage.sha256": digest},
        config={"action": "python-coverage", "coverage": "100-percent-line-and-branch"},
        environment=(("PYTHONDONTWRITEBYTECODE", "1"),),
        results=(
            Result("reports/coverage/python/coverage.json", "coverage", "application/json", "coverage.json"),
            Result("reports/coverage/python/coverage.xml", "coverage", "application/xml", "coverage.xml"),
            Result("reports/coverage/python/coverage.txt", "coverage", "text/plain", "coverage.txt"),
            Result("reports/coverage/python/coverage.toml", "coverage-config", "application/toml", "coverage.toml"),
            Result("reports/coverage/python/coverage.data", "coverage-data", "application/octet-stream", ".coverage"),
        ),
    )
    return Graph((gate,), (gate.name,), {"python-coverage": 1})

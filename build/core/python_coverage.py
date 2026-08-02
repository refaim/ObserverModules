"""Run the project-local coverage.py gate and publish its native evidence."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
from collections.abc import Sequence


def _directory(name: str) -> Path:
    value = os.environ.get(name)
    if not value or not (path := Path(value)).is_dir():
        raise RuntimeError(f"{name} must name an existing directory")
    return path.resolve()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("coverage", type=Path)
    parser.add_argument("build_root", type=Path)
    args = parser.parse_args(argv)
    coverage = args.coverage.resolve(strict=True)
    build_root = args.build_root.resolve(strict=True)
    config = build_root / "pyproject.toml"
    if not coverage.is_file() or not build_root.is_dir() or not config.is_file():
        raise FileNotFoundError("coverage executable, build root, and config must exist")

    work, output = _directory("OBSERVER_BUILD_DIR"), _directory("OBSERVER_OUT_DIR")
    data = work / ".coverage"
    command = [str(coverage)]
    subprocess.run(command + ["run", "--rcfile", str(config), "--data-file", str(data),
                                "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
                   cwd=build_root, check=True)
    report = subprocess.run(
        command + ["report", "--rcfile", str(config), "--data-file", str(data), "--fail-under=100"],
        cwd=build_root, check=False, stdout=subprocess.PIPE, text=True,
    )
    (output / "coverage.txt").write_text(report.stdout, encoding="utf-8")
    subprocess.run(command + ["json", "--rcfile", str(config), "--data-file", str(data),
                              "--fail-under=0", "-o", str(output / "coverage.json")],
                   cwd=build_root, check=True)
    subprocess.run(command + ["xml", "--rcfile", str(config), "--data-file", str(data),
                              "--fail-under=0", "-o", str(output / "coverage.xml")],
                   cwd=build_root, check=True)
    shutil.copyfile(config, output / "coverage.toml")
    shutil.copyfile(data, output / data.name)
    print(report.stdout, end="")
    report.check_returncode()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

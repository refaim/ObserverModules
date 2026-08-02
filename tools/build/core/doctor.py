"""Read-only, fail-soft discovery of complete local build prerequisites."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
import sys
import tomllib

from core.quality_tools import discover_quality_tools, resolve_sanitizer_runtimes
from core.source_tools import discover_source_tools
from core.toolchain import discover_msvc_toolchain

_MSVC = (("msbuild_version", "MSBuild"), ("vc_tools_version", "MSVC"),
         ("clang_tidy_version", "LLVM"), ("windows_sdk_version", "SDK"))
_SOURCE = (("pwsh_version", "PowerShell"), ("clang_format_version", "clang-format"),
           ("cppcheck_version", "Cppcheck"), ("psscriptanalyzer_version", "PSScriptAnalyzer"))

@dataclass(frozen=True, slots=True)
class Probe:
    name: str
    status: str
    detail: str

def _python_version() -> str:
    config = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    required = config["project"]["requires-python"]
    actual = ".".join(map(str, sys.version_info[:3]))
    if required != f"=={actual}":
        raise RuntimeError(f"requires {required}, running {actual}")
    return actual

def _versions(value: object, fields: tuple[tuple[str, str], ...]) -> str:
    identity = dict(value.identity)  # type: ignore[attr-defined]
    return ", ".join(f"{label}={identity[key]}" for key, label in fields)

def _concise(error: Exception) -> str:
    return " ".join(str(error).split()) or type(error).__name__

def doctor_report() -> tuple[Probe, ...]:
    rows: list[Probe] = []

    def probe(name: str, action: Callable[[], object],
              detail: Callable[[object], str]) -> object | None:
        try:
            value = action()
            rows.append(Probe(name, "OK", detail(value)))
            return value
        except Exception as error:  # doctor must preserve the remaining independent probes
            rows.append(Probe(name, "MISSING", _concise(error)))
            return None

    def require_toolchain() -> object:
        if toolchain is None:
            raise RuntimeError("MSVC toolchain unavailable")
        return toolchain

    probe("python", _python_version, str)
    toolchain = probe("msvc", discover_msvc_toolchain, lambda value: _versions(value, _MSVC))
    probe("source-tools", lambda: discover_source_tools(require_toolchain()),
          lambda value: _versions(value, _SOURCE))
    probe("quality-tools", lambda: discover_quality_tools(require_toolchain()),
          lambda _value: "clang-cl, clang-scan-deps, llvm-cov, llvm-profdata, dumpbin, BinSkim, UMDH")
    probe("sanitizer-runtimes", lambda: resolve_sanitizer_runtimes(require_toolchain()),
          lambda _value: "ASan x86/x64, UBSan x64")
    return tuple(rows)

def main(argv: Sequence[str] | None = None) -> int:
    argparse.ArgumentParser(prog="observer-build doctor").parse_args(argv)
    report = doctor_report()
    print("probe\tstatus\tdetail")
    for item in report:
        print(f"{item.name}\t{item.status}\t{item.detail}")
    return int(any(item.status != "OK" for item in report))

if __name__ == "__main__":
    raise SystemExit(main())

"""PowerShell-compatible table-driven CLI for the local build DAG."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
import os
from pathlib import Path
import sys

from core.doctor import main as doctor_main
from core.host import verify_route
from core.quality_tools import resolve_binskim, resolve_dumpbin, resolve_umdh
from core.source_tools import discover_source_tools
from core.toolchain import discover_msvc_toolchain
from driver import Driver as BuildDriver


_ARCHITECTURES = ("x86", "x64", "arm64")
_CONFIGURATIONS = ("Debug", "Release")
_FUZZ_TARGETS = ("pickle", "renpy", "rpgmaker", "zanzarah")


def _selection(choices: tuple[str, ...]) -> Callable[[str], tuple[str, ...]]:
    def parse(value: str) -> tuple[str, ...]:
        parts = tuple(item.strip() for item in value.split(","))
        if any(item.casefold() == "all" for item in parts):
            return choices
        lookup = {item.casefold(): item for item in choices}
        try:
            selected = tuple(lookup[item.casefold()] for item in parts if item)
        except KeyError as error:
            raise argparse.ArgumentTypeError(f"expected all or comma-separated: {','.join(choices)}")
        if len(selected) != len(parts):
            raise argparse.ArgumentTypeError(f"expected all or comma-separated: {','.join(choices)}")
        return tuple(dict.fromkeys(selected))
    return parse


def _integer(minimum: int, maximum: int | None = None) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("expected an integer") from error
        if number < minimum or maximum is not None and number > maximum:
            limit = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
            raise argparse.ArgumentTypeError(f"expected {limit}")
        return number
    return parse


_OPTIONS: dict[str, tuple[tuple[str, ...], dict[str, object]]] = {
    "arch": (("-Arch", "--arch"), {"type": _selection(_ARCHITECTURES), "default": ("x64",)}),
    "config": (("-Config", "--config"), {"type": _selection(_CONFIGURATIONS), "default": ("Debug",)}),
    "corpus": (("-Corpus", "--corpus"), {"type": Path}),
    "restore": (("-RestoreFlavor", "--restore-flavor"), {
        "type": _selection(("default", "asan")), "default": ("default",),
    }),
    "shards": (("-TestShards", "--test-shards"), {"type": _integer(1), "default": 4}),
    "fuzz_seconds": (("-FuzzSeconds", "--fuzz-seconds"), {"type": _integer(1, 86400), "default": 60}),
    "fuzz_target": (("-FuzzTarget", "--fuzz-target"), {"type": _selection(_FUZZ_TARGETS), "default": _FUZZ_TARGETS}),
    "leak_warmup": (("-LeakWarmup", "--leak-warmup"), {"type": _integer(1, 1_000_000), "default": 8}),
    "leak_iterations": (("-LeakIterations", "--leak-iterations"), {"type": _integer(1, 1_000_000), "default": 100}),
    "leak_windows": (("-LeakWindows", "--leak-windows"), {"type": _integer(3, 10), "default": 3}),
    "leak_tolerance": (("-LeakToleranceBytes", "--leak-tolerance-bytes"), {
        "type": _integer(0, 1_073_741_824), "default": 0,
    }),
    "export_dir": (("-ExportDir", "--export-dir"), {"type": Path}),
    "clean_mode": (("-CleanMode", "--clean-mode"), {"choices": ("all", "stale-work"), "default": "all"}),
}

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="observer-build")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    for name, (options, _invoke) in _COMMANDS.items():
        command = commands.add_parser(name)
        command.add_argument("-Repository", "--repository", type=Path, default=Path(__file__).parents[1])
        command.add_argument("-Jobs", "--jobs", type=_integer(1))
        for option in options:
            flags, settings = _OPTIONS[option]
            command.add_argument(*flags, dest=option, **settings)
    clean = commands.add_parser("clean")
    clean.add_argument("-Repository", "--repository", type=Path, default=Path(__file__).parents[1])
    flags, settings = _OPTIONS["clean_mode"]
    clean.add_argument(*flags, dest="clean_mode", **settings)
    return parser


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"


def run_clean(repository: Path, mode: str) -> int:
    from core.clean import main as clean_main
    return clean_main((str(repository), "--mode", mode))


Invoker = Callable[[object, argparse.Namespace, object], Awaitable[tuple[Path, ...]]]


async def _restore(driver: object, args: argparse.Namespace, _toolchain: object) -> tuple[Path, ...]:
    outputs: tuple[Path, ...] = ()
    if "default" in args.restore:
        outputs += await driver.restore(args.arch, flavors=("",))
    if "asan" in args.restore:
        architectures = tuple(item for item in args.arch if item != "arm64")
        if architectures:
            outputs += await driver.restore(architectures, flavors=("asan",))
    return outputs


_COMMANDS: dict[str, tuple[tuple[str, ...], Invoker]] = {
    "restore": (("arch", "restore"), _restore),
    "build": (("arch", "config"), lambda driver, args, _toolchain: driver.build(args.arch, args.config)),
    "test": (("arch", "config", "corpus", "shards"), lambda driver, args, _toolchain: driver.test(
        args.arch, args.config, test_shards=args.shards, corpus=args.corpus, run_nonce=args.run_nonce
    )),
    "source-checks": (("arch",), lambda driver, args, toolchain: driver.source_checks(
        args.arch, discover_source_tools(toolchain)
    )),
    "compiler-analysis": (("arch",), lambda driver, args, _toolchain: driver.compiler_analysis(args.arch)),
    "test-coverage": (("arch", "corpus", "shards"), lambda driver, args, _toolchain: driver.test_coverage(
        args.arch, test_shards=args.shards, corpus=args.corpus, run_nonce=args.run_nonce
    )),
    "test-asan": (("arch", "shards"), lambda driver, args, _toolchain: driver.test_asan(args.arch, test_shards=args.shards)),
    "test-ubsan": (("arch", "shards"), lambda driver, args, _toolchain: driver.test_ubsan(args.arch, test_shards=args.shards)),
    "test-leaks": (("arch", "leak_warmup", "leak_iterations", "leak_windows", "leak_tolerance"), lambda driver, args, toolchain: driver.test_leaks(
        run_nonce=args.run_nonce, dumpbin=resolve_dumpbin(toolchain), binskim=resolve_binskim(),
        umdh=resolve_umdh(), warmup=args.leak_warmup, iterations=args.leak_iterations,
        windows=args.leak_windows, tolerance_bytes=args.leak_tolerance,
    )),
    "fuzz": (("arch", "fuzz_seconds", "fuzz_target"), lambda driver, args, _toolchain: driver.fuzz(
        run_nonce=args.run_nonce, seconds=args.fuzz_seconds, targets=args.fuzz_target
    )),
    "audit-binaries": (("arch",), lambda driver, args, toolchain: driver.audit(
        args.arch, dumpbin=resolve_dumpbin(toolchain), binskim=resolve_binskim()
    )),
    "package": (("arch", "export_dir"), lambda driver, args, toolchain: driver.package(
        args.arch, dumpbin=resolve_dumpbin(toolchain), binskim=resolve_binskim(),
        export_dir=args.export_dir,
    )),
    "verify-source": (("export_dir",), lambda driver, args, _toolchain: driver.verify_source(
        export_dir=args.export_dir,
    )),
    "verify-arch": (("arch", "corpus", "shards", "fuzz_seconds", "leak_warmup",
                     "leak_iterations", "leak_windows", "leak_tolerance", "export_dir"),
                    lambda driver, args, _toolchain: driver.verify_arch(
        args.arch, corpus=args.corpus, run_nonce=args.run_nonce, fuzz_seconds=args.fuzz_seconds,
        test_shards=args.shards, warmup=args.leak_warmup, iterations=args.leak_iterations,
        windows=args.leak_windows, tolerance_bytes=args.leak_tolerance,
        export_dir=args.export_dir,
    )),
    "verify": (("arch", "corpus", "shards", "fuzz_seconds", "leak_warmup",
                "leak_iterations", "leak_windows", "leak_tolerance", "export_dir"),
               lambda driver, args, _toolchain: driver.verify(
        args.arch, corpus=args.corpus, run_nonce=args.run_nonce, fuzz_seconds=args.fuzz_seconds,
        test_shards=args.shards, warmup=args.leak_warmup, iterations=args.leak_iterations,
        windows=args.leak_windows, tolerance_bytes=args.leak_tolerance,
        export_dir=args.export_dir,
    )),
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or len(arguments) == 1 and arguments[0].casefold() == "help":
        parser.print_help()
        return 0
    args = parser.parse_args(arguments)
    if args.command == "doctor":
        return doctor_main(())
    if args.command == "clean":
        return run_clean(args.repository, args.clean_mode)
    if args.command in {"fuzz", "test-leaks"} and args.arch != ("x64",):
        parser.error(f"{args.command} requires -Arch x64")
    if args.command == "verify-arch" and len(args.arch) != 1:
        parser.error("verify-arch requires exactly one architecture")
    args.run_nonce = _run_id()
    toolchain = discover_msvc_toolchain()
    driver = BuildDriver(args.repository, args.run_nonce, toolchain, jobs=args.jobs)
    outputs = asyncio.run(_COMMANDS[args.command][1](driver, args, toolchain))
    if args.command in {"verify", "verify-arch"}:
        for item in verify_route(args.arch).deferred:
            print(f"[DEFERRED] {item.gate} {item.architecture}: {item.reason}")
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

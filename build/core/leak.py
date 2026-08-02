"""Small process-safe worker for fine-grained UMDH leak nodes."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from filelock import FileLock
import psutil


MODES = ("operations", "lifecycle")
SCENARIOS = (
    "small-success", "malformed", "cancellation", "read-failure", "write-failure",
    "large-metadata", "sparse-metadata",
)
BINARIES = ("leak-probe.exe", "renpy.so", "rpgmaker.so", "zanzarah.so")


class LeakError(RuntimeError):
    pass


def _output() -> Path:
    value = os.environ.get("OBSERVER_OUT_DIR")
    if not value or not (output := Path(value)).is_dir():
        raise LeakError("OBSERVER_OUT_DIR must be an existing directory")
    return output


def _json(name: str, value: object) -> None:
    (_output() / name).write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _exact(action: str, args: Sequence[str], count: int) -> tuple[str, ...]:
    if len(args) != count:
        raise LeakError(f"{action} expects {count} arguments")
    return tuple(args)


def _file(value: str, name: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise LeakError(f"{name} was not found: {path}")
    return path


def _selection(mode: str, scenario: str) -> None:
    if mode not in MODES or scenario not in SCENARIOS:
        raise LeakError(f"invalid leak selection: {mode}/{scenario}")


def _count(value: str, name: str, *, minimum: int = 1) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise LeakError(f"{name} must be an integer") from error
    if result < minimum:
        raise LeakError(f"{name} must be at least {minimum}")
    return result


def _marker(lines: Sequence[str], marker: str) -> str:
    prefix = f"OBSERVER_LEAK_PROBE|{marker}|"
    found = [line for line in lines if line.startswith(prefix)]
    if len(found) != 1:
        raise LeakError(f"leak probe emitted {len(found)} {marker} markers")
    return found[0]


def _ready(line: str, mode: str, scenario: str, pid: int | None = None) -> None:
    expected = rf"^OBSERVER_LEAK_PROBE\|READY\|pid=([0-9]+)\|mode={re.escape(mode)}\|configuration=Release\|scenarios={re.escape(scenario)}$"
    match = re.fullmatch(expected, line)
    if match is None or (pid is not None and int(match.group(1)) != pid):
        raise LeakError("leak READY marker does not match the requested process/selection")


def _setup(args: Sequence[str]) -> None:
    sources = tuple(Path(value) for value in _exact("setup", args, len(BINARIES)))
    output, evidence = _output(), []
    for name, source in zip(BINARIES, sources, strict=True):
        if not source.is_file():
            raise LeakError(f"leak binary was not found: {source}")
        destination = output / name
        shutil.copyfile(source, destination)
        for symbol in source.parent.glob("*.pdb"):
            shutil.copyfile(symbol, output / symbol.name)
        with destination.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        evidence.append({"name": name, "path": str(destination), "sha256": digest})
    _json("release-binaries.json", {"architecture": "x64", "configuration": "Release", "runtimeLibrary": "MT_StaticRelease", "binaries": evidence})


def _preflight(args: Sequence[str]) -> None:
    directory, mode, scenario = _exact("preflight", args, 3)
    _selection(mode, scenario)
    probe = _file(str(Path(directory) / BINARIES[0]), "leak probe")
    command = [str(probe), "--automatic", "--mode", mode, "--scenario", scenario, "--warmup", "1", "--iterations", "1", "--windows", "3"]
    result = subprocess.run(command, cwd=directory, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace")
    if result.returncode:
        raise LeakError(f"leak preflight failed ({result.returncode}): {result.stdout}")
    lines = result.stdout.splitlines()
    ready = _marker(lines, "READY")
    _ready(ready, mode, scenario)
    _marker(lines, "DONE")
    _json("preflight.json", {"mode": mode, "scenario": scenario, "ready": ready})


def _read(process: psutil.Popen[str], marker: str, label: str = "") -> str:
    assert process.stdout is not None
    prefix = f"OBSERVER_LEAK_PROBE|{marker}|{label + '|' if label else ''}"
    while line := process.stdout.readline():
        if line.rstrip("\r\n").startswith(prefix):
            return line.rstrip("\r\n")
    raise LeakError(f"leak probe ended before {marker} marker")


def _kill_tree(process: psutil.Popen[str]) -> None:
    processes = [*process.children(recursive=True), process]
    for current in reversed(processes):
        try:
            current.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(processes, timeout=5)


def _run_gflags(gflags: Path, image: str, flag: str | None = None) -> subprocess.CompletedProcess[str]:
    command = [str(gflags), "/i", image]
    if flag is not None:
        command.append(flag)
    return subprocess.run(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace",
    )


def _change_stack_traces(gflags: Path, image: str, enabled: bool) -> None:
    flag = "+ust" if enabled else "-ust"
    result = _run_gflags(gflags, image, flag)
    if result.returncode:
        raise LeakError(f"GFlags {flag} failed ({result.returncode}): {result.stdout}")


def _enable_stack_traces(gflags: Path, image: str) -> bool:
    if not gflags.is_file():
        return False
    current = _run_gflags(gflags, image)
    if current.returncode:
        raise LeakError(f"GFlags query failed ({current.returncode}): {current.stdout}")
    match = re.search(r"are:\s*([0-9A-Fa-f]+)\s*$", current.stdout)
    if current.stdout.startswith("No Registry Settings for "):
        flags = 0
    elif match is not None:
        flags = int(match.group(1), 16)
    else:
        raise LeakError(f"GFlags returned an unrecognized setting: {current.stdout}")
    if flags & 0x1000:
        return False
    try:
        _change_stack_traces(gflags, image, True)
    except OSError as error:
        if getattr(error, "winerror", None) != 740:
            raise
        return False
    return True


def _snapshot(umdh: Path, pid: int, destination: Path, baseline: bool) -> None:
    result = subprocess.run([str(umdh), f"-p:{pid}", f"-f:{destination}"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace")
    text = destination.read_text(encoding="utf-8", errors="replace") if destination.is_file() else ""
    if baseline:
        if result.returncode not in (0, 1) or (result.returncode == 1 and "enabled allocation stack collection" not in text):
            raise LeakError(f"UMDH could not prime stack collection ({result.returncode}): {result.stdout}")
    elif result.returncode or re.search(r"didn't find any allocations|database is full|stack trace database.*full", text, re.I) or "BackTrace" not in text:
        raise LeakError(f"UMDH snapshot is unusable: {destination}")


def _capture(args: Sequence[str]) -> None:
    directory, umdh_value, mode, scenario, warmup_value, iterations_value, windows_value = _exact("capture", args, 7)
    _selection(mode, scenario)
    probe, umdh = _file(str(Path(directory) / BINARIES[0]), "leak probe"), _file(umdh_value, "UMDH")
    gflags = umdh.with_name("gflags.exe")
    warmup, iterations, windows = _count(warmup_value, "warmup"), _count(iterations_value, "iterations"), _count(windows_value, "windows", minimum=3)
    output, snapshot_dir = _output(), _output() / "snapshots"
    snapshot_dir.mkdir()
    environment = os.environ | {"_NT_SYMBOL_PATH": directory, "OANOCACHE": "1"}
    command = [str(probe), "--mode", mode, "--scenario", scenario, "--warmup", str(warmup), "--iterations", str(iterations), "--windows", str(windows)]
    error_path = output / "probe.stderr.log"
    process: psutil.Popen[str] | None = None
    with error_path.open("w+", encoding="utf-8") as errors:
        try:
            lock = FileLock(Path(tempfile.gettempdir()) / "observer-modules-gflags.lock")
            with lock:
                changed = _enable_stack_traces(gflags, probe.name)
                try:
                    process = psutil.Popen(command, cwd=directory, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8", errors="replace")
                finally:
                    if changed:
                        _change_stack_traces(gflags, probe.name, False)
            assert process is not None
            _ready(_read(process, "READY"), mode, scenario, process.pid)
            for label in ("baseline", *(f"window-{index}" for index in range(1, windows + 1))):
                line = _read(process, "SNAPSHOT", label)
                if not re.search(rf"\|pid={process.pid}\|", line):
                    raise LeakError("leak SNAPSHOT marker has the wrong PID")
                _snapshot(umdh, process.pid, snapshot_dir / f"{label}.txt", label == "baseline")
                assert process.stdin is not None
                process.stdin.write(f"continue|{label}\n")
                process.stdin.flush()
            _read(process, "DONE")
            try:
                result = process.wait(timeout=120)
            except psutil.TimeoutExpired as error:
                raise LeakError("leak probe did not exit after its final snapshot") from error
            if result:
                errors.flush(); errors.seek(0)
                raise LeakError(f"leak probe failed ({result}): {errors.read()}")
        finally:
            if process is not None:
                if process.poll() is None:
                    _kill_tree(process)
                assert process.stdin is not None and process.stdout is not None
                process.stdin.close()
                process.stdout.close()
    assert process is not None
    _json("capture.json", {"mode": mode, "scenario": scenario, "processId": process.pid, "windows": windows})


def _diff(args: Sequence[str]) -> None:
    umdh_value, directory, label, before, after = _exact("diff", args, 5)
    report = _output() / "report.txt"
    environment = os.environ | {"_NT_SYMBOL_PATH": directory, "OANOCACHE": "1"}
    result = subprocess.run([umdh_value, "-d", before, after, f"-f:{report}"], env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace")
    if result.returncode or not report.is_file():
        raise LeakError(f"UMDH comparison failed ({result.returncode}): {result.stdout}")
    text = report.read_text(encoding="utf-8", errors="replace")
    totals = list(re.finditer(r"^Total (increase|decrease)\s*==\s*([0-9]+)", text, re.M))
    if not totals:
        raise LeakError("UMDH comparison contains no total allocation delta")
    total = int(totals[-1].group(2)) * (-1 if totals[-1].group(1) == "decrease" else 1)
    stacks = {match.group(2): int(match.group(1)) for match in re.finditer(r"^\+\s+([0-9]+)\s+\([^)]*\)\s+[0-9]+\s+allocs\s+BackTrace\s*([0-9A-Fa-f]+)", text, re.M)}
    _json("diff.json", {"label": label, "totalIncrease": total, "positiveStacks": stacks, "report": "report.txt"})


def _summary(args: Sequence[str]) -> dict[str, object]:
    if len(args) < 8 or len(args) % 2:
        raise LeakError("judge expects settings followed by label/report pairs")
    mode, scenario, warmup_value, iterations_value, windows_value, tolerance_value, *pairs = args
    _selection(mode, scenario)
    warmup, iterations, windows = _count(warmup_value, "warmup"), _count(iterations_value, "iterations"), _count(windows_value, "windows", minimum=3)
    tolerance = _count(tolerance_value, "tolerance", minimum=0)
    labels, records = pairs[::2], [json.loads(Path(path).read_text(encoding="utf-8")) for path in pairs[1::2]]
    expected = [*(f"window-{index}" for index in range(1, windows)), "overall"]
    if labels != expected or [record.get("label") for record in records] != expected:
        raise LeakError("judge comparison labels do not match the measurement windows")
    previous, last, overall = records[-3], records[-2], records[-1]
    repeated = sorted(key for key, value in last["positiveStacks"].items() if value > tolerance and previous["positiveStacks"].get(key, 0) > tolerance)
    sustained = last["totalIncrease"] > tolerance and previous["totalIncrease"] > tolerance and overall["totalIncrease"] > 2 * tolerance
    summary = {"mode": mode, "scenario": scenario, "warmupRounds": warmup, "iterationsPerWindow": iterations, "windows": windows, "toleranceBytes": tolerance, "totalGrowthByWindow": [record["totalIncrease"] for record in records[:-1]], "overallGrowthBytes": overall["totalIncrease"], "repeatedGrowingStacks": repeated, "passed": not sustained and not repeated}
    _json("summary.json", summary)
    return summary


def _summarize(args: Sequence[str]) -> None:
    _summary(args)


def _judge(args: Sequence[str]) -> None:
    summary = _summary(args)
    if not summary["passed"]:
        raise LeakError("UMDH found sustained heap growth")


def _gate(args: Sequence[str]) -> None:
    (path,) = _exact("gate", args, 1)
    try:
        passed = json.loads(Path(path).read_text(encoding="utf-8"))["passed"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise LeakError("leak summary is invalid") from error
    if not isinstance(passed, bool):
        raise LeakError("leak summary is invalid")
    if not passed:
        raise LeakError("UMDH found sustained heap growth")


_ACTIONS = {
    "setup": _setup, "preflight": _preflight, "capture": _capture,
    "diff": _diff, "summarize": _summarize, "gate": _gate, "judge": _judge,
}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in _ACTIONS:
        raise LeakError("expected leak action: setup, preflight, capture, diff, or judge")
    _ACTIONS[arguments[0]](arguments[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

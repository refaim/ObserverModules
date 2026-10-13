"""Live UMDH leak probe worker.

``setup`` stages the built x64 Release binaries into the operation output and ``measure`` runs
one scenario under UMDH, compares consecutive windows and raises on repeated growth.  Only the
irreducible live-measurement logic lives here: the capture-and-judge run must share the session
that captured the snapshots, so it cannot be split into separate tool invocations.  The
surrounding CI orchestration is in :mod:`main`.
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading

from filelock import FileLock, Timeout as FileLockTimeout
import psutil


MODES = ("operations", "lifecycle")
SCENARIOS = (
    "small-success", "malformed", "cancellation", "read-failure", "write-failure",
    "large-metadata", "sparse-metadata",
)
BINARIES = ("leak-probe.exe", "renpy.so", "rpgmaker.so", "zanzarah.so", "godot.so")
# Every probe marker wait and every UMDH invocation is bounded so a stalled child cannot wedge CI.
_MARKER_TIMEOUT = 300.0
_UMDH_TIMEOUT = 300.0
# The GFlags query/change and the GFlags registry lock are equally bounded.
_GFLAGS_TIMEOUT = 60.0
_GFLAGS_LOCK_TIMEOUT = 60.0


class LeakError(RuntimeError):
    """A leak operation could not complete its contract, or found sustained growth."""


def _output() -> Path:
    value = os.environ.get("UNPACKRAT_OUT_DIR")
    if not value or not (output := Path(value)).is_dir():
        raise LeakError("UNPACKRAT_OUT_DIR must be an existing directory")
    return output


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


def _ready(line: str, mode: str, scenario: str, pid: int | None = None) -> None:
    expected = (
        rf"^UNPACKRAT_LEAK_PROBE\|READY\|pid=([0-9]+)\|mode={re.escape(mode)}"
        rf"\|configuration=Release\|scenarios={re.escape(scenario)}$"
    )
    match = re.fullmatch(expected, line)
    if match is None or (pid is not None and int(match.group(1)) != pid):
        raise LeakError("leak READY marker does not match the requested process/selection")


def _setup(args: Sequence[str]) -> None:
    sources = tuple(Path(value) for value in _exact("setup", args, len(BINARIES)))
    output = _output()
    for name, source in zip(BINARIES, sources, strict=True):
        if not source.is_file():
            raise LeakError(f"leak binary was not found: {source}")
        shutil.copyfile(source, output / name)
        for symbol in source.parent.glob("*.pdb"):
            shutil.copyfile(symbol, output / symbol.name)


def _read(
    process: psutil.Popen[str], marker: str, label: str = "", timeout: float = _MARKER_TIMEOUT
) -> str:
    """Return one probe marker line, read on a helper thread under a finite timeout."""

    stream = process.stdout
    assert stream is not None
    prefix = f"UNPACKRAT_LEAK_PROBE|{marker}|{label + '|' if label else ''}"
    found: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        while True:
            line = stream.readline()
            if not line:
                found.put(None)
                return
            text = line.rstrip("\r\n")
            if text.startswith(prefix):
                found.put(text)
                return

    threading.Thread(target=pump, daemon=True).start()
    try:
        result = found.get(timeout=timeout)
    except queue.Empty:
        raise LeakError(f"leak probe never reported the {marker} marker") from None
    if result is None:
        raise LeakError(f"leak probe ended before {marker} marker")
    return result


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
    try:
        return subprocess.run(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", timeout=_GFLAGS_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise LeakError(f"GFlags timed out after {_GFLAGS_TIMEOUT}s: {image}") from error


def _change_stack_traces(gflags: Path, image: str, enabled: bool) -> None:
    flag = "+ust" if enabled else "-ust"
    result = _run_gflags(gflags, image, flag)
    if result.returncode:
        raise LeakError(f"GFlags {flag} failed ({result.returncode}): {result.stdout}")


def _enable_stack_traces(gflags: Path, image: str) -> tuple[bool, bool]:
    """Report whether this call set ``+ust``, and whether traces are live from process start."""

    if not gflags.is_file():
        return False, False
    try:
        current = _run_gflags(gflags, image)
    except OSError as error:
        if getattr(error, "winerror", None) != 740:
            raise
        return False, False
    if current.returncode:
        raise LeakError(f"GFlags query failed ({current.returncode}): {current.stdout}")
    match = re.search(r"are:\s*([0-9A-Fa-f]{8}(?:\s*:\s*[0-9A-Fa-f]{8})*)\s*$", current.stdout)
    if current.stdout.startswith("No Registry Settings for "):
        flags = 0
    elif match is not None:
        flags = 0
        for value in match.group(1).split(":"):
            flags |= int(value.strip(), 16)
    else:
        raise LeakError(f"GFlags returned an unrecognized setting: {current.stdout}")
    if flags & 0x1000:
        return False, True
    try:
        _change_stack_traces(gflags, image, True)
    except OSError as error:
        if getattr(error, "winerror", None) != 740:
            raise
        return False, False
    return True, True


def _umdh_run(
    argv: Sequence[str], environment: dict[str, str], destination: Path
) -> subprocess.CompletedProcess[str]:
    """Run one UMDH invocation under a finite timeout, surfacing a stall as ``LeakError``."""

    try:
        return subprocess.run(
            argv, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", timeout=_UMDH_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise LeakError(f"UMDH timed out after {_UMDH_TIMEOUT}s: {destination}") from error


def _snapshot(
    umdh: Path, pid: int, destination: Path, baseline: bool, environment: dict[str, str]
) -> None:
    result = _umdh_run([str(umdh), f"-p:{pid}", f"-f:{destination}"], environment, destination)
    text = destination.read_text(encoding="utf-8", errors="replace") if destination.is_file() else ""
    if baseline:
        if result.returncode not in (0, 1) or (
            result.returncode == 1 and "enabled allocation stack collection" not in text
        ):
            raise LeakError(f"UMDH could not prime stack collection ({result.returncode}): {result.stdout}")
    elif (
        result.returncode
        or re.search(r"didn't find any allocations|database is full|stack trace database.*full", text, re.I)
        or "BackTrace" not in text
    ):
        raise LeakError(f"UMDH snapshot is unusable: {destination}")


def _capture(
    directory: str, umdh_value: str, mode: str, scenario: str,
    warmup: int, iterations: int, windows: int,
) -> Path:
    """Run one probe under UMDH and return the directory of its window snapshots."""

    probe, umdh = _file(str(Path(directory) / BINARIES[0]), "leak probe"), _file(umdh_value, "UMDH")
    gflags = umdh.with_name("gflags.exe")
    output, snapshot_dir = _output(), _output() / "snapshots"
    snapshot_dir.mkdir(exist_ok=True)
    environment = os.environ | {"_NT_SYMBOL_PATH": directory, "OANOCACHE": "1"}
    command = [
        str(probe), "--mode", mode, "--scenario", scenario,
        "--warmup", str(warmup), "--iterations", str(iterations), "--windows", str(windows),
    ]
    error_path = output / "probe.stderr.log"
    process: psutil.Popen[str] | None = None
    with error_path.open("w+", encoding="utf-8") as errors:
        try:
            lock = FileLock(
                Path(tempfile.gettempdir()) / "unpackrat-gflags.lock", timeout=_GFLAGS_LOCK_TIMEOUT
            )
            try:
                with lock:
                    changed, _ = _enable_stack_traces(gflags, probe.name)
                    try:
                        process = psutil.Popen(
                            command, cwd=directory, env=environment, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8", errors="replace",
                        )
                    finally:
                        if changed:
                            _change_stack_traces(gflags, probe.name, False)
            except FileLockTimeout as error:
                raise LeakError(
                    f"could not acquire the GFlags lock within {_GFLAGS_LOCK_TIMEOUT}s"
                ) from error
            assert process is not None
            _ready(_read(process, "READY"), mode, scenario, process.pid)
            for label in ("baseline", *(f"window-{index}" for index in range(1, windows + 1))):
                line = _read(process, "SNAPSHOT", label)
                if not re.search(rf"\|pid={process.pid}\|", line):
                    raise LeakError("leak SNAPSHOT marker has the wrong PID")
                _snapshot(umdh, process.pid, snapshot_dir / f"{label}.txt", label == "baseline", environment)
                assert process.stdin is not None
                process.stdin.write(f"continue|{label}\n")
                process.stdin.flush()
            _read(process, "DONE")
            try:
                result = process.wait(timeout=120)
            except psutil.TimeoutExpired as error:
                raise LeakError("leak probe did not exit after its final snapshot") from error
            if result:
                errors.flush()
                errors.seek(0)
                raise LeakError(f"leak probe failed ({result}): {errors.read()}")
        finally:
            if process is not None:
                if process.poll() is None:
                    _kill_tree(process)
                assert process.stdin is not None and process.stdout is not None
                process.stdin.close()
                process.stdout.close()
    return snapshot_dir


def _diff(umdh_value: str, directory: str, label: str, before: Path, after: Path, destination: Path) -> dict[str, object]:
    """Compare one snapshot pair and record its report beside the parsed growth."""

    report = destination / f"{label}.txt"
    environment = os.environ | {"_NT_SYMBOL_PATH": directory, "OANOCACHE": "1"}
    result = _umdh_run([umdh_value, "-d", str(before), str(after), f"-f:{report}"], environment, report)
    if result.returncode or not report.is_file():
        raise LeakError(f"UMDH comparison failed ({result.returncode}): {result.stdout}")
    text = report.read_text(encoding="utf-8", errors="replace")
    totals = list(re.finditer(r"^Total (increase|decrease)\s*==\s*([0-9]+)", text, re.M))
    if not totals:
        raise LeakError("UMDH comparison contains no total allocation delta")
    total = int(totals[-1].group(2)) * (-1 if totals[-1].group(1) == "decrease" else 1)
    stacks = {
        match.group(2): int(match.group(1))
        for match in re.finditer(r"^\+\s+([0-9]+)\s+\([^)]*\)\s+[0-9]+\s+allocs\s+BackTrace\s*([0-9A-Fa-f]+)", text, re.M)
    }
    return {"label": label, "totalIncrease": total, "positiveStacks": stacks, "report": report.name}


def _summary(
    mode: str, scenario: str, warmup: int, iterations: int, windows: int,
    tolerance: int, records: Sequence[dict[str, object]],
) -> bool:
    previous, last, overall = records[-3], records[-2], records[-1]
    repeated = sorted(
        key for key, value in last["positiveStacks"].items()
        if value > tolerance and previous["positiveStacks"].get(key, 0) > tolerance
    )
    sustained = (
        last["totalIncrease"] > tolerance and previous["totalIncrease"] > tolerance
        and overall["totalIncrease"] > 2 * tolerance
    )
    passed = not sustained and not repeated
    (_output() / "summary.json").write_text(
        json.dumps(
            {
                "mode": mode, "scenario": scenario, "warmupRounds": warmup,
                "iterationsPerWindow": iterations, "windows": windows, "toleranceBytes": tolerance,
                "totalGrowthByWindow": [record["totalIncrease"] for record in records[:-1]],
                "overallGrowthBytes": overall["totalIncrease"], "repeatedGrowingStacks": repeated,
                "passed": passed,
            },
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    return passed


def _measure(args: Sequence[str]) -> None:
    """Capture one scenario, diff every window pair and fail on sustained growth."""

    directory, umdh_value, mode, scenario, *values = _exact("measure", args, 8)
    _selection(mode, scenario)
    warmup, iterations = _count(values[0], "warmup"), _count(values[1], "iterations")
    windows, tolerance = _count(values[2], "windows", minimum=3), _count(values[3], "tolerance", minimum=0)
    snapshots = _capture(directory, umdh_value, mode, scenario, warmup, iterations, windows)
    destination = _output() / "diffs"
    destination.mkdir(exist_ok=True)
    specs = [
        (f"window-{index}", snapshots / f"window-{index}.txt", snapshots / f"window-{index + 1}.txt")
        for index in range(1, windows)
    ]
    specs.append(("overall", snapshots / "window-1.txt", snapshots / f"window-{windows}.txt"))
    records = [_diff(umdh_value, directory, label, before, after, destination) for label, before, after in specs]
    if not _summary(mode, scenario, warmup, iterations, windows, tolerance, records):
        raise LeakError("UMDH found sustained heap growth")


_ACTIONS = {"setup": _setup, "measure": _measure}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in _ACTIONS:
        raise LeakError("expected leak action: setup or measure")
    _ACTIONS[arguments[0]](arguments[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

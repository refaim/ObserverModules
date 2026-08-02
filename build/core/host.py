"""Windows host architecture and local test execution policy."""

from __future__ import annotations

from dataclasses import dataclass
import platform


_MACHINE_ARCHITECTURES = {
    "amd64": "x64",
    "x86_64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "x86": "x86",
    "i386": "x86",
    "i686": "x86",
}
_RUNNABLE = {
    "x86": frozenset({"x86"}),
    "x64": frozenset({"x86", "x64"}),
    "arm64": frozenset({"x86", "x64", "arm64"}),
}


@dataclass(frozen=True, slots=True)
class DeferredGate:
    gate: str
    architecture: str
    reason: str


@dataclass(frozen=True, slots=True)
class VerifyRoute:
    runnable: tuple[str, ...]
    coverage: tuple[str, ...]
    asan: tuple[str, ...]
    ubsan: tuple[str, ...]
    deferred: tuple[DeferredGate, ...]

    @property
    def run_x64_specialists(self) -> bool:
        return bool(self.ubsan)


def detect_host_architecture(machine: str | None = None) -> str:
    value = platform.machine() if machine is None else machine
    try:
        return _MACHINE_ARCHITECTURES[value.casefold()]
    except KeyError as error:
        raise RuntimeError(f"unsupported Windows host architecture: {value}") from error


def runnable_architectures(
    requested: tuple[str, ...], host_architecture: str | None = None
) -> tuple[str, ...]:
    host = detect_host_architecture() if host_architecture is None else host_architecture
    if host not in _RUNNABLE:
        raise ValueError(f"unsupported host architecture: {host}")
    unsupported = set(requested) - _RUNNABLE.keys()
    if unsupported:
        raise ValueError(f"unsupported requested architecture: {sorted(unsupported)[0]}")
    return tuple(architecture for architecture in requested if architecture in _RUNNABLE[host])


def require_runnable(
    requested: tuple[str, ...], host_architecture: str | None = None
) -> tuple[str, ...]:
    runnable = runnable_architectures(requested, host_architecture)
    if runnable != requested:
        missing = next(architecture for architecture in requested if architecture not in runnable)
        host = detect_host_architecture() if host_architecture is None else host_architecture
        raise RuntimeError(f"cannot run {missing} tests on {host} host")
    return runnable


def verify_route(
    requested: tuple[str, ...], host_architecture: str | None = None
) -> VerifyRoute:
    """Route locally executable verify work and preserve explicit deferrals."""

    runnable = runnable_architectures(requested, host_architecture)
    missing = tuple(item for item in requested if item not in runnable)
    deferred = [
        DeferredGate(gate, architecture, f"host cannot execute {architecture} {gate}")
        for architecture in missing
        for gate in ("tests", "package-runtime")
    ]
    specialists = (
        ("coverage", ("x64",)),
        ("asan", ("x86", "x64")),
        ("ubsan", ("x64",)),
        ("leaks", ("x64",)),
        ("fuzz", ("x64",)),
    )
    deferred.extend(
        DeferredGate(gate, architecture, f"host cannot execute {architecture} {gate}")
        for gate, supported in specialists
        for architecture in missing
        if architecture in supported
    )
    return VerifyRoute(
        runnable,
        tuple(item for item in runnable if item == "x64"),
        tuple(item for item in runnable if item in {"x86", "x64"}),
        tuple(item for item in runnable if item == "x64"),
        tuple(deferred),
    )

"""Out-of-process verifier for the leak-probe fatal-report fixtures.

The Catch2 fixtures in ``leak_probe_errors.cpp`` run the probe entry point with a
throwing C++ streambuf installed and judge only the in-process invariants: the
entry point returns ``EXIT_FAILURE`` and never lets the report exception escape.
A Streams-only Catch2 build cannot capture the probe's C stdio at the
file-descriptor level, so the exact report bytes are checked here, across a
process boundary, by running the built harness once per fatal handler case.

Each case selects exactly one Catch2 test case by name, so the probe's report is
the only protocol traffic the process can produce.  The two fatal cases must
write their exact ``UNPACKRAT_LEAK_PROBE|ERROR|...`` line to the real stderr and
must never write a ``UNPACKRAT_LEAK_PROBE|READY`` line to stdout; the helper
self-check must exit cleanly and write no protocol report at all.

Usage:
    python build/tests/native/check_leak_probe_errors.py <path-to-tests.exe>

The child inherits this process's environment unchanged, so an instrumented
build keeps its ``LLVM_PROFILE_FILE`` ``%p`` template and the verifier never
mutates global state.
"""

from __future__ import annotations

import argparse
import subprocess
from typing import NamedTuple


PROTOCOL_MARKER = b"UNPACKRAT_LEAK_PROBE"
READY_MARKER = b"UNPACKRAT_LEAK_PROBE|READY"
_TIMEOUT_SECONDS = 30


class _Case(NamedTuple):
    """One Catch2 case: its exact name and the stderr it must (or must not) emit.

    ``expected_stderr is None`` selects the helper self-check, which never
    routes a protocol report.  Any other value is the exact report the probe
    must have written to its real stderr, newline included.
    """

    name: str
    expected_stderr: bytes | None


# The three fixtures, in the order ``leak_probe_errors.cpp`` declares them.  The
# names must match the TEST_CASE strings byte for byte.
_CASES = (
    _Case(
        "leak-probe reports an invalid command line from the std::exception handler",
        b"UNPACKRAT_LEAK_PROBE|ERROR|Unknown option: --bogus\n",
    ),
    _Case(
        "leak-probe reports an unknown failure from the catch-all handler",
        b"UNPACKRAT_LEAK_PROBE|ERROR|unknown failure\n",
    ),
    _Case(
        "leak-probe fault injection throws a non-std exception and restores the stream",
        None,
    ),
)


def _run_case(harness: str, name: str) -> subprocess.CompletedProcess[bytes]:
    """Run one Catch2 case under the compact reporter, inheriting the environment."""

    # No ``env=`` argument: the child keeps ``LLVM_PROFILE_FILE`` (with its
    # ``%p`` template) and every other variable exactly as this process sees it.
    return subprocess.run(
        [harness, name, "--reporter", "compact"],
        capture_output=True,
        timeout=_TIMEOUT_SECONDS,
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", help="path to the built native leak-probe test harness (tests.exe)")
    arguments = parser.parse_args(argv)

    for case in _CASES:
        completed = _run_case(arguments.harness, case.name)
        # The probe writes '\n' through the C++ streams; the Windows text-mode
        # stdio layer may translate it to CRLF at the pipe, so compare content
        # with the line ending normalised to a single LF.
        stderr = completed.stderr.replace(b"\r\n", b"\n")

        # These gates are explicit raises, not bare ``assert`` statements: under
        # ``python -O`` an assertion would be stripped and a wrong or missing
        # report would silently pass.
        if READY_MARKER in completed.stdout:
            raise AssertionError(f"{case.name!r} unexpectedly wrote a READY protocol line to stdout")
        if case.expected_stderr is None:
            if PROTOCOL_MARKER in stderr:
                raise AssertionError(
                    f"the helper self-check {case.name!r} emitted a protocol report on stderr: {stderr!r}"
                )
            if PROTOCOL_MARKER in completed.stdout:
                raise AssertionError(
                    f"the helper self-check {case.name!r} emitted a protocol report on stdout: "
                    f"{completed.stdout!r}"
                )
        elif stderr != case.expected_stderr:
            raise AssertionError(
                f"{case.name!r} wrote the wrong stderr report: {stderr!r} != {case.expected_stderr!r}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

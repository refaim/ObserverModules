"""Unit tests for the out-of-process leak-probe report verifier.

``build/tests/native/check_leak_probe_errors.py`` is the production verifier that runs the
built native harness once per fatal handler case and checks the exact protocol bytes the probe
writes across the process boundary.  These tests drive the real verifier with ``subprocess.run``
faked, so no harness is built or launched: they pin the protocol it accepts, the exact
subprocess contract it uses, and every failure it must turn into a loud assertion.

Run just this module (the interpreter the driver ships is the project venv)::

    build/.venv/Scripts/python.exe -m unittest build.tests.test_leak_error_verifier

Or, from ``build``, under the same discovery the driver uses::

    <venv-python> -m unittest discover -s tests -p test_leak_error_verifier.py

Measure the verifier's own lines and branches (it must stay at 100%).  The driver's
``build/pyproject.toml`` already sets ``source``/``include`` for its own ``main.py``/``leaks.py``
scope, so the verifier is scored with a dedicated rcfile; run from ``build``::

    $env:COVERAGE_RCFILE = "$PWD/../out/evidence/leak-error-profile/coverage-verifier.rc"
    <venv-python> -m coverage run -m unittest discover -s tests -p test_leak_error_verifier.py
    <venv-python> -m coverage report -m
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


_VERIFIER_PATH = Path(__file__).resolve().parent / "native" / "check_leak_probe_errors.py"


def _load_verifier():
    """Import the production verifier by path without a bespoke loader framework."""

    spec = importlib.util.spec_from_file_location("check_leak_probe_errors_under_test", _VERIFIER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verifier = _load_verifier()


class FakeRun:
    """A ``subprocess.run`` stand-in that answers per selected Catch2 case name.

    The mapping keys are the case names the verifier will request; each value is either a
    ``(stdout, stderr)`` byte pair or an exception instance to raise, so a test can drive the
    correct protocol or any single wrong byte without a real child process.
    """

    def __init__(self, mapping) -> None:
        self._mapping = dict(mapping)
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(self, argv, **kwargs):
        argv = tuple(str(item) for item in argv)
        self.calls.append((argv, kwargs))
        outcome = self._mapping[argv[1]]
        if isinstance(outcome, BaseException):
            raise outcome
        stdout, stderr = outcome
        return subprocess.CompletedProcess(argv, 0, stdout, stderr)


def _good_mapping(*, line_ending: bytes = b"\n") -> dict[str, object]:
    """The byte-for-byte correct response for each of the three declared cases."""

    mapping: dict[str, object] = {}
    for case in verifier._CASES:
        if case.expected_stderr is None:
            mapping[case.name] = (b"", b"")
        else:
            mapping[case.name] = (b"", case.expected_stderr.replace(b"\n", line_ending))
    return mapping


class VerifierProtocolCase(unittest.TestCase):
    """What the verifier accepts and rejects across the process boundary."""

    HARNESS = r"C:\fake\stage\tests.exe"

    def _run(self, run: FakeRun) -> int:
        with mock.patch.object(subprocess, "run", run):
            return verifier.main([self.HARNESS])

    def _wrong_name(self) -> str:
        return verifier._CASES[0].name

    def test_accepts_all_three_cases_and_returns_zero(self) -> None:
        run = FakeRun(_good_mapping())
        self.assertEqual(self._run(run), 0)
        self.assertEqual([call[0][1] for call in run.calls], [case.name for case in verifier._CASES])

    def test_normalises_crlf_line_endings_before_comparing(self) -> None:
        # The Windows text-mode stdio layer may translate the probe's '\n' to CRLF at the pipe;
        # the verifier must still accept the report.
        run = FakeRun(_good_mapping(line_ending=b"\r\n"))
        self.assertEqual(self._run(run), 0)
        self.assertEqual(len(run.calls), 3)

    def test_rejects_a_wrong_std_exception_report(self) -> None:
        mapping = _good_mapping()
        mapping[self._wrong_name()] = (b"", b"UNPACKRAT_LEAK_PROBE|ERROR|not the expected text\n")
        with self.assertRaises(AssertionError) as raised:
            self._run(FakeRun(mapping))
        self.assertIn(self._wrong_name(), str(raised.exception))

    def test_rejects_a_wrong_unknown_failure_report(self) -> None:
        name = verifier._CASES[1].name
        mapping = _good_mapping()
        mapping[name] = (b"", b"UNPACKRAT_LEAK_PROBE|ERROR|some other failure\n")
        with self.assertRaises(AssertionError) as raised:
            self._run(FakeRun(mapping))
        self.assertIn(name, str(raised.exception))

    def test_rejects_a_protocol_report_from_the_helper_self_check(self) -> None:
        helper = verifier._CASES[2]
        self.assertIsNone(helper.expected_stderr)
        mapping = _good_mapping()
        mapping[helper.name] = (b"", b"UNPACKRAT_LEAK_PROBE|ERROR|unexpected\n")
        with self.assertRaises(AssertionError) as raised:
            self._run(FakeRun(mapping))
        self.assertIn(helper.name, str(raised.exception))

    def test_rejects_a_protocol_report_from_the_helper_on_stdout(self) -> None:
        # The helper self-check must stay silent on *both* streams; a protocol report on stdout
        # is just as illegal as one on stderr and must not slip through.
        helper = verifier._CASES[2]
        self.assertIsNone(helper.expected_stderr)
        mapping = _good_mapping()
        mapping[helper.name] = (b"UNPACKRAT_LEAK_PROBE|ERROR|leaked to stdout\n", b"")
        with self.assertRaises(AssertionError) as raised:
            self._run(FakeRun(mapping))
        self.assertIn(helper.name, str(raised.exception))

    def test_rejects_any_ready_line_on_stdout(self) -> None:
        # A READY line is illegal for every case, so even a byte-correct stderr must fail.
        mapping = _good_mapping()
        mapping[self._wrong_name()] = (b"UNPACKRAT_LEAK_PROBE|READY\n", verifier._CASES[0].expected_stderr)
        with self.assertRaises(AssertionError) as raised:
            self._run(FakeRun(mapping))
        self.assertIn("READY", str(raised.exception))


class VerifierOptimizedInterpreterCase(unittest.TestCase):
    """The protocol gate must hold under ``python -O``, where bare ``assert`` statements vanish."""

    def test_optimized_interpreter_still_rejects_a_wrong_report(self) -> None:
        # Drive the real verifier through an interpreter started with ``-O`` and a child that
        # always emits a wrong protocol report.  With the gate written as a stdlib ``assert``
        # this exits 0 under ``-O``; an explicit raise keeps it failing, so the check survives
        # optimized bytecode rather than an in-process mock.
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory) / "wrong_report_harness.cmd"
            harness.write_bytes(
                b"@echo off\r\n"
                b"echo UNPACKRAT_LEAK_PROBE^|ERROR^|not the expected report 1>&2\r\n"
            )
            completed = subprocess.run(
                [sys.executable, "-O", str(_VERIFIER_PATH), str(harness)],
                capture_output=True,
                timeout=60,
            )
        self.assertNotEqual(
            completed.returncode, 0, completed.stdout.decode(errors="replace") + completed.stderr.decode(errors="replace")
        )
        self.assertIn(b"wrong stderr report", completed.stderr)


class VerifierSubprocessContractCase(unittest.TestCase):
    """The exact child invocation is part of the verifier's contract."""

    HARNESS = r"C:\fake\stage\tests.exe"

    def test_child_runs_compact_reporter_with_exact_flags_and_inherited_environment(self) -> None:
        run = FakeRun(_good_mapping())
        before = dict(os.environ)
        with mock.patch.object(subprocess, "run", run):
            self.assertEqual(verifier.main([self.HARNESS]), 0)

        self.assertEqual(dict(os.environ), before, "the verifier must not mutate the parent environment")
        for (argv, kwargs), case in zip(run.calls, verifier._CASES):
            self.assertEqual(argv, (self.HARNESS, case.name, "--reporter", "compact"))
            self.assertEqual(kwargs, {"capture_output": True, "timeout": 30, "check": True})
            self.assertNotIn("env", kwargs, "the child must inherit the parent environment unchanged")

    def test_propagates_a_child_timeout(self) -> None:
        mapping = _good_mapping()
        mapping[verifier._CASES[0].name] = subprocess.TimeoutExpired(cmd=self.HARNESS, timeout=30)
        run = FakeRun(mapping)
        with self.assertRaises(subprocess.TimeoutExpired):
            with mock.patch.object(subprocess, "run", run):
                verifier.main([self.HARNESS])
        self.assertEqual(len(run.calls), 1, "the verifier must stop at the first failing child")

    def test_propagates_a_nonzero_child_return(self) -> None:
        mapping = _good_mapping()
        mapping[verifier._CASES[0].name] = subprocess.CalledProcessError(returncode=1, cmd=self.HARNESS)
        with self.assertRaises(subprocess.CalledProcessError):
            with mock.patch.object(subprocess, "run", FakeRun(mapping)):
                verifier.main([self.HARNESS])


class VerifierEntryPointCase(unittest.TestCase):
    """The ``python check_leak_probe_errors.py`` guard behaves like any argparse CLI."""

    def test_main_guard_parses_argv_and_help_exits_cleanly(self) -> None:
        with mock.patch.object(sys, "argv", [str(_VERIFIER_PATH), "--help"]):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    runpy.run_path(str(_VERIFIER_PATH), run_name="__main__")
        self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

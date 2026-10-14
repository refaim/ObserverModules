"""Contract of the fatal directory-cleanup probe in ``build/main.py``.

``test::support::temporary_directory`` (a C++ test support helper) removes a populated
directory through the error-code ``std::filesystem::remove_all`` overload while its destructor
is implicitly ``noexcept``.  An allocation failure inside that cleanup is not a filesystem
error, so it still escapes as ``std::bad_alloc`` and terminates the process.  The observable
contract is judged across a process boundary: ``main._cleanup_fatal_probe`` launches the hidden
Catch2 case (``src/tests/unit/image_codecs.cpp``) in a child ``tests.exe`` under an owned
temporary root and requires the exact fatal exit code together with the directory the child
could not remove.

The process management lives here, in Python, so path handling stays Unicode-correct; the C++
child only produces the fatal exit and the leftover fixture.  These tests are written before
the implementation: ``main._cleanup_fatal_probe`` does not exist yet, so every case below fails
with the expected missing-attribute error until the probe lands.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import main  # noqa: E402


# The exact hidden Catch2 case the probe must select; it must match the TEST_CASE name in
# src/tests/unit/image_codecs.cpp byte for byte.
_HIDDEN_CASE = (
    "directory cleanup: an allocation failure while removing a populated directory stays fatal"
)
# The fatal exit code the child installs through std::terminate -> std::exit, distinct from
# Catch2's own 0..4 codes and from the _Exit(86) used elsewhere.
_FATAL_EXIT = 90
# The marker every fixture directory the child leaves behind carries, from
# test::support::unused_path("cleanup-fatal").
_DIRECTORY_PREFIX = "unpackrat-lazy-cleanup-fatal-"


_FULL_COVERAGE = {
    "data": [
        {
            "totals": {
                "lines": {"count": 10, "covered": 10},
                "branches": {"count": 4, "covered": 4},
            }
        }
    ]
}


_LCOV = "SF:src/marker.cpp\nDA:10,1\nend_of_record\n"


class FakeChild:
    """A ``subprocess.run`` stand-in that materialises the fatal child's leftovers.

    The probe launches the child with the owned temporary root in ``TEMP``/``TMP``; this fake
    reads that root, writes the fixture directory the real child could not remove, and returns
    the exit code the real child would produce.
    """

    def __init__(
        self, *, returncode: int = _FATAL_EXIT, directories: int = 1,
        prefix: str = _DIRECTORY_PREFIX, nested: bytes | None = b"nested",
        create: bool = True, error: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.returncode = returncode
        self.directories = directories
        self.prefix = prefix
        self.nested = nested
        self.create = create
        self.error = error

    def __call__(self, argv, **kwargs):
        recorded = tuple(str(item) for item in argv)
        self.calls.append((recorded, kwargs))
        if self.error is not None:
            raise self.error
        if self.create and self.returncode == _FATAL_EXIT:
            root = Path(kwargs["env"]["TEMP"])
            for index in range(self.directories):
                fixture = root / f"{self.prefix}4242-{index}.tmp"
                fixture.mkdir(parents=True, exist_ok=True)
                if self.nested is not None:
                    (fixture / "nested.bin").write_bytes(self.nested)
        return subprocess.CompletedProcess(list(recorded), self.returncode, b"", b"")


class UnremovableTemporaryDirectory(tempfile.TemporaryDirectory):
    """A real temporary root whose cleanup fails, so the probe must not swallow the error."""

    instances: list["UnremovableTemporaryDirectory"] = []

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        UnremovableTemporaryDirectory.instances.append(self)

    def cleanup(self) -> None:
        raise OSError("the owned temporary root could not be removed")


class CleanupFatalProbeCase(unittest.TestCase):
    """The probe's own behaviour: one child, one exit code, one leftover fixture."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stage = self.root / "stage"
        self.stage.mkdir()
        (self.stage / "tests.exe").write_bytes(b"MZ")

    def _probe(self, run: FakeChild, *, env: dict[str, str] | None = None):
        with mock.patch.object(subprocess, "run", run):
            return main._cleanup_fatal_probe(self.stage, env=env)

    def test_probe_runs_the_hidden_case_under_an_owned_unicode_root(self) -> None:
        run = FakeChild()
        before = dict(os.environ)
        self._probe(run, env={"LLVM_PROFILE_FILE": "unit-%p.profraw"})
        self.assertEqual(dict(os.environ), before, "the probe must not mutate the parent environment")

        self.assertEqual(len(run.calls), 1)
        argv, kwargs = run.calls[0]
        self.assertEqual(Path(argv[0]), self.stage / "tests.exe")
        self.assertIn(_HIDDEN_CASE, argv)
        self.assertEqual(Path(kwargs["cwd"]), self.stage)
        self.assertEqual(kwargs["timeout"], 30)
        self.assertFalse(kwargs["check"], "the probe decides on the exit code itself")
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

        child_env = kwargs["env"]
        self.assertEqual(child_env["LLVM_PROFILE_FILE"], "unit-%p.profraw")
        self.assertEqual(child_env["TMP"], child_env["TEMP"], "both temp variables must name the owned root")
        owned = Path(child_env["TEMP"])
        self.assertTrue(
            any(ord(character) > 0x7F for character in owned.name),
            "the owned temporary root must carry a Unicode prefix",
        )
        self.assertFalse(owned.exists(), "the owned temporary root must be removed again")

    def test_probe_rejects_a_child_that_did_not_terminate_fatally(self) -> None:
        run = FakeChild(returncode=0)
        with self.assertRaises(main.BuildError):
            self._probe(run)

    def test_probe_propagates_a_child_timeout(self) -> None:
        run = FakeChild(error=subprocess.TimeoutExpired(cmd="tests.exe", timeout=30))
        with self.assertRaises(subprocess.TimeoutExpired):
            self._probe(run)

    def test_probe_requires_the_leftover_directory(self) -> None:
        run = FakeChild(create=False)
        with self.assertRaises(main.BuildError):
            self._probe(run)

    def test_probe_rejects_more_than_one_leftover_directory(self) -> None:
        run = FakeChild(directories=2)
        with self.assertRaises(main.BuildError):
            self._probe(run)

    def test_probe_ignores_a_directory_without_the_fixture_marker(self) -> None:
        run = FakeChild(prefix="unpackrat-lazy-cleanup-other-")
        with self.assertRaises(main.BuildError):
            self._probe(run)

    def test_probe_requires_the_nested_fixture_contents(self) -> None:
        run = FakeChild(nested=b"corrupt")
        with self.assertRaises(main.BuildError):
            self._probe(run)

    def test_probe_propagates_a_temporary_root_cleanup_failure(self) -> None:
        UnremovableTemporaryDirectory.instances.clear()
        run = FakeChild()
        with mock.patch.object(tempfile, "TemporaryDirectory", UnremovableTemporaryDirectory), \
                mock.patch.object(subprocess, "run", run), self.assertRaises(OSError):
            main._cleanup_fatal_probe(self.stage)
        for instance in UnremovableTemporaryDirectory.instances:
            shutil.rmtree(instance.name, ignore_errors=True)


class ToolRun:
    """A minimal checked ``main.run`` stand-in for the probe's call-site contracts."""

    def __init__(self, llvm_export=None) -> None:
        self.calls: list[tuple[tuple[str, ...], object, object, bool]] = []
        self._llvm_export = json.dumps(_FULL_COVERAGE if llvm_export is None else llvm_export)

    def __call__(self, argv, cwd=None, env=None, capture=False):
        argv = tuple(str(item) for item in argv)
        self.calls.append((argv, cwd, env, capture))
        name = os.path.basename(argv[0]).lower()
        if capture and "llvm-cov" in name:
            if any("--format=lcov" in item for item in argv):
                return subprocess.CompletedProcess(list(argv), 0, _LCOV)
            return subprocess.CompletedProcess(list(argv), 0, self._llvm_export)
        if capture:
            return subprocess.CompletedProcess(list(argv), 0, "")
        if env and "LLVM_PROFILE_FILE" in env:
            path = Path(env["LLVM_PROFILE_FILE"].replace("%m", "1").replace("%p", "2"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"raw")
        return subprocess.CompletedProcess(list(argv), 0, None)


class CleanupFatalProbeHookCase(unittest.TestCase):
    """Where the driver runs the probe: Debug builds and the coverage unit scope only."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.llvm = main.Llvm(
            directory=self.root / "llvm",
            runtime=self.root / "llvm" / "runtime",
            cov=self.root / "llvm-cov.exe",
            profdata=self.root / "llvm-profdata.exe",
        )

    def _build_tools(self, run: ToolRun):
        return mock.patch.multiple(
            main,
            run=run,
            _native_tools=mock.Mock(return_value=(self.root / "MSBuild.exe", self.root / "vcpkg.exe")),
            _restore=mock.Mock(),
            _msbuild_command=mock.Mock(return_value=("MSBuild.exe", "tests.vcxproj")),
        )

    def test_debug_test_runs_the_probe_once(self) -> None:
        with mock.patch.object(main, "_cleanup_fatal_probe") as probe, self._build_tools(ToolRun()):
            main.test("x64", "Debug", repository=self.repo)
        probe.assert_called_once()

    def test_release_test_skips_the_probe(self) -> None:
        with mock.patch.object(main, "_cleanup_fatal_probe") as probe, self._build_tools(ToolRun()):
            main.test("x64", "Release", repository=self.repo)
        probe.assert_not_called()

    def test_coverage_runs_the_probe_once_for_the_unit_scope(self) -> None:
        with mock.patch.object(main, "_cleanup_fatal_probe") as probe, self._build_tools(ToolRun()):
            main._coverage(
                self.repo, "x64", self.root / "MSBuild.exe", self.root / "vcpkg.exe", self.llvm
            )
        probe.assert_called_once_with(
            main.bin_directory(self.repo, "x64", "Coverage"),
            env={
                "LLVM_PROFILE_FILE": str(
                    main.artifacts_root(self.repo) / "coverage" / "x64" / "unit" / "coverage-%m-%p.profraw"
                )
            },
        )

    def test_coverage_does_not_repeat_the_probe_for_the_corpus_scope(self) -> None:
        corpus = self.root / "corpus"
        corpus.mkdir()
        with mock.patch.object(main, "_cleanup_fatal_probe") as probe, self._build_tools(ToolRun()):
            main._coverage(
                self.repo, "x64", self.root / "MSBuild.exe", self.root / "vcpkg.exe", self.llvm,
                corpus=corpus,
            )
        probe.assert_called_once_with(
            main.bin_directory(self.repo, "x64", "Coverage"),
            env={
                "LLVM_PROFILE_FILE": str(
                    main.artifacts_root(self.repo) / "coverage" / "x64" / "unit" / "coverage-%m-%p.profraw"
                )
            },
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

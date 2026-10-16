"""Tests-first contract for the minimal build driver (``build/main.py``).

This module is written before the minimal driver exists.  It pins the observable
behaviour of the small public surface the migration targets: ``build``, ``test``,
``package``, ``coverage_complete`` and the ``main`` / ``run`` entry points.  Until
that surface exists in ``build/main.py`` every test fails with a missing-attribute
error, which is the intended RED state for this phase.

Only observable behaviour is asserted: recorded tool invocations, the names and
bytes of the produced ZIP members, and the coverage verdicts.  No test inspects
source text, imports retired helpers, or launches a real tool.  The guard in
``GuardedCase.setUp`` turns any attempt to start a real subprocess into a hard
failure, so a partial implementation cannot accidentally shell out to vcpkg,
MSBuild or a sanitizer.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import main  # noqa: E402
import licenses  # noqa: E402


MODULE = "renpy"
VERSION = "1.2.3"

# Fixture bytes: each shipped payload is compared against these independent
# literals, never against a value rederived from the same source it copied.
BINARY_X64 = b"ELF fake renpy module x64 payload\n"
BINARY_X86 = b"fake renpy module x86 payload\n"
PDB_X64 = b"fake x64 program database\n"
PDB_X86 = b"fake x86 program database\n"
OBSERVER_INI = b"[Modules]\nRenPy=modules\\renpy.so\n"
CHANGELOG_EN = b"Version 1.2.3\n-------------\n + Fixture change note.\n"
CHANGELOG_RU = "\u0412\u0435\u0440\u0441\u0438\u044f 1.2.3\n-------------\n + \u0422\u0435\u0441\u0442\u043e\u0432\u0430\u044f \u0437\u0430\u043c\u0435\u0442\u043a\u0430.\n".encode("utf-8")
LICENSE_MIT = b"Fixture MIT license text.\n"
LICENSE_OBSERVER = b"Fixture Observer LGPL notice.\n"
LICENSE_GPL = b"Fixture GPLv3 text.\n"
LICENSE_RPATOOL = b"Fixture rpatool notice.\n"
LICENSE_SERDE = b"Fixture serde-pickle notice.\n"
LICENSE_ZLIB_NG = b"Fixture zlib-ng notice.\n"

_PROPERTY = re.compile(r"^[-/](?:p|property):(?P<name>[^=]+)=(?P<value>.*)$")


def _fake_git(repository, *arguments):
    """Deterministic stand-in for the single ``git`` boundary the Release attestation reads.

    ``build`` now records a Release attestation that re-derives the tracked build-input set and the
    checkout identity, so these driver-level tests fake that one boundary instead of letting the
    guard forbid the read.  The tracked set is the manifest the fake repository carries.
    """

    if arguments == ("ls-files", "-z"):
        return "vcpkg.json\x00"
    if arguments == ("rev-parse", "HEAD"):
        return "a" * 40
    if arguments == ("rev-parse", "HEAD^{tree}"):
        return "b" * 40
    raise AssertionError(f"unexpected git invocation: {arguments}")


def _norm(path: os.PathLike[str] | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _tool_name(argv: tuple[str, ...]) -> str:
    return os.path.basename(argv[0]).lower() if argv else ""


def _option(argv: tuple[str, ...], flag: str) -> str | None:
    prefix = flag + "="
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
        if item.startswith(prefix):
            return item[len(prefix):]
    return None


def _properties(argv: tuple[str, ...]) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in argv:
        match = _PROPERTY.match(item)
        if match is not None:
            found[match.group("name")] = match.group("value")
    return found


class RecordedCall:
    def __init__(self, argv: tuple[str, ...], cwd, env) -> None:
        self.argv = tuple(str(item) for item in argv)
        self.cwd = None if cwd is None else Path(cwd)
        self.env = None if env is None else {str(k): str(v) for k, v in env.items()}


class RecordingRun:
    """A faithful stand-in for the checked ``main.run`` entry point.

    ``run`` is documented as the single *checked* subprocess entry: it returns a
    ``CompletedProcess`` on success and lets a nonzero exit raise.  This fake
    mirrors that contract so callers see the same success/failure semantics, and
    a ``fail`` predicate decides which invocation fails.
    """

    def __init__(self, *, vswhere_stdout: str | None = None, fail_when=None) -> None:
        self.calls: list[RecordedCall] = []
        self._vswhere_stdout = vswhere_stdout
        self._fail_when = fail_when

    def __call__(self, argv, cwd=None, env=None, capture=False, **_: object):
        recorded = tuple(str(item) for item in argv)
        self.calls.append(RecordedCall(recorded, cwd, env))
        if self._fail_when is not None and self._fail_when(recorded):
            raise subprocess.CalledProcessError(3, list(recorded), "")
        if self._vswhere_stdout is not None and "vswhere" in _tool_name(recorded):
            return subprocess.CompletedProcess(list(recorded), 0, self._vswhere_stdout)
        return subprocess.CompletedProcess(list(recorded), 0, "" if capture else None)

    def by_tool(self, tool: str) -> list[RecordedCall]:
        return [call for call in self.calls if _tool_name(call.argv) == tool]


class OptionReaderTests(unittest.TestCase):
    def test_option_reader_handles_equals_and_missing_values(self) -> None:
        self.assertEqual(_option(("vcpkg", "--triplet=fixture"), "--triplet"), "fixture")
        self.assertIsNone(_option(("vcpkg", "--triplet"), "--triplet"))


class GuardedCase(unittest.TestCase):
    """Base case that forbids any real process launch and builds tool fakes."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "vcpkg.json").write_text('{"name": "unpackrat"}\n', encoding="utf-8")

        # Hard guard: a partial implementation can never shell out for real.
        forbidden = AssertionError("a real subprocess launch is forbidden in this test")
        self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=forbidden))
        self.enterContext(mock.patch.object(subprocess, "run", side_effect=forbidden))

        self.vcpkg_root = self.root / "vcpkg"
        self.vcpkg_exe = self.vcpkg_root / "vcpkg.exe"
        self.vcpkg_props = self.vcpkg_root / "scripts" / "buildsystems" / "msbuild" / "vcpkg.props"
        self.vs_install = self.root / "vs"
        self.msbuild_exe = self.vs_install / "MSBuild" / "Current" / "Bin" / "amd64" / "MSBuild.exe"
        for path in (self.vcpkg_exe, self.vcpkg_props, self.msbuild_exe):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake")
        # The driver forwards the selected 14.51 servicing toolset selected from this install.
        cl = self.vs_install / "VC" / "Tools" / "MSVC" / "14.51.36231" / "bin" / "Hostx64" / "x64" / "cl.exe"
        cl.parent.mkdir(parents=True, exist_ok=True)
        cl.write_bytes(b"fake")

    def tools(self, run: RecordingRun):
        """Patch the tool-lookup boundaries and the single ``main.run`` entry."""

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(main, "run", run))
        # A Release build records an attestation that reads the checkout through the git boundary.
        stack.enter_context(mock.patch.object(licenses, "_git", _fake_git))
        # Debug tests launch the fatal cleanup probe; the tool fake must never let that escape
        # into a real subprocess.
        stack.enter_context(mock.patch.object(main, "_cleanup_fatal_probe"))
        stack.enter_context(mock.patch.object(main, "_clang_tidy", return_value=self.root / "clang-tidy.exe"))
        stack.enter_context(mock.patch("shutil.which", return_value=None))
        stack.enter_context(mock.patch.dict(os.environ, {"VCPKG_ROOT": str(self.vcpkg_root)}))
        return stack

    def make_repo(self) -> Path:
        module = self.repo / "src" / "modules" / MODULE
        module.mkdir(parents=True)
        (module / "VERSION").write_text(VERSION + "\n", encoding="utf-8")
        (module / "observer_user.ini").write_bytes(OBSERVER_INI)
        (module / "ChangeLog.en.txt").write_bytes(CHANGELOG_EN)
        (module / "ChangeLog.ru.txt").write_bytes(CHANGELOG_RU)
        (self.repo / "LICENSE.txt").write_bytes(LICENSE_MIT)
        licenses = self.repo / "licenses"
        licenses.mkdir()
        for name, content in {
            "Observer.txt": LICENSE_OBSERVER,
            "GPL-3.0.txt": LICENSE_GPL,
            "rpatool.txt": LICENSE_RPATOOL,
            "serde-pickle.txt": LICENSE_SERDE,
            "zlib-ng.txt": LICENSE_ZLIB_NG,
            "VC-LTL5.txt": b"Fixture VC-LTL5 license\n",
            "YY-Thunks.txt": b"Fixture YY-Thunks license\n",
        }.items():
            (licenses / name).write_bytes(content)
        for architecture, binary, symbol in (
            ("x64", BINARY_X64, PDB_X64),
            ("x86", BINARY_X86, PDB_X86),
        ):
            stage = self.repo / "out" / "native" / "bin" / architecture / "Release"
            stage.mkdir(parents=True)
            (stage / f"{MODULE}.so").write_bytes(binary)
            (stage / f"{MODULE}.pdb").write_bytes(symbol)
        return self.repo


class BuildTests(GuardedCase):
    def test_build_debug_uses_default_triplet_project_and_stable_root(self) -> None:
        run = RecordingRun(vswhere_stdout=str(self.vs_install))
        with self.tools(run):
            main.build("x64", "Debug", repository=self.repo)

        vcpkg = run.by_tool("vcpkg.exe")
        self.assertTrue(vcpkg, "build must restore a vcpkg triplet")
        triplets = [value for call in vcpkg if (value := _option(call.argv, "--triplet"))]
        self.assertIn("unpackrat-x64-windows-static", triplets)
        for call in vcpkg:
            self.assertIn("--overlay-triplets", call.argv)

        msbuild = run.by_tool("msbuild.exe")
        self.assertEqual(len(msbuild), 1, "build must drive exactly one MSBuild request")
        argv = msbuild[0].argv
        self.assertTrue(
            any(item.replace("/", "\\").endswith(r"projects\tests.vcxproj") for item in argv),
            "build must target build/projects/tests.vcxproj",
        )
        properties = _properties(argv)
        self.assertEqual(properties.get("Configuration"), "Debug")
        self.assertEqual(properties.get("Platform"), "x64")
        root = properties.get("ArtifactsRoot", "")
        self.assertTrue(_norm(root).startswith(_norm(self.repo / "out")), root)
        self.assertIn("native", root.replace("\\", "/").split("/"))

    def test_build_asan_uses_asan_triplet(self) -> None:
        run = RecordingRun(vswhere_stdout=str(self.vs_install))
        with self.tools(run):
            main.build("x64", "ASan", repository=self.repo)

        triplets = [
            value for call in run.by_tool("vcpkg.exe") if (value := _option(call.argv, "--triplet"))
        ]
        self.assertIn("unpackrat-x64-windows-static-asan", triplets)

    def test_build_nonzero_tool_result_propagates(self) -> None:
        run = RecordingRun(
            vswhere_stdout=str(self.vs_install),
            fail_when=lambda argv: _tool_name(argv) == "msbuild.exe",
        )
        with self.tools(run):
            with self.assertRaises(subprocess.CalledProcessError):
                main.build("x64", "Debug", repository=self.repo)


class TestTests(GuardedCase):
    def _test_calls(self, run: RecordingRun) -> list[RecordedCall]:
        return run.by_tool("tests.exe")

    def test_test_runs_built_executable_in_its_own_directory(self) -> None:
        run = RecordingRun(vswhere_stdout=str(self.vs_install))
        with self.tools(run):
            main.test("x64", "Release", repository=self.repo)

        calls = self._test_calls(run)
        self.assertEqual(len(calls), 1, "without a corpus the hermetic suite runs once")
        call = calls[0]
        stage = self.repo / "out" / "native" / "bin" / "x64" / "Release"
        self.assertEqual(_norm(call.cwd), _norm(stage))
        self.assertEqual(_norm(call.argv[0]), _norm(stage / "tests.exe"))

    def test_test_writes_junit_report_under_the_repository(self) -> None:
        run = RecordingRun(vswhere_stdout=str(self.vs_install))
        with self.tools(run):
            main.test("x64", "Debug", repository=self.repo)

        call = self._test_calls(run)[0]
        reports = [item.split("JUnit::out=", 1)[1] for item in call.argv if "JUnit::out=" in item]
        self.assertEqual(len(reports), 1, "one JUnit report per suite")
        report = _norm(reports[0])
        self.assertTrue(report.startswith(_norm(self.repo)), reports[0])
        self.assertTrue(report.endswith(".xml"), reports[0])
        self.assertIn("tests", report)

    def test_test_without_corpus_has_no_compatibility_environment(self) -> None:
        run = RecordingRun(vswhere_stdout=str(self.vs_install))
        with self.tools(run):
            main.test("x64", "Debug", repository=self.repo)

        call = self._test_calls(run)[0]
        self.assertNotIn("UNPACKRAT_TEST_CORPUS", call.env or {})

    def test_test_with_corpus_runs_an_additional_suite(self) -> None:
        corpus = self.root / "corpus"
        corpus.mkdir()
        run = RecordingRun(vswhere_stdout=str(self.vs_install))
        with self.tools(run):
            main.test("x64", "Debug", repository=self.repo, corpus=corpus)

        calls = self._test_calls(run)
        self.assertEqual(len(calls), 2, "an optional corpus adds one suite")
        corpus_calls = [
            call for call in calls if (call.env or {}).get("UNPACKRAT_TEST_CORPUS")
        ]
        self.assertEqual(len(corpus_calls), 1)
        self.assertEqual(_norm(corpus_calls[0].env["UNPACKRAT_TEST_CORPUS"]), _norm(corpus))
        self.assertEqual({_norm(call.cwd) for call in calls}, {_norm(calls[0].cwd)})


class PackageTests(GuardedCase):
    def package(self) -> list[Path]:
        return list(
            main.package(
                repository=self.make_repo(),
                output=self.repo / "out" / "packages",
                modules=(MODULE,),
            )
        )

    def test_package_creates_the_three_named_archives(self) -> None:
        created = self.package()
        names = sorted(path.name for path in created)
        self.assertEqual(
            names,
            [
                f"{MODULE}-{VERSION}-bin-x64.zip",
                f"{MODULE}-{VERSION}-bin-x86.zip",
                f"{MODULE}-{VERSION}-pdb.zip",
            ],
        )
        for path in created:
            self.assertTrue(Path(path).is_file(), path)

    def test_package_module_archives_carry_the_shipped_payload(self) -> None:
        self.package()
        output = self.repo / "out" / "packages"
        for architecture, binary in (("x64", BINARY_X64), ("x86", BINARY_X86)):
            archive = output / f"{MODULE}-{VERSION}-bin-{architecture}.zip"
            with zipfile.ZipFile(archive) as opened:
                members = {name: opened.read(name) for name in opened.namelist()}
            expected = {
                f"{MODULE}.so": binary,
                "observer_user.ini": OBSERVER_INI,
                "ChangeLog.en.txt": CHANGELOG_EN,
                "ChangeLog.ru.txt": CHANGELOG_RU,
            }
            self.assertEqual(sorted(members), sorted([*expected, "LICENSES.txt"]))
            for name, content in expected.items():
                self.assertEqual(members[name], content, f"{architecture}:{name}")
            licenses = members["LICENSES.txt"]
            self.assertTrue(licenses.strip(), "LICENSES.txt must not be empty")
            self.assertIn(LICENSE_MIT, licenses)
            self.assertIn(LICENSE_OBSERVER, licenses)
            self.assertIn(LICENSE_ZLIB_NG, licenses)
            self.assertNotIn(b"Fixture VC-LTL5 license", licenses)
            self.assertIn(b"Fixture YY-Thunks license", licenses)

    def test_package_symbol_archive_holds_both_architectures(self) -> None:
        self.package()
        archive = self.repo / "out" / "packages" / f"{MODULE}-{VERSION}-pdb.zip"
        with zipfile.ZipFile(archive) as opened:
            members = {name: opened.read(name) for name in opened.namelist()}
        self.assertEqual(
            members,
            {"x86/renpy.pdb": PDB_X86, "x64/renpy.pdb": PDB_X64},
        )

    def test_package_emits_no_service_metadata(self) -> None:
        self.package()
        forbidden = {"manifest.json", "packages.json", "validation.json", "snapshot.json"}
        for archive in (self.repo / "out" / "packages").glob("*.zip"):
            with zipfile.ZipFile(archive) as opened:
                names = {Path(name).name for name in opened.namelist()}
            self.assertFalse(names & forbidden, archive.name)


class CoverageTests(GuardedCase):
    @staticmethod
    def document(lines: tuple[int, int], branches: tuple[int, int]) -> dict:
        return {
            "type": "llvm.coverage.json.export",
            "version": "2.0.1",
            "data": [
                {
                    "totals": {
                        "lines": {"count": lines[0], "covered": lines[1]},
                        "branches": {"count": branches[0], "covered": branches[1]},
                        "functions": {"count": 3, "covered": 3},
                        "regions": {"count": 4, "covered": 4},
                    },
                    "files": [],
                }
            ],
        }

    def test_coverage_complete_accepts_full_line_and_branch_coverage(self) -> None:
        self.assertTrue(main.coverage_complete(self.document((12, 12), (7, 7))))

    def test_coverage_complete_rejects_uncovered_branches(self) -> None:
        with self.assertRaises(ValueError):
            main.coverage_complete(self.document((12, 12), (7, 6)))

    def test_coverage_complete_rejects_uncovered_lines(self) -> None:
        with self.assertRaises(ValueError):
            main.coverage_complete(self.document((12, 11), (7, 7)))

    def test_coverage_complete_rejects_empty_totals(self) -> None:
        with self.assertRaises(ValueError):
            main.coverage_complete(self.document((0, 0), (0, 0)))

    def test_coverage_complete_rejects_malformed_document(self) -> None:
        for document in ({}, {"data": []}, {"data": [{}]}, {"data": [{"totals": {}}]}):
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    main.coverage_complete(document)


class MainDispatchTests(GuardedCase):
    def test_main_dispatches_build(self) -> None:
        handler = mock.MagicMock(return_value=[])
        with mock.patch.object(main, "build", handler):
            code = main.main(["build", "-Arch", "x64", "-Config", "Debug"])
        self.assertEqual(code, 0)
        handler.assert_called_once()
        self.assertIn("x64", handler.call_args.args)
        self.assertIn("Debug", handler.call_args.args)

    def test_main_dispatches_test(self) -> None:
        handler = mock.MagicMock(return_value=[])
        with mock.patch.object(main, "test", handler):
            code = main.main(["test", "-Arch", "x64", "-Config", "Release"])
        self.assertEqual(code, 0)
        self.assertIn("x64", handler.call_args.args)
        self.assertIn("Release", handler.call_args.args)

    def test_main_dispatches_package(self) -> None:
        handler = mock.MagicMock(return_value=[])
        with mock.patch.object(main, "package", handler):
            code = main.main(["package"])
        self.assertEqual(code, 0)
        handler.assert_called_once()

    def test_main_rejects_unknown_command(self) -> None:
        with self.assertRaises(SystemExit):
            main.main(["definitely-not-a-command"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

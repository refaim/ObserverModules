from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import main  # noqa: E402
from core import result_export  # noqa: E402
from core.host import DeferredGate, VerifyRoute  # noqa: E402

import diagnostics as diagnostics_module  # noqa: E402
import native as native_module  # noqa: E402
import packaging_ops as packaging_module  # noqa: E402
import source_checks as source_module  # noqa: E402


_NATIVE_TOOLS = SimpleNamespace(msbuild=Path("msbuild.exe"), vcpkg=Path("vcpkg/vcpkg.exe"))
_TOOLCHAIN = SimpleNamespace(llvm_dir=Path("llvm"))
_FULL_ROUTE = VerifyRoute(("x64",), ("x64",), ("x86", "x64"), ("x64",), ("x86", "x64"), ())
_EMPTY_ROUTE = VerifyRoute(
    (), (), (), (), (), (DeferredGate("tests", "x64", "host cannot execute x64 tests"),)
)


def _spec(function, **options):
    """An autospec mock, so a call with a wrong keyword fails against the real signature."""

    return mock.create_autospec(function, **options)


def _processes() -> main._Processes:
    return main._Processes(mock.MagicMock(), mock.MagicMock(), mock.MagicMock())


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.repo = Path(self._temporary.name)

        self.build = _spec(native_module.build, return_value=(Path("out/build"),))
        self.restore = _spec(native_module.restore, return_value=(Path("out/restore"),))
        self.test = _spec(native_module.test, return_value=(Path("out/test"),))
        self.source_checks = _spec(source_module.source_checks, return_value=(Path("out/source"),))
        self.compiler_analysis = _spec(source_module.compiler_analysis, return_value=(Path("out/analysis"),))
        self.audit = _spec(packaging_module.audit_binaries, return_value=(Path("out/audit"),))
        self.package = _spec(packaging_module.package, return_value=SimpleNamespace(
            archives=(Path("out/packages/renpy.zip"),), deferred=()
        ))
        self.coverage = _spec(diagnostics_module.test_coverage, return_value=(Path("out/coverage"),))
        self.sanitizer = _spec(diagnostics_module.test_sanitizer, return_value=(Path("out/sanitizer"),))
        self.fuzz = _spec(diagnostics_module.fuzz, return_value=(Path("out/fuzz"),))
        self.leaks = _spec(diagnostics_module.test_leaks, return_value=(Path("out/leaks"),))
        self.export = mock.MagicMock(return_value=Path("export"))

        self.processes = _processes()

        self.patches = [
            mock.patch.object(main.native, "locate_tools", return_value=_NATIVE_TOOLS),
            mock.patch.object(main.native, "build", self.build),
            mock.patch.object(main.native, "restore", self.restore),
            mock.patch.object(main.native, "test", self.test),
            mock.patch.object(main.source_checks, "locate_tools", return_value="source-tools"),
            mock.patch.object(main.source_checks, "locate_analysis_tools", return_value="analysis-tools"),
            mock.patch.object(main.source_checks, "source_checks", self.source_checks),
            mock.patch.object(main.source_checks, "compiler_analysis", self.compiler_analysis),
            mock.patch.object(main.packaging_ops, "locate_packaging_tools", return_value="packaging-tools"),
            mock.patch.object(main.packaging_ops, "audit_binaries", self.audit),
            mock.patch.object(main.packaging_ops, "package", self.package),
            mock.patch.object(main.diagnostics, "test_coverage", self.coverage),
            mock.patch.object(main.diagnostics, "test_sanitizer", self.sanitizer),
            mock.patch.object(main.diagnostics, "fuzz", self.fuzz),
            mock.patch.object(main.diagnostics, "test_leaks", self.leaks),
            mock.patch.object(main, "discover_msvc_toolchain", return_value=_TOOLCHAIN),
            mock.patch.object(main, "resolve_llvm", return_value=SimpleNamespace(path=Path("llvm-cov"))),
            mock.patch.object(main, "resolve_profile_runtime", return_value=SimpleNamespace(path=Path("profile"))),
            mock.patch.object(main, "resolve_ubsan_runtime", return_value=SimpleNamespace(path=Path("ubsan"))),
            mock.patch.object(main, "resolve_fuzzer_runtime", return_value=SimpleNamespace(path=Path("fuzz-rt"))),
            mock.patch.object(main, "resolve_asan_runtimes", return_value=(
                ("x86", SimpleNamespace(path=Path("asan-x86"))),
                ("x64", SimpleNamespace(path=Path("asan-x64"))),
            )),
            mock.patch.object(main, "resolve_umdh", return_value=SimpleNamespace(path=Path("umdh"))),
            mock.patch.object(main, "detect_host_architecture", return_value="x64"),
            mock.patch.object(main, "_prioritize", return_value=None),
            mock.patch.object(main, "_restore_priority"),
            mock.patch.object(main, "_processes", return_value=self.processes),
            mock.patch.object(main, "_export", self.export),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def invoke(self, argv: list[str]) -> int:
        return main.main(argv)

    def output(self, name: str) -> Path:
        return self.repo / "out" / "reports" / name


class DispatchTests(Harness):
    def test_build_and_test_route_to_the_native_helpers(self) -> None:
        self.assertEqual(self.invoke([
            "build", "-Repository", str(self.repo), "-Arch", "x64", "-Config", "Release", "-Jobs", "3",
        ]), 0)
        self.build.assert_called_once_with(
            self.repo, ("x64",), ("Release",), jobs=3, tools=_NATIVE_TOOLS,
            runner=self.processes.native,
        )

        self.invoke([
            "test", "-Repository", str(self.repo), "-Arch", "x86,x64", "-Config", "Debug,Release",
            "-Corpus", str(self.repo / "golden"),
        ])
        self.test.assert_called_once_with(
            self.repo, ("x86", "x64"), ("Debug", "Release"),
            corpus=self.repo / "golden", jobs=None, tools=_NATIVE_TOOLS,
            runner=self.processes.native,
        )

    def test_restore_defaults_and_asan_flavors(self) -> None:
        self.invoke(["restore", "-Repository", str(self.repo)])
        self.restore.assert_called_once_with(
            self.repo, ("x64",), tools=_NATIVE_TOOLS, runner=self.processes.native
        )
        self.processes.native.assert_not_called()

        self.restore.reset_mock()
        self.invoke([
            "restore", "-Repository", str(self.repo), "-Arch", "x86,x64", "-RestoreFlavor", "all",
        ])
        self.restore.assert_called_once_with(
            self.repo, ("x86", "x64"), tools=_NATIVE_TOOLS, runner=self.processes.native
        )
        self.assertEqual(self.processes.native.call_count, 2)
        self.assertIn(
            "unpackrat-x86-windows-static-asan", self.processes.native.call_args_list[0].args[0]
        )
        self.assertIn(
            "unpackrat-x64-windows-static-asan", self.processes.native.call_args_list[1].args[0]
        )

        self.restore.reset_mock()
        self.processes.native.reset_mock()
        self.invoke(["restore", "-Repository", str(self.repo), "-Arch", "x64", "-RestoreFlavor", "asan"])
        self.restore.assert_not_called()
        self.assertEqual(self.processes.native.call_count, 1)
        self.assertIn("unpackrat-x64-windows-static-asan", self.processes.native.call_args.args[0])

    def test_source_and_analysis_commands_use_their_own_output_tree(self) -> None:
        self.invoke(["source-checks", "-Repository", str(self.repo)])
        self.source_checks.assert_called_once_with(
            self.repo, ("x64",), self.output("source-checks"), jobs=4, tools="source-tools",
            runner=self.processes.native,
        )
        self.invoke(["compiler-analysis", "-Repository", str(self.repo), "-Jobs", "2"])
        self.compiler_analysis.assert_called_once_with(
            self.repo, ("x64",), self.output("compiler-analysis"), jobs=2, tools="analysis-tools",
            runner=self.processes.native,
        )

    def test_diagnostic_commands_route_with_only_the_tools_they_need(self) -> None:
        self.invoke(["test-coverage", "-Repository", str(self.repo)])
        tools = self.coverage.call_args.kwargs["tools"]
        self.assertIsInstance(tools, main.diagnostics.DiagnosticsTools)
        self.assertEqual(tools.asan_runtimes, {})
        self.assertEqual(tools.umdh, Path())
        self.assertEqual(tools.llvm_cov, Path("llvm-cov"))
        self.assertEqual(dict(tools.llvm_runtimes), {"x64": Path("profile")})
        self.assertEqual(set(tools.asan_runtimes), set())
        self.assertEqual(self.coverage.call_args.kwargs["corpus"], None)
        self.assertEqual(self.coverage.call_args.kwargs["runner"], self.processes.tools)

        self.invoke(["test-coverage", "-Repository", str(self.repo), "-Corpus", str(self.repo / "g")])
        self.assertEqual(self.coverage.call_args.kwargs["corpus"], self.repo / "g")

        self.invoke(["test-asan", "-Repository", str(self.repo), "-Arch", "x86,x64"])
        asan = self.sanitizer.call_args.kwargs["tools"]
        self.assertEqual(set(asan.asan_runtimes), {"x86", "x64"})
        self.assertEqual(asan.llvm_runtimes, {})
        self.sanitizer.assert_called_with(
            self.repo, "asan", ("x86", "x64"), self.output("test-asan"), tools=asan,
            runner=self.processes.tools, jobs=None,
        )

        self.invoke(["test-ubsan", "-Repository", str(self.repo)])
        ubsan = self.sanitizer.call_args.kwargs["tools"]
        self.assertEqual(ubsan.asan_runtimes, {})
        self.assertEqual(ubsan.llvm_cov, Path())
        self.assertEqual(dict(ubsan.llvm_runtimes), {"x64": Path("ubsan")})
        self.invoke(["test-ubsan", "-Repository", str(self.repo), "-Arch", "x86"])
        self.assertEqual(self.sanitizer.call_args.kwargs["tools"].llvm_runtimes, {})

        self.invoke(["test-leaks", "-Repository", str(self.repo), "-LeakWarmup", "2",
                     "-LeakIterations", "6", "-LeakWindows", "5", "-LeakToleranceBytes", "9"])
        self.assertEqual(self.leaks.call_args.args[2:6], (2, 6, 5, 9))
        self.assertEqual(self.leaks.call_args.kwargs["tools"].umdh, Path("umdh"))

        self.invoke(["fuzz", "-Repository", str(self.repo), "-Arch", "x86,x64",
                     "-FuzzSeconds", "17", "-FuzzTarget", "renpy"])
        fuzz_tools = self.fuzz.call_args.kwargs["tools"]
        self.assertEqual(fuzz_tools.llvm_cov, Path())
        self.assertEqual(
            dict(fuzz_tools.llvm_runtimes), {"x86": Path("fuzz-rt"), "x64": Path("fuzz-rt")}
        )
        self.fuzz.assert_called_once_with(
            self.repo, ("x86", "x64"), ("renpy",), 17, self.output("fuzz"),
            tools=self.fuzz.call_args.kwargs["tools"], runner=self.processes.tools, jobs=None,
        )

    def test_audit_and_package_build_release_then_gate(self) -> None:
        self.invoke(["audit-binaries", "-Repository", str(self.repo), "-Module", "renpy,rpgmaker"])
        self.build.assert_called_once_with(
            self.repo, ("x64",), ("Release",), jobs=None, tools=_NATIVE_TOOLS,
            runner=self.processes.native,
        )
        self.audit.assert_called_once_with(
            self.repo, ("x64",), self.output("audit-binaries"),
            modules=("renpy", "rpgmaker"), tools="packaging-tools",
            runner=self.processes.packaging,
        )

        self.build.reset_mock()
        self.invoke([
            "package", "-Repository", str(self.repo), "-Module", "all", "-ExportDir", str(self.repo / "ev"),
        ])
        self.build.assert_called_once_with(
            self.repo, ("x64",), ("Release",), jobs=None, tools=_NATIVE_TOOLS,
            runner=self.processes.native,
        )
        self.package.assert_called_once_with(
            self.repo, ("x64",), self.repo / "out" / "reports" / "package",
            modules=("renpy", "rpgmaker", "zanzarah"), smoke_architectures=("x64",),
            tools="packaging-tools", runner=self.processes.packaging, host="x64",
        )
        destination, command, operations = self.export.call_args.args
        self.assertEqual((destination, command), (self.repo / "ev", "package"))
        self.assertEqual(operations[0].status, result_export.PASSED)
        self.assertIn(
            self.repo / "out" / "reports" / "package" / "packages" / "packages.json",
            self.export.call_args.kwargs["packages"],
        )

    def test_package_export_captures_a_failure_before_reraising(self) -> None:
        self.package.side_effect = main.packaging_ops.PackagingError("no binary")
        args = main._parser().parse_args(
            ["package", "-Repository", str(self.repo), "-ExportDir", str(self.repo / "ev")]
        )
        with self.assertRaises(main.packaging_ops.PackagingError):
            main._package_command(self.repo, args, self.processes)
        operations = self.export.call_args.args[2]
        self.assertEqual(operations[0].status, result_export.FAILED)
        self.assertIn("no binary", operations[0].detail)

    def test_package_without_export_does_not_export(self) -> None:
        self.invoke(["package", "-Repository", str(self.repo)])
        self.export.assert_not_called()

    def test_package_success_logs_the_captured_output(self) -> None:
        def fake_package(repository, architectures, output, **options):
            print("packaging stdout")
            return SimpleNamespace(archives=(Path("out/packages/renpy.zip"),), deferred=())

        self.package.side_effect = fake_package
        with redirect_stdout(StringIO()):
            self.invoke([
                "package", "-Repository", str(self.repo), "-ExportDir", str(self.repo / "ev"),
            ])
        operations = self.export.call_args.args[2]
        self.assertEqual(operations[0].status, result_export.PASSED)
        self.assertIn("packaging stdout", operations[0].log)

    def test_package_failure_without_export_reraises_without_exporting(self) -> None:
        self.package.side_effect = main.packaging_ops.PackagingError("boom")
        args = main._parser().parse_args(["package", "-Repository", str(self.repo)])
        with self.assertRaises(main.packaging_ops.PackagingError):
            main._package_command(self.repo, args, self.processes)
        self.export.assert_not_called()

    def test_package_command_exports_current_evidence_without_stale_archives(self) -> None:
        stale = self.repo / "out" / "packages" / "renpy-3.0.0-x64.zip"
        stale.parent.mkdir(parents=True)
        stale.write_text("old release", encoding="utf-8")

        def fail_after_audit(repository, architectures, output, **options):
            audit = Path(output) / "audit" / "x64" / "renpy" / "binskim.sarif"
            audit.parent.mkdir(parents=True)
            audit.write_text("{}", encoding="utf-8")
            leftover = Path(output) / "packages" / "staging-partial.zip"
            leftover.parent.mkdir(parents=True)
            leftover.write_text("partial", encoding="utf-8")
            raise main.packaging_ops.PackagingError("audit failed")

        self.package.side_effect = fail_after_audit
        args = main._parser().parse_args(
            ["package", "-Repository", str(self.repo), "-ExportDir", str(self.repo / "ev")]
        )
        with self.assertRaises(main.packaging_ops.PackagingError):
            main._package_command(self.repo, args, self.processes)
        root = self.repo / "out" / "reports" / "package"
        self.package.assert_called_once_with(
            self.repo, ("x64",), root, modules=("renpy", "rpgmaker", "zanzarah"),
            smoke_architectures=("x64",), tools="packaging-tools",
            runner=self.processes.packaging, host="x64",
        )
        operations = self.export.call_args.args[2]
        self.assertEqual(operations[0].status, result_export.FAILED)
        self.assertIn("audit failed", operations[0].detail)
        names = [path.name for path in operations[0].reports]
        self.assertIn("binskim.sarif", names)
        self.assertNotIn("staging-partial.zip", names)
        self.assertNotIn("renpy-3.0.0-x64.zip", names)
        self.assertTrue(stale.exists())
        self.assertNotIn("packages", self.export.call_args.kwargs)

    def test_package_command_success_exports_current_audit_evidence(self) -> None:
        def ok_package(repository, architectures, output, **options):
            root = Path(output)
            audit = root / "audit" / "x64" / "renpy" / "binskim.sarif"
            audit.parent.mkdir(parents=True)
            audit.write_text("{}", encoding="utf-8")
            packages = root / "packages"
            (packages / "x64").mkdir(parents=True)
            archive = packages / "x64" / "renpy-3.1.0-x64.zip"
            archive.write_text("zip", encoding="utf-8")
            (packages / "packages.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(archives=(archive,), deferred=())

        self.package.side_effect = ok_package
        self.invoke([
            "package", "-Repository", str(self.repo), "-ExportDir", str(self.repo / "ev"),
        ])
        operations = self.export.call_args.args[2]
        self.assertEqual(operations[0].status, result_export.PASSED)
        # The current audit evidence is published, while the release payload stays in packages.
        self.assertEqual([path.name for path in operations[0].reports], ["binskim.sarif"])
        packages = self.export.call_args.kwargs["packages"]
        self.assertEqual(
            sorted(path.name for path in packages), ["packages.json", "renpy-3.1.0-x64.zip"]
        )

    def test_clean_and_doctor_bypass_the_build_handlers(self) -> None:
        with mock.patch.object(main, "clean_output", return_value=(Path("out/native"),)) as clean, \
             mock.patch.object(main, "doctor_main", return_value=0) as doctor:
            self.assertEqual(self.invoke(["doctor"]), 0)
            doctor.assert_called_once_with(())
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = self.invoke(["clean", "-Repository", str(self.repo), "-CleanMode", "reports"])
            self.assertEqual(code, 0)
            clean.assert_called_once_with(self.repo, "reports")
        self.assertIn("native", stdout.getvalue())


class VerifyTests(Harness):
    def test_verify_source_runs_the_flat_source_and_python_gates(self) -> None:
        with mock.patch.object(main, "_python_coverage", return_value=(Path("coverage.txt"),)) as coverage:
            result = self.invoke([
                "verify-source", "-Repository", str(self.repo), "-ExportDir", str(self.repo / "ev"),
            ])
        self.assertEqual(result, 0)
        self.source_checks.assert_called_once_with(
            self.repo, ("x64",), self.output("verify-source") / "source", jobs=4, tools="source-tools",
            runner=self.processes.native,
        )
        coverage.assert_called_once_with(
            self.repo, self.output("verify-source") / "python-coverage", self.processes
        )
        operations = self.export.call_args.args[2]
        self.assertEqual([item.name for item in operations], ["source", "python-coverage"])
        self.assertEqual(self.export.call_args.kwargs["packages"], ())

    def test_verify_arch_runs_every_routed_gate_and_blocks_package_on_a_failure(self) -> None:
        with mock.patch.object(main, "verify_route", return_value=_FULL_ROUTE):
            self.test.side_effect = RuntimeError("tests failed")
            stderr = StringIO()
            with redirect_stderr(stderr):
                result = self.invoke([
                    "verify-arch", "-Repository", str(self.repo), "-Arch", "x64",
                    "-ExportDir", str(self.repo / "ev"),
                ])
        self.assertEqual(result, 1)
        operations = {item.name: item for item in self.export.call_args.args[2]}
        self.assertEqual(operations["tests-x64"].status, result_export.FAILED)
        for name in ("source", "compiler-analysis", "coverage", "asan", "ubsan", "fuzz", "leaks", "audit"):
            self.assertEqual(operations[name].status, result_export.PASSED)
        self.assertEqual(operations["package"].status, result_export.DEFERRED)
        self.assertIn("failed gates: tests-x64", stderr.getvalue())
        self.assertEqual(self.export.call_args.kwargs["packages"], ())

    def test_verify_arch_packages_on_success_with_flat_archives(self) -> None:
        def write_package(repository, architectures, output, **options):
            directory = Path(output) / "packages" / "x64"
            directory.mkdir(parents=True)
            archive = directory / "renpy-3.1.0-x64.zip"
            archive.write_text("zip", encoding="utf-8")
            manifest = Path(output) / "packages" / "packages.json"
            manifest.write_text("{}", encoding="utf-8")
            return SimpleNamespace(archives=(archive,), deferred=())

        self.package.side_effect = write_package
        with mock.patch.object(main, "verify_route", return_value=_FULL_ROUTE):
            result = self.invoke([
                "verify-arch", "-Repository", str(self.repo), "-Arch", "x64",
                "-ExportDir", str(self.repo / "ev"),
            ])
        self.assertEqual(result, 0)
        packages = self.export.call_args.kwargs["packages"]
        self.assertEqual([path.name for path in packages], ["packages.json", "renpy-3.1.0-x64.zip"])
        self.coverage.assert_called_once()
        self.leaks.assert_called_once()
        self.test.assert_any_call(
            self.repo, ("x64",), ("Debug", "Release"), corpus=None, jobs=None,
            tools=_NATIVE_TOOLS, runner=self.processes.native, host="x64",
        )
        # verify already built the Release tree, so packaging must not trigger a second build.
        self.build.assert_not_called()

    def test_verify_arch_retains_package_failure_evidence_from_a_dedicated_root(self) -> None:
        def fail_after_audit(repository, architectures, output, **options):
            root = Path(output)
            audit = root / "audit" / "x64" / "renpy"
            audit.mkdir(parents=True)
            (audit / "binskim.sarif").write_text("{}", encoding="utf-8")
            (audit / "headers.txt").write_text("headers", encoding="utf-8")
            partial = root / "packages" / "renpy-3.1.0-x64.zip"
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_text("partial release", encoding="utf-8")
            raise main.packaging_ops.PackagingError("audit failed")

        self.package.side_effect = fail_after_audit
        with mock.patch.object(main, "verify_route", return_value=_FULL_ROUTE), \
             redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = self.invoke([
                "verify-arch", "-Repository", str(self.repo), "-Arch", "x64",
                "-ExportDir", str(self.repo / "ev"),
            ])
        self.assertEqual(result, 1)
        # Packaging owns a dedicated root, so its current audit evidence can never be confused
        # with the independent audit gate's tree.
        self.package.assert_called_once_with(
            self.repo, ("x64",), self.output("verify-arch") / "package",
            modules=("renpy", "rpgmaker", "zanzarah"), smoke_architectures=("x64",),
            tools="packaging-tools", runner=self.processes.packaging, host="x64",
        )
        operations = {item.name: item for item in self.export.call_args.args[2]}
        self.assertEqual(operations["package"].status, result_export.FAILED)
        self.assertIn("audit failed", operations["package"].detail)
        names = [path.name for path in operations["package"].reports]
        self.assertEqual(names, ["binskim.sarif", "headers.txt"])
        self.assertIn("audit failed", operations["package"].log)
        # A blocked package never exports a release payload.
        self.assertEqual(self.export.call_args.kwargs["packages"], ())

    def test_verify_arch_package_success_keeps_audit_and_excludes_packages(self) -> None:
        def write_package(repository, architectures, output, **options):
            root = Path(output)
            audit = root / "audit" / "x64" / "renpy" / "binskim.sarif"
            audit.parent.mkdir(parents=True)
            audit.write_text("{}", encoding="utf-8")
            packages = root / "packages"
            (packages / "x64").mkdir(parents=True)
            archive = packages / "x64" / "renpy-3.1.0-x64.zip"
            archive.write_text("zip", encoding="utf-8")
            (packages / "packages.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(archives=(archive,), deferred=())

        self.package.side_effect = write_package
        with mock.patch.object(main, "verify_route", return_value=_FULL_ROUTE):
            result = self.invoke([
                "verify-arch", "-Repository", str(self.repo), "-Arch", "x64",
                "-ExportDir", str(self.repo / "ev"),
            ])
        self.assertEqual(result, 0)
        operations = {item.name: item for item in self.export.call_args.args[2]}
        self.assertEqual(operations["package"].status, result_export.PASSED)
        # The declared archives and manifest live under packages, so they are reports never;
        # the package gate still publishes its own audit evidence and the release payload.
        self.assertEqual(
            [path.name for path in operations["package"].reports], ["binskim.sarif"]
        )
        packages = self.export.call_args.kwargs["packages"]
        self.assertEqual(
            sorted(path.name for path in packages), ["packages.json", "renpy-3.1.0-x64.zip"]
        )

    def test_verify_arch_reports_deferred_and_builds_unrunnable_targets(self) -> None:
        with mock.patch.object(main, "verify_route", return_value=_EMPTY_ROUTE):
            stdout = StringIO()
            with redirect_stdout(stdout):
                result = self.invoke([
                    "verify-arch", "-Repository", str(self.repo), "-Arch", "x64",
                    "-FuzzSeconds", "5", "-LeakWarmup", "1", "-LeakIterations", "1",
                    "-LeakWindows", "3", "-LeakToleranceBytes", "0",
                ])
        self.assertEqual(result, 0)
        self.build.assert_any_call(
            self.repo, ("x64",), ("Debug", "Release"), jobs=None, tools=_NATIVE_TOOLS,
            runner=self.processes.native,
        )
        self.test.assert_not_called()
        self.coverage.assert_not_called()
        self.assertIn("[DEFERRED] tests-x64: host cannot execute x64 tests", stdout.getvalue())

    def test_verify_includes_the_python_coverage_gate(self) -> None:
        with mock.patch.object(main, "verify_route", return_value=_FULL_ROUTE), \
             mock.patch.object(main, "_python_coverage", return_value=(Path("coverage.txt"),)) as coverage:
            result = self.invoke([
                "verify", "-Repository", str(self.repo), "-Arch", "x64",
                "-ExportDir", str(self.repo / "ev"), "-Module", "renpy",
            ])
        self.assertEqual(result, 0)
        coverage.assert_called_once()
        self.assertIn("python-coverage", [item.name for item in self.export.call_args.args[2]])
        # verify-arch must not run the python gate.
        self.export.reset_mock()
        with mock.patch.object(main, "verify_route", return_value=_FULL_ROUTE):
            self.invoke([
                "verify-arch", "-Repository", str(self.repo), "-Arch", "x64",
                "-ExportDir", str(self.repo / "ev2"),
            ])
        self.assertNotIn("python-coverage", [item.name for item in self.export.call_args.args[2]])

    def test_verify_passes_the_corpus_to_coverage(self) -> None:
        with mock.patch.object(main, "verify_route", return_value=_FULL_ROUTE):
            self.invoke([
                "verify-arch", "-Repository", str(self.repo), "-Arch", "x64",
                "-Corpus", str(self.repo / "golden"),
            ])
        self.assertEqual(self.coverage.call_args.kwargs["corpus"], self.repo / "golden")

    def test_verify_retains_the_current_failure_evidence(self) -> None:
        def fail_after_writing(repository, architectures, output, **options):
            report = Path(output) / "analysis.sarif"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("{}", encoding="utf-8")
            raise RuntimeError("source gate failed")

        self.source_checks.side_effect = fail_after_writing
        with mock.patch.object(main, "verify_route", return_value=_EMPTY_ROUTE), \
             redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = self.invoke([
                "verify-arch", "-Repository", str(self.repo), "-Arch", "x64",
                "-ExportDir", str(self.repo / "ev"),
            ])
        self.assertEqual(result, 1)
        operations = {item.name: item for item in self.export.call_args.args[2]}
        self.assertEqual(operations["source"].status, result_export.FAILED)
        self.assertEqual([path.name for path in operations["source"].reports], ["analysis.sarif"])
        self.assertIn("source gate failed", operations["source"].detail)

    def test_verify_arch_refreshes_only_the_selected_arch_report_tree(self) -> None:
        tests_root = self.repo / "out" / "native" / "reports" / "tests"
        stale = tests_root / "x64" / "Debug" / "unit" / "tests.xml"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale success", encoding="utf-8")
        untouched = tests_root / "x86" / "Debug" / "unit" / "tests.xml"
        untouched.parent.mkdir(parents=True)
        untouched.write_text("other arch", encoding="utf-8")

        self.test.side_effect = RuntimeError("early build failure")
        with mock.patch.object(main, "verify_route", return_value=_FULL_ROUTE), \
             redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = self.invoke([
                "verify-arch", "-Repository", str(self.repo), "-Arch", "x64",
                "-ExportDir", str(self.repo / "ev"),
            ])
        self.assertEqual(result, 1)
        operations = {item.name: item for item in self.export.call_args.args[2]}
        self.assertEqual(operations["tests-x64"].status, result_export.FAILED)
        self.assertEqual(operations["tests-x64"].reports, ())
        self.assertIn("early build failure", operations["tests-x64"].log)
        self.assertFalse(stale.exists())
        self.assertTrue(untouched.exists())

    def test_verify_arch_blocks_package_when_an_independent_gate_fails(self) -> None:
        self.source_checks.side_effect = RuntimeError("source gate failed")
        with mock.patch.object(main, "verify_route", return_value=_FULL_ROUTE), \
             redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = self.invoke([
                "verify-arch", "-Repository", str(self.repo), "-Arch", "x64",
                "-ExportDir", str(self.repo / "ev"),
            ])
        self.assertEqual(result, 1)
        operations = {item.name: item for item in self.export.call_args.args[2]}
        self.assertEqual(operations["source"].status, result_export.FAILED)
        self.assertEqual(operations["package"].status, result_export.DEFERRED)
        self.package.assert_not_called()
        self.assertEqual(self.export.call_args.kwargs["packages"], ())


class CrossBuildExportRegressionTests(unittest.TestCase):
    """A cross-built, non-runnable architecture must export a unique ``build-<arch>`` record.

    This exercises the real result exporter (never a mock), because the exporter rejects a
    duplicated operation name: ``verify-arch -Arch x64`` on an x86 host records the cross-build
    as ``build-x64`` and then the routing deferral adds ``tests-x64``, so the record must be named
    ``build-x64`` to leave ``tests-x64`` carrying only the deferred verdict.
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.repo = Path(self._temporary.name)

        self.build = _spec(native_module.build, return_value=())
        self.test = _spec(native_module.test, return_value=())
        self.source_checks = _spec(source_module.source_checks, return_value=())
        self.compiler_analysis = _spec(source_module.compiler_analysis, return_value=())
        self.audit = _spec(packaging_module.audit_binaries, return_value=())
        self.package = _spec(packaging_module.package, return_value=SimpleNamespace(
            archives=(), deferred=()
        ))

        self.processes = _processes()
        self.patches = [
            mock.patch.object(main.native, "locate_tools", return_value=_NATIVE_TOOLS),
            mock.patch.object(main.native, "build", self.build),
            mock.patch.object(main.native, "test", self.test),
            mock.patch.object(main.source_checks, "locate_tools", return_value="source-tools"),
            mock.patch.object(main.source_checks, "locate_analysis_tools", return_value="analysis-tools"),
            mock.patch.object(main.source_checks, "source_checks", self.source_checks),
            mock.patch.object(main.source_checks, "compiler_analysis", self.compiler_analysis),
            mock.patch.object(main.packaging_ops, "locate_packaging_tools", return_value="packaging-tools"),
            mock.patch.object(main.packaging_ops, "audit_binaries", self.audit),
            mock.patch.object(main.packaging_ops, "package", self.package),
            mock.patch.object(main, "detect_host_architecture", return_value="x86"),
            mock.patch.object(main, "_prioritize", return_value=None),
            mock.patch.object(main, "_restore_priority"),
            mock.patch.object(main, "_processes", return_value=self.processes),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_cross_build_exports_a_unique_build_record(self) -> None:
        export = self.repo / "evidence-x64"
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main.main([
                "verify-arch", "-Repository", str(self.repo), "-Arch", "x64",
                "-ExportDir", str(export),
            ])
        self.assertEqual(result, 0, stderr.getvalue())

        manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
        names = [item["name"] for item in manifest["operations"]]
        self.assertEqual(len(names), len(set(names)), names)
        self.assertIn("build-x64", manifest["passed"])
        self.assertNotIn("tests-x64", manifest["passed"])
        self.assertEqual(
            [item["name"] for item in manifest["deferred"]],
            [
                "tests-x64", "package-runtime-x64", "coverage-x64", "asan-x64",
                "ubsan-x64", "leaks-x64", "fuzz-x64",
            ],
        )
        for name in ("source", "compiler-analysis", "audit", "package"):
            self.assertIn(name, manifest["passed"])
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["packages"], [])

        self.build.assert_called_once_with(
            self.repo, ("x64",), ("Debug", "Release"), jobs=None, tools=_NATIVE_TOOLS,
            runner=self.processes.native,
        )
        self.test.assert_not_called()
        self.assertIn("[DEFERRED] tests-x64", stdout.getvalue())


class AuditDirectoryRegressionTests(unittest.TestCase):
    """``verify`` and ``verify-arch`` must create the audit root before auditing.

    ``packaging_ops.audit_binaries`` fails closed unless its output root already exists, and the
    shared ``work`` tree is reset fresh per run, so the audit gate itself must create
    ``work/audit``; otherwise the whole verification aborts with ``output directory must be an
    existing directory`` before any gate evidence is exported.  The real audit work is mocked
    away, but its output boundary is asserted to be a real directory.  The selected x64 target is
    cross-built on an x86 host, so routing defers the runtime gates instead of exercising them.
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.repo = Path(self._temporary.name)

        self.build = _spec(native_module.build, return_value=())
        self.test = _spec(native_module.test, return_value=())
        self.source_checks = _spec(source_module.source_checks, return_value=())
        self.compiler_analysis = _spec(source_module.compiler_analysis, return_value=())
        self.seen_roots: list[Path] = []

        def audit(repository, architectures, output, **options):
            # The real gate fails closed on a missing root, so record whether it existed here.
            self.seen_roots.append(Path(output))
            return ()

        self.audit = _spec(packaging_module.audit_binaries, side_effect=audit)
        self.package = _spec(packaging_module.package, return_value=SimpleNamespace(
            archives=(), deferred=()
        ))

        self.processes = _processes()
        self.patches = [
            mock.patch.object(main.native, "locate_tools", return_value=_NATIVE_TOOLS),
            mock.patch.object(main.native, "build", self.build),
            mock.patch.object(main.native, "test", self.test),
            mock.patch.object(main.source_checks, "locate_tools", return_value="source-tools"),
            mock.patch.object(main.source_checks, "locate_analysis_tools", return_value="analysis-tools"),
            mock.patch.object(main.source_checks, "source_checks", self.source_checks),
            mock.patch.object(main.source_checks, "compiler_analysis", self.compiler_analysis),
            mock.patch.object(main.packaging_ops, "locate_packaging_tools", return_value="packaging-tools"),
            mock.patch.object(main.packaging_ops, "audit_binaries", self.audit),
            mock.patch.object(main.packaging_ops, "package", self.package),
            mock.patch.object(main, "detect_host_architecture", return_value="x86"),
            mock.patch.object(main, "_prioritize", return_value=None),
            mock.patch.object(main, "_restore_priority"),
            mock.patch.object(main, "_processes", return_value=self.processes),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_verify_arch_creates_the_audit_root_before_auditing(self) -> None:
        export = self.repo / "evidence-x64"
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main.main([
                "verify-arch", "-Repository", str(self.repo), "-Arch", "x64",
                "-ExportDir", str(export),
            ])
        self.assertEqual(result, 0, stderr.getvalue())

        audit_root = self.repo / "out" / "reports" / "verify-arch" / "audit"
        self.assertTrue(self.seen_roots, "audit_binaries was never invoked")
        for seen in self.seen_roots:
            self.assertEqual(seen, audit_root)
            self.assertTrue(seen.is_dir(), f"audit ran without an existing root: {seen}")

        manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("audit", manifest["passed"])
        self.assertIn("package", manifest["passed"])
        self.assertEqual(manifest["status"], "success")


class ReportNormalizationTests(unittest.TestCase):
    def test_fuzz_target_directories_export_as_distinct_nested_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            targets = []
            for name, body in (("x64/renpy", "renpy-body"), ("x64/pickle", "pickle-body")):
                directory = root / "work" / "fuzz" / name
                directory.mkdir(parents=True)
                (directory / "status.json").write_text(body, encoding="utf-8")
                (directory / "run.log").write_text(body, encoding="utf-8")
                targets.append(directory)
            records: list[result_export.Operation] = []
            with redirect_stdout(StringIO()):
                self.assertTrue(main._attempt(records, "fuzz", lambda: tuple(targets)))
            self.assertEqual(len(records[0].reports), 4)
            published = main._export(root / "export", "verify-arch", records)
            manifest = json.loads((published / "manifest.json").read_text(encoding="utf-8"))
            paths = sorted(record["path"] for record in manifest["reports"])
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(len(paths), len(set(paths)))
            self.assertIn("reports/fuzz/renpy/run.log", paths)
            self.assertIn("reports/fuzz/pickle/status.json", paths)
            self.assertEqual(
                (published / "reports" / "fuzz" / "renpy" / "status.json").read_text(
                    encoding="utf-8"
                ),
                "renpy-body",
            )
            self.assertEqual(
                (published / "reports" / "fuzz" / "pickle" / "status.json").read_text(
                    encoding="utf-8"
                ),
                "pickle-body",
            )


class HelperTests(unittest.TestCase):
    def test_selection_accepts_lists_rejects_mixed_all_and_unknown(self) -> None:
        parse = main._selection(("x86", "x64"))
        self.assertEqual(parse("x64, x86"), ("x64", "x86"))
        self.assertEqual(parse("all"), ("x86", "x64"))
        self.assertEqual(parse("X64,x64"), ("x64",))
        for value in ("", "x64,", "all,x64", "mips"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                parse(value)

    def test_integer_bounds(self) -> None:
        self.assertEqual(main._integer(1, 10)("5"), 5)
        for value in ("x", "0", "11"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                main._integer(1, 10)(value)
        with self.assertRaisesRegex(argparse.ArgumentTypeError, ">= 1"):
            main._integer(1)("0")

    def test_concise_and_jobs(self) -> None:
        self.assertEqual(main._concise(ValueError("  boom  here ")), "boom here")
        self.assertEqual(main._concise(ValueError("   ")), "ValueError")
        self.assertEqual(main._jobs(None, 4), 4)
        self.assertEqual(main._jobs(3, 4), 3)

    def test_priority_is_set_on_the_current_process_and_restored(self) -> None:
        priority = main._PRIORITIES["below-normal"]
        with (
            mock.patch.object(main.win32api, "GetCurrentProcess", return_value=123),
            mock.patch.object(main.win32process, "GetPriorityClass", return_value=64),
            mock.patch.object(main.win32process, "SetPriorityClass") as setter,
        ):
            state = main._prioritize(priority)
        self.assertEqual(state, (123, 64))
        setter.assert_called_once_with(123, priority)
        self.assertIsNone(main._prioritize(None))
        with mock.patch.object(main.win32process, "SetPriorityClass") as restore:
            main._restore_priority(state)
        restore.assert_called_once_with(123, 64)
        main._restore_priority(None)

    def test_attempt_collects_a_failure_and_keeps_going(self) -> None:
        records: list[result_export.Operation] = []
        self.assertTrue(main._attempt(records, "good", lambda: (Path("a"),)))
        self.assertFalse(main._attempt(
            records, "bad", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        ))
        by_name = {item.name: item for item in records}
        self.assertEqual(by_name["good"].status, result_export.PASSED)
        self.assertEqual(by_name["bad"].status, result_export.FAILED)
        self.assertIn("boom", by_name["bad"].detail)

    def test_attempt_records_the_current_failure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            (evidence / "current.sarif").write_text("{}", encoding="utf-8")
            records: list[result_export.Operation] = []
            main._attempt(
                records, "source",
                lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                evidence=evidence,
            )
        self.assertEqual([path.name for path in records[0].reports], ["current.sarif"])

    def test_attempt_merges_declared_and_current_evidence_with_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            declared = evidence / "junit.xml"
            declared.write_text("<testsuite/>", encoding="utf-8")
            nested = evidence / "Debug" / "unit" / "junit.xml"
            nested.parent.mkdir(parents=True)
            nested.write_text("<testsuite/>", encoding="utf-8")
            leak = evidence / "leaks" / "snapshot.diff"
            leak.parent.mkdir()
            leak.write_text("diff", encoding="utf-8")
            records: list[result_export.Operation] = []
            with redirect_stdout(StringIO()):
                self.assertTrue(
                    main._attempt(records, "tests-x64", lambda: (declared,), evidence=evidence)
                )
        reports = records[0].reports
        # The declared report is also on disk, so the merge must keep it exactly once while
        # retaining the nested JUnit and leak evidence the operation also produced.
        self.assertEqual(len(reports), len(set(reports)))
        self.assertEqual(set(reports), {declared, nested, leak})

    def test_attempt_persists_stdout_on_success_and_failure(self) -> None:
        records: list[result_export.Operation] = []

        def good() -> tuple[Path, ...]:
            print("child stdout ok")
            return (Path("a"),)

        def bad() -> tuple[Path, ...]:
            print("child stdout bad")
            raise RuntimeError("exploded")

        with redirect_stdout(StringIO()):
            self.assertTrue(main._attempt(records, "good", good))
            self.assertFalse(main._attempt(records, "bad", bad))
        by_name = {item.name: item for item in records}
        self.assertEqual(by_name["good"].status, result_export.PASSED)
        self.assertIn("child stdout ok", by_name["good"].log)
        self.assertIn("child stdout bad", by_name["bad"].log)
        self.assertIn("Traceback", by_name["bad"].log)
        self.assertIn("exploded", by_name["bad"].log)

    def test_tee_writes_to_every_stream_and_flushes(self) -> None:
        first, second = StringIO(), StringIO()
        tee = main._Tee(first, second)
        self.assertEqual(tee.write("hello"), 5)
        tee.flush()
        self.assertEqual(first.getvalue(), "hello")
        self.assertEqual(second.getvalue(), "hello")

    def test_files_under_is_flat_sorted_and_tolerant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(main._files_under(root / "absent"), ())
            (root / "x64").mkdir()
            (root / "x64" / "b.zip").write_text("b", encoding="utf-8")
            (root / "packages.json").write_text("{}", encoding="utf-8")
            self.assertEqual(
                [path.name for path in main._files_under(root)], ["packages.json", "b.zip"]
            )

    def test_tests_action_runs_or_only_builds(self) -> None:
        args = argparse.Namespace(corpus=None, jobs=None)
        processes = _processes()
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            with mock.patch.object(main.native, "locate_tools", return_value=_NATIVE_TOOLS), \
                 mock.patch.object(main.native, "test") as test, \
                 mock.patch.object(main.native, "build") as build:
                main._tests_action(repository, args, "x64", ("x64",), processes, "x64")()
                test.assert_called_once_with(
                    repository, ("x64",), ("Debug", "Release"), corpus=None, jobs=None,
                    tools=_NATIVE_TOOLS, runner=processes.native, host="x64",
                )
                main._tests_action(repository, args, "x64", (), processes, "x86")()
                build.assert_called_once_with(
                    repository, ("x64",), ("Debug", "Release"), jobs=None, tools=_NATIVE_TOOLS,
                    runner=processes.native,
                )
                # The selected architecture's report tree is cleared before the build or test.
                self.assertTrue(
                    (repository / "out" / "native" / "reports" / "tests" / "x64").is_dir()
                )

    def test_python_coverage_invokes_the_gate_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "build").mkdir()
            output = repository / "out"
            native = mock.MagicMock()
            processes = main._Processes(native, mock.MagicMock(), mock.MagicMock())
            reports = main._python_coverage(repository, output, processes)
            argv, cwd, env = native.call_args.args
            # The retained gate writes its raw data file into the build directory and then copies
            # it into the output directory, so the two must be distinct or its ``copyfile`` raises
            # SameFileError before the evidence is published.
            self.assertNotEqual(env["UNPACKRAT_BUILD_DIR"], env["UNPACKRAT_OUT_DIR"])
            self.assertTrue(Path(env["UNPACKRAT_BUILD_DIR"]).is_dir())
            self.assertTrue(Path(env["UNPACKRAT_OUT_DIR"]).is_dir())
        self.assertEqual(reports, (
            output / ".coverage", output / "coverage.json", output / "coverage.xml",
            output / "coverage.toml", output / "coverage.txt",
        ))
        self.assertEqual(argv[1:3], ("-m", "core.python_coverage"))
        self.assertEqual(cwd, (repository / "build").resolve())
        self.assertEqual(env["UNPACKRAT_OUT_DIR"], str(output))

    def test_python_coverage_runs_the_real_gate_with_distinct_directories(self) -> None:
        from core import python_coverage as coverage_gate

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            build = repository / "build"
            build.mkdir()
            (build / "pyproject.toml").write_text("[coverage]\n", encoding="utf-8")
            output = repository / "out" / "reports" / "verify-source" / "python-coverage"
            native = mock.MagicMock()

            def run_gate(argv, cwd, env):
                def fake_run(command, **options):
                    if "run" in command:
                        data = Path(command[command.index("--data-file") + 1])
                        data.write_text("raw coverage data", encoding="utf-8")
                    if "-o" in command:
                        Path(command[command.index("-o") + 1]).write_text(
                            "generated\n", encoding="utf-8"
                        )
                    return subprocess.CompletedProcess(command, 0, "coverage report\n")

                with mock.patch.dict(os.environ, env), \
                     mock.patch.object(coverage_gate.subprocess, "run", side_effect=fake_run):
                    coverage_gate.run(build)

            native.side_effect = run_gate
            processes = main._Processes(native, mock.MagicMock(), mock.MagicMock())
            reports = main._python_coverage(repository, output, processes)
            self.assertEqual(reports, (
                output / ".coverage", output / "coverage.json", output / "coverage.xml",
                output / "coverage.toml", output / "coverage.txt",
            ))
            self.assertTrue(all(path.is_file() for path in reports))
            self.assertEqual(
                (output / "coverage.txt").read_text(encoding="utf-8"), "coverage report\n"
            )
            self.assertTrue((output / "work" / ".coverage").is_file())

    def test_files_under_skips_reparse_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "keep").mkdir()
            (root / "keep" / "status.json").write_text("{}", encoding="utf-8")
            linked = root / "skip"
            linked.mkdir()
            (linked / "inner.txt").write_text("x", encoding="utf-8")
            with mock.patch.object(main, "_is_reparse", side_effect=lambda path: Path(path) == linked):
                self.assertEqual([path.name for path in main._files_under(root)], ["status.json"])
            with mock.patch.object(main, "_is_reparse", return_value=True):
                self.assertEqual(main._files_under(root), ())

    def test_evidence_reports_expands_directories_and_keeps_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "fuzz" / "x64" / "renpy"
            target.mkdir(parents=True)
            (target / "status.txt").write_text("0", encoding="utf-8")
            (target / "run.log").write_text("log", encoding="utf-8")
            expanded = main._evidence_reports((target, root / "absent.xml"))
        self.assertEqual(
            sorted(path.name for path in expanded), ["absent.xml", "run.log", "status.txt"]
        )

    def test_reset_output_rebuilds_a_fresh_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            stale = repository / "out" / "reports" / "verify-source" / "old.txt"
            stale.parent.mkdir(parents=True)
            stale.write_text("stale", encoding="utf-8")
            output = main._reset_output(repository, "verify-source")
            self.assertEqual(list(output.iterdir()), [])
            self.assertTrue(main._output_dir(repository, "verify-source").is_dir())

    def test_export_publishes_through_the_result_export_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "evidence"
            published = main._export(
                destination, "verify", (result_export.Operation("source", result_export.PASSED),)
            )
            self.assertEqual(published, destination)
            self.assertTrue((published / "manifest.json").is_file())

    def test_exported_manifest_includes_python_coverage_outputs_and_nested_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "python-coverage"
            output.mkdir()
            names = (".coverage", "coverage.json", "coverage.xml", "coverage.toml", "coverage.txt")
            for name in names:
                (output / name).write_text(name, encoding="utf-8")
            nested = output / "work" / ".coverage"
            nested.parent.mkdir()
            nested.write_text("raw coverage data", encoding="utf-8")
            records: list[result_export.Operation] = []
            with redirect_stdout(StringIO()):
                self.assertTrue(main._attempt(
                    records, "python-coverage",
                    lambda: tuple(output / name for name in names),
                    evidence=output,
                ))
            published = main._export(root / "evidence", "verify", records)
            manifest = json.loads((published / "manifest.json").read_text(encoding="utf-8"))
            paths = {record["path"] for record in manifest["reports"]}
            self.assertEqual(manifest["status"], "success")
            for name in names:
                self.assertIn(f"reports/python-coverage/{name}", paths)
            self.assertIn("reports/python-coverage/work/.coverage", paths)


class ProcessTests(unittest.TestCase):
    def _runner_processes(self, returncode: int, stdout: str):
        runner = mock.MagicMock()
        runner.run.return_value = subprocess.CompletedProcess(["tool"], returncode, stdout)
        return mock.patch.object(main, "WindowsProcessRunner", return_value=runner)

    def test_native_runner_shows_captured_output(self) -> None:
        with self._runner_processes(0, "compiler output"):
            processes = main._processes(None)
            stdout = StringIO()
            with redirect_stdout(stdout):
                processes.native(["tool"])
        self.assertIn("compiler output", stdout.getvalue())

    def test_native_failure_reports_captured_output(self) -> None:
        with self._runner_processes(2, "error text"):
            processes = main._processes(None)
            stdout = StringIO()
            with redirect_stdout(stdout), self.assertRaises(subprocess.CalledProcessError) as raised:
                processes.native(["tool"])
        self.assertIn("error text", stdout.getvalue())
        self.assertEqual(raised.exception.output, "error text")

    def test_tool_runner_shows_captured_output(self) -> None:
        with self._runner_processes(0, "diagnostic output"):
            processes = main._processes(None)
            stdout = StringIO()
            with redirect_stdout(stdout):
                result = processes.tools(["tool"])
        self.assertEqual(result.stdout, "diagnostic output")
        self.assertIn("diagnostic output", stdout.getvalue())

    def test_packaging_runner_shows_captured_output(self) -> None:
        with self._runner_processes(0, "package output"):
            processes = main._processes(None)
            stdout = StringIO()
            with redirect_stdout(stdout):
                processes.packaging(["tool"])
        self.assertIn("package output", stdout.getvalue())

    def test_adapters_translate_one_runner_to_each_contract(self) -> None:
        runner = mock.MagicMock()
        runner.run.return_value = subprocess.CompletedProcess(["tool"], 0, "captured")
        with mock.patch.object(main, "WindowsProcessRunner", return_value=runner) as factory:
            processes = main._processes(64)
        factory.assert_called_once_with(priority_class=64)

        self.assertIsNone(processes.native(["tool", "-x"], Path("cwd"), None))
        tool = processes.tools(["tool"], None, {"K": "V"})
        self.assertEqual((tool.returncode, tool.stdout), (0, "captured"))
        stream = StringIO()
        self.assertIsNone(processes.packaging(["tool"], cwd=Path("cwd"), env=None, stdout=stream))
        self.assertIs(runner.run.call_args.kwargs["stdout"], stream)

    def test_adapters_raise_called_process_error_on_nonzero(self) -> None:
        runner = mock.MagicMock()
        runner.run.return_value = subprocess.CompletedProcess(["tool"], 5, "")
        with mock.patch.object(main, "WindowsProcessRunner", return_value=runner):
            processes = main._processes(None)
        with self.assertRaises(subprocess.CalledProcessError) as native_error:
            processes.native(["tool"])
        self.assertEqual(native_error.exception.returncode, 5)
        with self.assertRaises(subprocess.CalledProcessError) as packaging_error:
            processes.packaging(["tool"])
        self.assertEqual(packaging_error.exception.returncode, 5)
        self.assertEqual(processes.tools(["tool"]).returncode, 5)
        self.assertEqual(processes.tools(["tool"]).stdout, "")


class ContractTests(unittest.TestCase):
    def test_package_keyword_matches_the_real_signature(self) -> None:
        parameters = inspect.signature(main.packaging_ops.package).parameters
        self.assertIn("smoke_architectures", parameters)
        self.assertNotIn("smoke_archs", parameters)

    def test_the_autospec_guard_rejects_a_wrong_keyword(self) -> None:
        guard = _spec(main.packaging_ops.package)
        guard(Path("repo"), ("x64",), Path("out"), smoke_architectures=("x64",))
        with self.assertRaises(TypeError):
            guard(Path("repo"), ("x64",), Path("out"), smoke_archs=("x64",))


class GrammarTests(Harness):
    def test_help_and_removed_flags_are_rejected_without_discovery(self) -> None:
        with redirect_stdout(StringIO()):
            self.assertEqual(self.invoke([]), 0)
        cases = (
            ["build", "-Arch", "mips"], ["build", "-Arch", "all,x64"], ["build", "-Config", "Profile"],
            ["build", "-Jobs", "0"], ["restore", "-RestoreFlavor", "ubsan"], ["fuzz", "-Arch", "arm64"],
            ["fuzz", "-FuzzSeconds", "0"], ["test-leaks", "-Arch", "x86"], ["verify-arch", "-Arch", "all"],
            ["build", "-Arch", "arm64"], ["verify-arch", "-Arch", "arm64"],
            ["build", "-SkipDependencyRestore"], ["verify-arch", "-Arch", "x64", "-PruneCas"],
            ["audit-binaries", "-Module", "bad"],
        )
        for argv in cases:
            with (
                self.subTest(argv=argv),
                redirect_stderr(StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                main.main(argv)
            self.assertEqual(raised.exception.code, 2)
        main.native.locate_tools.assert_not_called()
        main.source_checks.locate_tools.assert_not_called()

    def test_priority_normal_skips_the_priority_side_effect(self) -> None:
        self.invoke(["build", "-Repository", str(self.repo), "-Priority", "normal"])
        main._prioritize.assert_called_once_with(None)


class FailureTests(Harness):
    def test_a_called_process_error_reports_its_exit_code(self) -> None:
        self.build.side_effect = subprocess.CalledProcessError(3, ["msbuild"])
        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(self.invoke(["build", "-Repository", str(self.repo)]), 3)
        self.assertIn("exit code 3", stderr.getvalue())

    def test_an_operation_error_returns_two(self) -> None:
        self.coverage.side_effect = main.diagnostics.DiagnosticsError("no profile")
        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(self.invoke(["test-coverage", "-Repository", str(self.repo)]), 2)
        self.assertIn("no profile", stderr.getvalue())

    def test_a_failed_verification_exports_and_returns_one(self) -> None:
        with mock.patch.object(main, "verify_route", return_value=_FULL_ROUTE):
            self.package.side_effect = RuntimeError("gate failed")
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(self.invoke([
                    "verify-arch", "-Repository", str(self.repo), "-Arch", "x64",
                    "-ExportDir", str(self.repo / "ev"),
                ]), 1)
        operations = {item.name: item for item in self.export.call_args.args[2]}
        self.assertEqual(operations["package"].status, result_export.FAILED)
        self.assertEqual(self.export.call_args.kwargs["packages"], ())
        self.assertIn("source", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

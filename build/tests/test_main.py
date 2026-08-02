from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import runpy
import re
from types import SimpleNamespace
import sys
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import main  # noqa: E402


class FakeDriver:
    instances: list[FakeDriver] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.constructor = (args, kwargs)
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        FakeDriver.instances.append(self)

    def __getattr__(self, name: str):
        async def invoke(*args: object, **kwargs: object) -> tuple[Path, ...]:
            self.calls.append((name, args, kwargs))
            return (Path("out") / name,)
        return invoke


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeDriver.instances.clear()

    def invoke(self, argv: list[str], *, deferred: tuple[object, ...] = ()):
        toolchain = object()
        source_tools = object()
        dumpbin, binskim, umdh = object(), object(), object()
        stdout = StringIO()
        patches = (
            mock.patch.object(main, "BuildDriver", FakeDriver),
            mock.patch.object(main, "_run_id", return_value="run-id"),
            mock.patch.object(main, "discover_msvc_toolchain", return_value=toolchain),
            mock.patch.object(main, "discover_source_tools", return_value=source_tools),
            mock.patch.object(main, "resolve_dumpbin", return_value=dumpbin),
            mock.patch.object(main, "resolve_binskim", return_value=binskim),
            mock.patch.object(main, "resolve_umdh", return_value=umdh),
            mock.patch.object(main, "verify_route", return_value=SimpleNamespace(deferred=deferred)),
        )
        with (
            patches[0] as driver, patches[1], patches[2] as discover,
            patches[3] as source, patches[4] as find_dumpbin,
            patches[5] as find_binskim, patches[6] as find_umdh,
            patches[7] as route, redirect_stdout(stdout),
        ):
            result = main.main(argv)
        return SimpleNamespace(
            result=result, stdout=stdout.getvalue(), driver=driver, discover=discover,
            source=source, dumpbin=find_dumpbin, binskim=find_binskim, umdh=find_umdh,
            route=route, source_tools=source_tools, tools=(dumpbin, binskim, umdh),
        )

    def test_table_routes_every_driver_command_and_preserves_legacy_options(self) -> None:
        repository = Path("selected-repo")
        dumpbin, binskim, umdh = object(), object(), object()
        cases = (
            ("build", ["-Arch", " x64, X64,x86 ", "-Config", "debug, RELEASE"],
             (("x64", "x86"), ("Debug", "Release")), {}),
            ("test", ["-Config", "Release", "-Corpus", "golden", "-TestShards", "7"],
             (("x64",), ("Release",)), {"test_shards": 7, "corpus": Path("golden"), "run_nonce": "run-id"}),
            ("compiler_analysis", [], (("x64",),), {}),
            ("test_coverage", ["-Corpus", "golden"],
             (("x64",),), {"test_shards": 4, "corpus": Path("golden"), "run_nonce": "run-id"}),
            ("test_asan", ["-Arch", "x86,x64"], (("x86", "x64"),), {"test_shards": 4}),
            ("test_ubsan", [], (("x64",),), {"test_shards": 4}),
            ("fuzz", ["-FuzzSeconds", "91", "-FuzzTarget", "RenPy"], (),
             {"run_nonce": "run-id", "seconds": 91, "targets": ("renpy",)}),
            ("verify_source", ["-ExportDir", "source-evidence"], (),
             {"export_dir": Path("source-evidence")}),
            ("verify_arch", ["-Arch", "x86", "-ExportDir", "x86-evidence",
                             "-FuzzSeconds", "17"], (("x86",),),
             {"corpus": None, "run_nonce": "run-id", "fuzz_seconds": 17,
              "test_shards": 4, "warmup": 8, "iterations": 100, "windows": 3,
              "tolerance_bytes": 0, "export_dir": Path("x86-evidence")}),
            ("verify", ["-Arch", "all", "-Corpus", "golden", "-FuzzSeconds", "17",
                        "-LeakWarmup", "2", "-LeakIterations", "5", "-LeakWindows", "4",
                        "-LeakToleranceBytes", "9", "-ExportDir", "all-evidence"],
             (("x86", "x64", "arm64"),),
             {"corpus": Path("golden"), "run_nonce": "run-id", "fuzz_seconds": 17,
              "test_shards": 4, "warmup": 2, "iterations": 5, "windows": 4,
              "tolerance_bytes": 9, "export_dir": Path("all-evidence")}),
        )
        command_names = {
            "compiler_analysis": "compiler-analysis", "test_coverage": "test-coverage",
            "test_asan": "test-asan", "test_ubsan": "test-ubsan",
            "verify_source": "verify-source", "verify_arch": "verify-arch",
        }
        for method, options, positional, keywords in cases:
            with self.subTest(command=method):
                result = self.invoke([command_names.get(method, method), "-Repository", str(repository), *options])
                instance = FakeDriver.instances[-1]
                self.assertEqual(instance.constructor, (
                    (repository, "run-id", mock.ANY),
                    {"jobs": None, "prune_cas": False},
                ))
                self.assertEqual(instance.calls, [(method, positional, keywords)])
                self.assertEqual(result.result, 0)
                self.assertEqual(result.stdout.strip(), str(Path("out") / method))

        restore = self.invoke([
            "restore", "-Repository", str(repository), "-Arch", "arm64,ALL",
            "-RestoreFlavor", "ALL",
        ])
        self.assertEqual(FakeDriver.instances[-1].calls, [
            ("restore", (("x86", "x64", "arm64"),), {"flavors": ("",)}),
            ("restore", (("x86", "x64"),), {"flavors": ("asan",)}),
        ])
        self.invoke(["restore", "-Repository", str(repository)])
        self.assertEqual(FakeDriver.instances[-1].calls, [
            ("restore", (("x64",),), {"flavors": ("",)})
        ])
        no_op = self.invoke([
            "restore", "-Repository", str(repository), "-Arch", "arm64",
            "-RestoreFlavor", "asan",
        ])
        self.assertEqual(FakeDriver.instances[-1].calls, [])
        self.assertEqual(no_op.stdout, "")

        source = self.invoke(["source-checks", "-Repository", str(repository)])
        self.assertEqual(FakeDriver.instances[-1].calls, [
            ("source_checks", (("x64",), source.source_tools), {})
        ])
        source.source.assert_called_once()

        for command, method in (("audit-binaries", "audit"), ("package", "package")):
            with self.subTest(command=command):
                options = [command, "-Repository", str(repository), "-Jobs", "3"]
                if command == "package":
                    options.extend(("-ExportDir", "package-evidence"))
                result = self.invoke(options)
                dumpbin, binskim, _umdh = result.tools
                self.assertEqual(FakeDriver.instances[-1].calls, [
                    (method, (("x64",),), {
                        "dumpbin": dumpbin,
                        "binskim": binskim,
                        **({"export_dir": Path("package-evidence")} if command == "package" else {}),
                    })
                ])

        leak = self.invoke([
            "test-leaks", "-Repository", str(repository), "-LeakWarmup", "2",
            "-LeakIterations", "6", "-LeakWindows", "5", "-LeakToleranceBytes", "10",
        ])
        dumpbin, binskim, umdh = leak.tools
        self.assertEqual(FakeDriver.instances[-1].calls, [
            ("test_leaks", (), {
                "run_nonce": "run-id", "dumpbin": dumpbin, "binskim": binskim, "umdh": umdh,
                "warmup": 2, "iterations": 6, "windows": 5, "tolerance_bytes": 10,
            })
        ])

    def test_doctor_and_clean_bypass_toolchain_and_driver(self) -> None:
        with (
            mock.patch.object(main, "doctor_main", return_value=1) as doctor,
            mock.patch.object(main, "discover_msvc_toolchain") as discover,
        ):
            self.assertEqual(main.main(["doctor"]), 1)
        doctor.assert_called_once_with(())
        discover.assert_not_called()

        with mock.patch.object(main, "run_clean", return_value=0) as clean:
            self.assertEqual(main.main([
                "clean", "-Repository", "repo", "-CleanMode", "stale-work"
            ]), 0)
        clean.assert_called_once_with(Path("repo"), "stale-work")
        self.assertEqual(FakeDriver.instances, [])

    def test_clean_adapter_and_run_identifier_are_exact(self) -> None:
        with mock.patch("core.clean.main", return_value=0) as clean:
            self.assertEqual(main.run_clean(Path("repo"), "all"), 0)
        clean.assert_called_once_with((str(Path("repo")), "--mode", "all"))
        self.assertRegex(main._run_id(), re.compile(r"^\d{8}T\d{6}-\d+$"))

    def test_verify_prints_explicit_host_deferrals_before_outputs(self) -> None:
        deferred = SimpleNamespace(gate="tests", architecture="arm64", reason="not runnable")
        result = self.invoke(["verify", "-Arch", "arm64"], deferred=(deferred,))
        self.assertEqual(
            result.stdout.splitlines(),
            ["[DEFERRED] tests arm64: not runnable", str(Path("out") / "verify")],
        )

        async def fail(*_args: object, **_kwargs: object) -> tuple[Path, ...]:
            raise RuntimeError("verify failed")

        stdout = StringIO()
        with (
            mock.patch.dict(
                main._COMMANDS,
                {"verify": (main._COMMANDS["verify"][0], fail)},
            ),
            mock.patch.object(main, "verify_route", return_value=SimpleNamespace(deferred=(deferred,))),
            mock.patch.object(main, "discover_msvc_toolchain", return_value=object()),
            mock.patch.object(main, "BuildDriver", FakeDriver),
            mock.patch.object(main, "_run_id", return_value="run-id"),
            redirect_stdout(stdout), self.assertRaisesRegex(RuntimeError, "verify failed"),
        ):
            main.main(["verify", "-Arch", "arm64"])
        self.assertEqual(stdout.getvalue(), "")

    def test_verify_prune_cas_flag_enables_driver_sweep(self) -> None:
        result = self.invoke([
            "verify-arch", "-Arch", "x64", "-PruneCas",
        ])

        instance = FakeDriver.instances[-1]
        self.assertEqual(
            instance.constructor,
            ((Path(__file__).parents[2], "run-id", mock.ANY), {
                "jobs": None,
                "prune_cas": True,
            }),
        )
        self.assertEqual(result.result, 0)

    def test_help_and_removed_skip_restore_contract(self) -> None:
        for argv in ([], ["help"]):
            with self.subTest(argv=argv), redirect_stdout(StringIO()) as stdout:
                self.assertEqual(main.main(argv), 0)
            self.assertIn("audit-binaries", stdout.getvalue())

        for command in ("build", "restore"):
            with (
                self.subTest(command=command),
                redirect_stderr(StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                main.main([command, "-SkipDependencyRestore"])
            self.assertEqual(raised.exception.code, 2)

    def test_invalid_legacy_options_fail_before_discovery(self) -> None:
        cases = (
            ["build", "-Arch", "mips"],
            ["build", "-Arch", ""], ["build", "-Config", "Profile"],
            ["restore", "-RestoreFlavor", "ubsan"], ["fuzz", "-FuzzSeconds", "0"],
            ["fuzz", "-FuzzSeconds", "86401"], ["fuzz", "-FuzzTarget", "bad"],
            ["fuzz", "-Arch", "x86"], ["test-leaks", "-Arch", "x86"],
            ["test-leaks", "-LeakWarmup", "0"], ["test-leaks", "-LeakIterations", "x"],
            ["test-leaks", "-LeakWindows", "2"], ["test-leaks", "-LeakWindows", "11"],
            ["test-leaks", "-LeakToleranceBytes", "-1"],
            ["test-coverage", "-CoverageThreshold", "99"],
            ["test-coverage", "-CoverageThreshold", "100"],
            ["test-coverage", "-CoverageThreshold", "100.0"],
            ["test", "-TestShards", "0"], ["build", "-Jobs", "0"],
            ["verify-arch", "-Arch", "all"],
        )
        for argv in cases:
            with (
                self.subTest(argv=argv),
                mock.patch.object(main, "discover_msvc_toolchain") as discover,
                redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised,
            ):
                main.main(argv)
            self.assertEqual(raised.exception.code, 2)
            discover.assert_not_called()

    def test_script_entry_point_uses_the_same_cli(self) -> None:
        with (
            mock.patch.object(sys, "argv", [str(BUILD_ROOT / "main.py"), "doctor"]),
            mock.patch("core.doctor.main", return_value=0) as doctor,
            self.assertRaises(SystemExit) as raised,
        ):
            runpy.run_path(str(BUILD_ROOT / "main.py"), run_name="__main__")
        self.assertEqual(raised.exception.code, 0)
        doctor.assert_called_once_with(())

    def test_root_powershell_entry_point_uses_frozen_project_environment(self) -> None:
        script = (BUILD_ROOT.parent / "build.ps1").read_text(encoding="utf-8")

        self.assertIn("uv run --project $buildProject --frozen --no-sync", script)
        self.assertIn("$PSScriptRoot 'build'", script)
        self.assertNotIn("tools\\build", script)
        self.assertIn("exit $LASTEXITCODE", script)
        self.assertNotIn("build\\build.ps1", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)

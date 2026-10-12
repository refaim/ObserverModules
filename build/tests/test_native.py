from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET


BUILD_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BUILD_ROOT.parent
sys.path.insert(0, str(BUILD_ROOT))

import native  # noqa: E402


MSBUILD = "{http://schemas.microsoft.com/developer/msbuild/2003}"


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


@dataclass
class RecordedCall:
    argv: tuple[str, ...]
    cwd: Path | None
    env: dict[str, str] | None


class RecordingRunner:
    """Records every tool invocation and can be told to fail one of them."""

    def __init__(self, failure: type[BaseException] | None = None) -> None:
        self.calls: list[RecordedCall] = []
        self._failure = failure

    def __call__(self, argv, cwd=None, env=None) -> None:
        self.calls.append(RecordedCall(tuple(argv), cwd, None if env is None else dict(env)))
        if self._failure is not None:
            raise self._failure


@dataclass(frozen=True)
class FakeTools:
    msbuild: Path
    vcpkg: Path


def _tools(root: Path) -> FakeTools:
    return FakeTools(
        msbuild=_executable(root / "tools" / "MSBuild.exe"),
        vcpkg=_executable(root / "tools" / "vcpkg" / "vcpkg.exe"),
    )


class LayoutTests(unittest.TestCase):
    def test_stable_layout_lives_under_out_native(self) -> None:
        repository = Path("C:/work/repo")

        self.assertEqual(
            native.artifacts_root(repository),
            repository / "out" / "native",
        )
        self.assertEqual(
            native.restore_root(repository, "x64"),
            repository / "out" / "native" / "vcpkg_installed" / "x64",
        )
        self.assertEqual(
            native.bin_directory(repository, "arm64", "Release"),
            repository / "out" / "native" / "bin" / "arm64" / "Release",
        )

    def test_artifacts_are_the_shipped_module_and_test_binaries(self) -> None:
        repository = Path("C:/work/repo")
        directory = repository / "out" / "native" / "bin" / "x86" / "Debug"

        self.assertEqual(
            native.artifacts(repository, "x86", "Debug"),
            (
                directory / "renpy.so",
                directory / "rpgmaker.so",
                directory / "zanzarah.so",
                directory / "tests.exe",
            ),
        )
        self.assertEqual(
            native.test_executable(repository, "x86", "Debug"),
            directory / "tests.exe",
        )


class RestoreCommandTests(unittest.TestCase):
    def test_restore_installs_pinned_triplet_into_the_stable_per_arch_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            tools = _tools(repository)

            command = native.restore_command(repository, "x86", tools)

            scratch = repository / "out" / "native" / "vcpkg_scratch" / "x86"
            self.assertEqual(
                command,
                (
                    str(tools.vcpkg),
                    "install",
                    f"--x-install-root={repository / 'out' / 'native' / 'vcpkg_installed' / 'x86'}",
                    f"--x-buildtrees-root={scratch / 'buildtrees'}",
                    f"--x-packages-root={scratch / 'packages'}",
                    "--triplet",
                    "observer-x86-windows-static",
                    f"--x-manifest-root={repository}",
                    f"--overlay-triplets={repository / 'build' / 'vcpkg' / 'triplets'}",
                ),
            )


class BuildCommandTests(unittest.TestCase):
    def test_build_drives_observer_proj_with_the_platform_and_artifact_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            tools = _tools(repository)
            artifacts = repository / "out" / "native"

            command = native.build_command(repository, "arm64", "Release", 6, tools)

            self.assertEqual(
                command,
                (
                    str(tools.msbuild),
                    str(repository / "build" / "Observer.proj"),
                    "/nologo",
                    "/m:6",
                    "/nr:false",
                    "/t:Build",
                    "/p:Configuration=Release",
                    "/p:Platform=ARM64",
                    f"/p:ArtifactsRoot={artifacts}{os.sep}",
                    f"/p:VcpkgRoot={tools.vcpkg.parent}",
                    "/p:VcpkgManifestInstall=false",
                ),
            )

    def test_every_architecture_maps_to_its_msbuild_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            tools = _tools(repository)

            platforms = {
                architecture: native.build_command(repository, architecture, "Debug", 1, tools)[7]
                for architecture in native.ARCHITECTURES
            }

            self.assertEqual(
                platforms,
                {
                    "x86": "/p:Platform=Win32",
                    "x64": "/p:Platform=x64",
                    "arm64": "/p:Platform=ARM64",
                },
            )


class TestCommandTests(unittest.TestCase):
    def test_unit_suite_runs_the_test_binary_from_its_binary_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            report = repository / "out" / "native" / "reports" / "tests.xml"

            command = native.test_command(repository, "x64", "Debug", report, None)

            self.assertEqual(
                command,
                (
                    str(repository / "out" / "native" / "bin" / "x64" / "Debug" / "tests.exe"),
                    "--reporter",
                    "compact",
                    "--reporter",
                    f"JUnit::out={report}",
                    "--durations",
                    "yes",
                    "--order",
                    "lex",
                ),
            )

    def test_corpus_suite_selects_only_the_compatibility_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            report = repository / "out" / "native" / "reports" / "corpus.xml"

            command = native.test_command(repository, "x64", "Release", report, Path("C:/corpus"))

            self.assertEqual(command[1], "[compatibility]")
            self.assertIn(f"JUnit::out={report}", command)


class SelectionTests(unittest.TestCase):
    def test_architecture_and_configuration_selections_accept_all_and_lists(self) -> None:
        architecture = native._selection(native.ARCHITECTURES)
        configuration = native._selection(native.CONFIGURATIONS)

        self.assertEqual(architecture("all"), native.ARCHITECTURES)
        self.assertEqual(architecture("x86,x64"), ("x86", "x64"))
        self.assertEqual(architecture("X64,x64"), ("x64",))
        self.assertEqual(configuration("debug"), ("Debug",))

    def test_a_bad_selection_names_the_supported_values(self) -> None:
        architecture = native._selection(native.ARCHITECTURES)

        with self.assertRaises(argparse.ArgumentTypeError):
            architecture("sparc")

    def test_all_is_only_accepted_on_its_own(self) -> None:
        for choices in (native.ARCHITECTURES, native.CONFIGURATIONS):
            parse = native._selection(choices)

            self.assertEqual(parse("all"), choices)
            with self.assertRaises(argparse.ArgumentTypeError):
                parse(f"all,{choices[0]}")
            with self.assertRaises(argparse.ArgumentTypeError):
                parse("all,sparc")
            with self.assertRaises(argparse.ArgumentTypeError):
                parse("all,all")

    def test_empty_and_invalid_tokens_are_rejected_consistently(self) -> None:
        for choices in (native.ARCHITECTURES, native.CONFIGURATIONS):
            parse = native._selection(choices)

            for value in ("", ",", f"{choices[0]},", f",{choices[0]}", f"{choices[0]},Typo"):
                with self.subTest(choices=choices, value=value):
                    with self.assertRaises(argparse.ArgumentTypeError):
                        parse(value)

    def test_a_job_count_must_be_a_positive_integer(self) -> None:
        self.assertEqual(native._positive_integer("4"), 4)
        with self.assertRaises(argparse.ArgumentTypeError):
            native._positive_integer("0")
        with self.assertRaises(argparse.ArgumentTypeError):
            native._positive_integer("many")


class RelativeRepositoryTests(unittest.TestCase):
    """Public entry points resolve the repository before generating argv or cwd."""

    def _relative(self, repository: Path) -> Path:
        return Path(os.path.relpath(repository, Path.cwd()))

    def test_build_resolves_a_relative_repository_for_argv_outputs_and_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            tools = _tools(repository)
            runner = RecordingRunner()

            outputs = native.build(
                self._relative(repository), ("x64",), ("Debug",), tools=tools, runner=runner
            )

            self.assertEqual(outputs, native.artifacts(repository, "x64", "Debug"))
            build_argv = runner.calls[-1].argv
            self.assertIn(str(repository / "build" / "Observer.proj"), build_argv)
            self.assertIn(
                f"/p:ArtifactsRoot={repository / 'out' / 'native'}{os.sep}", build_argv
            )
            for call in runner.calls:
                self.assertEqual(call.cwd, repository)

    def test_restore_resolves_a_relative_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            tools = _tools(repository)
            runner = RecordingRunner()

            roots = native.restore(
                self._relative(repository), ("x86",), tools=tools, runner=runner
            )

            expected = native.restore_root(repository, "x86")
            self.assertEqual(roots, (expected,))
            install_root = next(
                item for item in runner.calls[0].argv if item.startswith("--x-install-root=")
            )
            self.assertEqual(install_root, f"--x-install-root={expected}")
            self.assertEqual(runner.calls[0].cwd, repository)

    def test_test_resolves_a_relative_repository_for_the_report_and_binary_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            (repository / "build" / "projects").mkdir(parents=True)
            tools = _tools(repository)
            runner = RecordingRunner()

            reports = native.test(
                self._relative(repository), ("x64",), ("Debug",), host="x64",
                tools=tools, runner=runner,
            )

            expected = (
                repository / "out" / "native" / "reports" / "tests"
                / "x64" / "Debug" / "unit" / "tests.xml"
            )
            self.assertEqual(reports, (expected,))
            self.assertEqual(runner.calls[-1].cwd, native.bin_directory(repository, "x64", "Debug"))


class ValidationTests(unittest.TestCase):
    def test_unknown_architectures_and_configurations_are_rejected_without_running_a_tool(self) -> None:
        runner = RecordingRunner()
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            tools = _tools(repository)

            with self.assertRaisesRegex(ValueError, "architecture"):
                native.build(repository, ("sparc",), ("Debug",), tools=tools, runner=runner)
            with self.assertRaisesRegex(ValueError, "configuration"):
                native.build(repository, ("x64",), ("RelWithDebInfo",), tools=tools, runner=runner)
            with self.assertRaisesRegex(ValueError, "architecture"):
                native.build(repository, (), ("Debug",), tools=tools, runner=runner)
            with self.assertRaisesRegex(ValueError, "configuration"):
                native.build(repository, ("x64",), (), tools=tools, runner=runner)

        self.assertEqual(runner.calls, [])


class HostTests(unittest.TestCase):
    def test_machine_names_canonicalise_to_a_supported_host(self) -> None:
        self.assertEqual(native.host_architecture("AMD64"), "x64")
        self.assertEqual(native.host_architecture("x86_64"), "x64")
        self.assertEqual(native.host_architecture("ARM64"), "arm64")
        self.assertEqual(native.host_architecture("aarch64"), "arm64")
        self.assertEqual(native.host_architecture("i686"), "x86")

    def test_an_unknown_host_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported Windows host architecture"):
            native.host_architecture("mips64")

    def test_runtime_tests_refuse_targets_the_host_cannot_execute(self) -> None:
        native.require_runnable(("x86", "x64"), "x64")
        native.require_runnable(("x86", "x64", "arm64"), "arm64")
        with self.assertRaisesRegex(ValueError, "cannot run"):
            native.require_runnable(("arm64",), "x64")
        with self.assertRaisesRegex(ValueError, "cannot run"):
            native.require_runnable(("x64",), "x86")
        with self.assertRaisesRegex(ValueError, "unsupported host architecture"):
            native.require_runnable(("x64",), "sparc")

    def test_the_host_policy_is_delegated_to_core_host(self) -> None:
        from core import host as host_policy

        for machine in ("AMD64", "x86_64", "arm64", "aarch64", "x86", "i386", "i686"):
            self.assertEqual(
                native.host_architecture(machine),
                host_policy.detect_host_architecture(machine),
            )

        # One policy only: the duplicated alias table is gone and no longer accepts
        # machine names that ``core.host`` rejects.
        self.assertFalse(hasattr(native, "_MACHINE_ALIASES"))
        self.assertFalse(hasattr(native, "_HOST_CAPABILITIES"))
        with self.assertRaisesRegex(ValueError, "unsupported Windows host architecture"):
            native.host_architecture("em64t")

    def test_runnable_acceptance_matches_core_host(self) -> None:
        from core import host as host_policy

        def rejects(action) -> bool:
            try:
                action()
            except (ValueError, RuntimeError):
                return True
            return False

        for host in ("x86", "x64", "arm64", "sparc"):
            for requested in (("x86",), ("arm64",), ("x86", "x64"), ("x86", "x64", "arm64")):
                self.assertEqual(
                    rejects(lambda: native.require_runnable(requested, host)),
                    rejects(lambda: host_policy.require_runnable(requested, host)),
                    msg=f"{host} {requested}",
                )


class ToolDiscoveryTests(unittest.TestCase):
    def test_vswhere_is_located_under_program_files_x86(self) -> None:
        environ = {"ProgramFiles(x86)": "C:/Program Files (x86)"}

        self.assertEqual(
            native.vswhere_path(environ),
            Path("C:/Program Files (x86)") / "Microsoft Visual Studio" / "Installer" / "vswhere.exe",
        )
        with self.assertRaises(FileNotFoundError):
            native.vswhere_path({})

    def test_msbuild_is_resolved_from_the_vswhere_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installation = Path(temporary) / "Visual Studio"
            amd64 = _executable(
                installation / "MSBuild" / "Current" / "Bin" / "amd64" / "MSBuild.exe"
            )
            recorded: list[tuple[str, ...]] = []

            def run(argv, **kwargs):
                recorded.append(tuple(argv))
                return subprocess.CompletedProcess(argv, 0, stdout=f"{installation}\r\n", stderr="")

            resolved = native.find_msbuild(Path("C:/vswhere.exe"), run=run)

            self.assertEqual(resolved, amd64)
            self.assertIn("-requires", recorded[0])
            self.assertIn("installationPath", recorded[0])

    def test_msbuild_falls_back_to_the_plain_binary_and_rejects_empty_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installation = Path(temporary) / "Visual Studio"
            plain = _executable(installation / "MSBuild" / "Current" / "Bin" / "MSBuild.exe")

            def run(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, stdout=f"{installation}\n", stderr="")

            self.assertEqual(native.find_msbuild(Path("C:/vswhere.exe"), run=run), plain)

        with self.assertRaisesRegex(RuntimeError, "Visual Studio"):
            native.find_msbuild(
                Path("C:/vswhere.exe"),
                run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="\n", stderr=""),
            )

    def test_msbuild_query_requires_the_cpp_build_tools_and_not_llvm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installation = Path(temporary) / "Visual Studio"
            _executable(installation / "MSBuild" / "Current" / "Bin" / "amd64" / "MSBuild.exe")
            recorded: list[tuple[str, ...]] = []

            def run(argv, **kwargs):
                recorded.append(tuple(argv))
                return subprocess.CompletedProcess(argv, 0, stdout=f"{installation}\n", stderr="")

            native.find_msbuild(Path("C:/vswhere.exe"), run=run)

        argv = recorded[0]
        requires = [argv[index + 1] for index, token in enumerate(argv) if token == "-requires"]
        self.assertIn("Microsoft.Component.MSBuild", requires)
        self.assertIn("Microsoft.VisualStudio.Component.VC.Tools.x86.x64", requires)
        self.assertNotIn("Microsoft.VisualStudio.Component.VC.Llvm.Clang", requires)

    def test_vcpkg_requires_a_real_root_and_refuses_a_path_shim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "vcpkg"
            _executable(root / "vcpkg.exe")
            (root / "scripts" / "buildsystems" / "msbuild").mkdir(parents=True, exist_ok=True)
            (root / "scripts" / "buildsystems" / "msbuild" / "vcpkg.props").write_text(
                "<Project />\n", encoding="utf-8"
            )

            self.assertEqual(native.find_vcpkg({"VCPKG_ROOT": str(root)}), (root / "vcpkg.exe").resolve())

        with self.assertRaises(FileNotFoundError):
            native.find_vcpkg({})
        with self.assertRaises(FileNotFoundError):
            native.find_vcpkg({"VCPKG_ROOT": "C:/shims/vcpkg"})

    def test_locate_tools_composes_vswhere_and_vcpkg_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program_files = root / "Program Files (x86)"
            vswhere = _executable(
                program_files / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
            )
            installation = root / "Visual Studio"
            msbuild = _executable(
                installation / "MSBuild" / "Current" / "Bin" / "amd64" / "MSBuild.exe"
            )
            vcpkg_root = root / "vcpkg"
            vcpkg = _executable(vcpkg_root / "vcpkg.exe")
            (vcpkg_root / "scripts" / "buildsystems" / "msbuild").mkdir(parents=True)
            (vcpkg_root / "scripts" / "buildsystems" / "msbuild" / "vcpkg.props").write_text(
                "<Project />\n", encoding="utf-8"
            )
            environ = {"ProgramFiles(x86)": str(program_files), "VCPKG_ROOT": str(vcpkg_root)}

            def run(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, stdout=f"{installation}\n", stderr="")

            tools = native.locate_tools(environ=environ, run=run)

            self.assertEqual(tools.msbuild, msbuild)
            self.assertEqual(tools.vcpkg, vcpkg)
            self.assertEqual(tools.vcpkg_root, vcpkg_root)
            self.assertTrue(vswhere.is_file())

    def test_a_missing_required_binary_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environ = {"ProgramFiles(x86)": temporary}

            with self.assertRaisesRegex(FileNotFoundError, "vswhere.exe"):
                native.locate_tools(environ=environ)


class BuildTests(unittest.TestCase):
    def test_build_restores_then_compiles_each_arch_and_config_and_returns_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            tools = _tools(repository)
            runner = RecordingRunner()

            outputs = native.build(
                repository, ("x64", "arm64"), ("Debug", "Release"), jobs=3,
                tools=tools, runner=runner,
            )

            # Every dependency tree is restored before any compilation starts.
            expected_commands = [
                native.restore_command(repository, architecture, tools)
                for architecture in ("x64", "arm64")
            ]
            for architecture in ("x64", "arm64"):
                for configuration in ("Debug", "Release"):
                    expected_commands.append(
                        native.build_command(repository, architecture, configuration, 3, tools)
                    )
            self.assertEqual(tuple(call.argv for call in runner.calls), tuple(expected_commands))
            self.assertTrue(all(call.cwd == repository for call in runner.calls))
            self.assertTrue(all(call.env is None for call in runner.calls))

            expected_artifacts = tuple(
                artifact
                for architecture in ("x64", "arm64")
                for configuration in ("Debug", "Release")
                for artifact in native.artifacts(repository, architecture, configuration)
            )
            self.assertEqual(outputs, expected_artifacts)

    def test_build_defaults_jobs_to_the_cpu_count_and_rejects_a_zero_job_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            tools = _tools(repository)
            runner = RecordingRunner()

            native.build(repository, ("x64",), ("Debug",), tools=tools, runner=runner)
            self.assertEqual(runner.calls[1].argv[3], f"/m:{os.cpu_count() or 1}")

            with self.assertRaisesRegex(ValueError, "jobs"):
                native.build(repository, ("x64",), ("Debug",), jobs=0, tools=tools, runner=runner)

    def test_build_does_not_swallow_a_failing_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            tools = _tools(repository)
            failure = subprocess.CalledProcessError(7, "MSBuild.exe")

            with self.assertRaises(subprocess.CalledProcessError) as raised:
                native.build(
                    repository, ("x64",), ("Debug",),
                    tools=tools, runner=RecordingRunner(failure),
                )
            self.assertEqual(raised.exception.returncode, 7)


class TestRunTests(unittest.TestCase):
    def _tools_and_runner(self, repository: Path) -> tuple[FakeTools, RecordingRunner]:
        return _tools(repository), RecordingRunner()

    def test_unit_test_builds_compiles_runs_from_the_binary_directory_and_returns_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "build" / "projects").mkdir(parents=True)
            tools, runner = self._tools_and_runner(repository)

            reports = native.test(
                repository, ("x64",), ("Debug",), host="x64", tools=tools, runner=runner,
            )

            expected_report = (
                repository / "out" / "native" / "reports" / "tests" / "x64" / "Debug" / "unit" / "tests.xml"
            )
            self.assertEqual(reports, (expected_report,))
            self.assertTrue(expected_report.parent.is_dir())
            run = runner.calls[-1]
            self.assertEqual(
                run.argv,
                native.test_command(
                    repository, "x64", "Debug", expected_report, None,
                ),
            )
            self.assertEqual(run.cwd, native.bin_directory(repository, "x64", "Debug"))
            self.assertIsNone(run.env)

    def _report(self, repository: Path, architecture: str, configuration: str, scope: str) -> Path:
        return (
            repository / "out" / "native" / "reports" / "tests"
            / architecture / configuration / scope / "tests.xml"
        )

    def test_corpus_test_runs_the_ordinary_suite_then_appends_the_compatibility_suite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            corpus = repository / "corpus"
            corpus.mkdir()
            tools, runner = self._tools_and_runner(repository)

            reports = native.test(
                repository, ("x86",), ("Release",), corpus=corpus, host="x64",
                tools=tools, runner=runner,
            )

            unit = self._report(repository, "x86", "Release", "unit")
            compatibility = self._report(repository, "x86", "Release", "corpus")
            self.assertEqual(reports, (unit, compatibility))

            unit_run, corpus_run = runner.calls[-2], runner.calls[-1]
            self.assertEqual(
                unit_run.argv,
                native.test_command(repository, "x86", "Release", unit, None),
            )
            # The ordinary suite must never inherit the compatibility corpus environment.
            self.assertIsNone(unit_run.env)
            self.assertEqual(corpus_run.argv[1], "[compatibility]")
            self.assertEqual(corpus_run.env, {"OBSERVER_TEST_CORPUS": str(corpus)})

    def test_every_variant_runs_its_ordinary_suite_even_with_a_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            corpus = repository / "corpus"
            corpus.mkdir()
            tools, runner = self._tools_and_runner(repository)

            reports = native.test(
                repository, ("x86", "x64"), ("Debug", "Release"), corpus=corpus, host="x64",
                tools=tools, runner=runner,
            )

            expected = tuple(
                self._report(repository, architecture, configuration, scope)
                for architecture in ("x86", "x64")
                for configuration in ("Debug", "Release")
                for scope in ("unit", "corpus")
            )
            self.assertEqual(reports, expected)
            # Every unit suite precedes its compatibility suite for the same variant.
            runs = runner.calls[-(2 * 4):]
            self.assertEqual(
                ["corpus" if call.argv[1] == "[compatibility]" else "unit" for call in runs],
                ["unit", "corpus"] * 4,
            )

    def test_a_failure_in_the_compatibility_suite_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            corpus = repository / "corpus"
            corpus.mkdir()
            tools = _tools(repository)
            seen: list[tuple[str, ...]] = []

            def runner(argv, cwd=None, env=None):
                seen.append(tuple(argv))
                if "[compatibility]" in argv:
                    raise subprocess.CalledProcessError(5, "tests.exe")

            with self.assertRaises(subprocess.CalledProcessError) as raised:
                native.test(
                    repository, ("x64",), ("Debug",), corpus=corpus, host="x64",
                    tools=tools, runner=runner,
                )
            self.assertEqual(raised.exception.returncode, 5)
            # The ordinary suite ran first and succeeded before the compatibility suite failed.
            self.assertTrue(any("[compatibility]" not in argv for argv in seen))

    def test_the_ordinary_suite_failure_propagates_before_the_compatibility_suite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            corpus = repository / "corpus"
            corpus.mkdir()
            tools = _tools(repository)
            seen: list[tuple[str, ...]] = []
            ordinary = str(native.test_executable(repository, "x64", "Debug"))

            def runner(argv, cwd=None, env=None):
                seen.append(tuple(argv))
                # Only the ordinary suite fails: the vcpkg restore and MSBuild steps before it
                # must run, so the test actually exercises the ordinary invocation.
                if argv[0] == ordinary and "[compatibility]" not in argv:
                    raise subprocess.CalledProcessError(4, "tests.exe")

            with self.assertRaises(subprocess.CalledProcessError) as raised:
                native.test(
                    repository, ("x64",), ("Debug",), corpus=corpus, host="x64",
                    tools=tools, runner=runner,
                )
            self.assertEqual(raised.exception.returncode, 4)
            # The ordinary suite actually ran and failed; the compatibility suite never did.
            self.assertEqual(
                len([
                    argv for argv in seen
                    if argv[0] == ordinary and "[compatibility]" not in argv
                ]),
                1,
            )
            self.assertFalse(any("[compatibility]" in argv for argv in seen))

    def test_a_corpus_path_must_be_an_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            tools, runner = self._tools_and_runner(repository)

            with self.assertRaises(OSError):
                native.test(
                    repository, ("x64",), ("Debug",), corpus=repository / "missing",
                    host="x64", tools=tools, runner=runner,
                )
            self.assertEqual(runner.calls, [])

    def test_a_corpus_path_that_is_a_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            corpus = repository / "corpus.txt"
            corpus.write_text("not a directory", encoding="utf-8")
            tools, runner = self._tools_and_runner(repository)

            with self.assertRaises(NotADirectoryError):
                native.test(
                    repository, ("x64",), ("Debug",), corpus=corpus,
                    host="x64", tools=tools, runner=runner,
                )
            self.assertEqual(runner.calls, [])

    def test_runtime_tests_refuse_an_arch_the_host_cannot_execute(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            tools, runner = self._tools_and_runner(repository)

            with self.assertRaisesRegex(ValueError, "cannot run"):
                native.test(
                    repository, ("arm64",), ("Release",), host="x64", tools=tools, runner=runner,
                )
            self.assertEqual(runner.calls, [])


class RunToolTests(unittest.TestCase):
    def test_the_real_runner_executes_and_merges_the_environment(self) -> None:
        native.run_tool([sys.executable, "-c", "raise SystemExit(0)"])
        with tempfile.TemporaryDirectory() as temporary:
            native.run_tool(
                [sys.executable, "-c", "import os; assert os.environ['OBSERVER_PROBE'] == '1'"],
                cwd=Path(temporary), env={"OBSERVER_PROBE": "1"},
            )

    def test_the_real_runner_always_passes_an_explicit_inherited_environment(self) -> None:
        # MSBuild fails with MSB6001 when it inherits the raw Windows block, which carries both
        # PATH and Path; an explicit merged mapping is normalised by os.environ's case-folding.
        with mock.patch.object(native.subprocess, "run") as run:
            native.run_tool(["tool.exe", "arg"])
            native.run_tool(["tool.exe", "arg"], env={"OBSERVER_PROBE": "1"})

        inherited = run.call_args_list[0].kwargs["env"]
        self.assertIsNotNone(inherited)
        self.assertEqual(inherited, dict(os.environ))

        override = run.call_args_list[1].kwargs["env"]
        self.assertEqual(override["OBSERVER_PROBE"], "1")
        self.assertEqual(override["PATH"], os.environ["PATH"])

    def test_the_real_runner_preserves_a_nonzero_exit(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            native.run_tool([sys.executable, "-c", "raise SystemExit(9)"])
        self.assertEqual(raised.exception.returncode, 9)


class CommandLineTests(unittest.TestCase):
    def _invoke(self, argv, **options) -> tuple[int, str, str]:
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = native.main(argv, **options)
        return code, out.getvalue(), err.getvalue()

    def test_build_command_prints_the_returned_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            tools = _tools(repository)
            runner = RecordingRunner()

            code, out, _ = self._invoke(
                ["build", "-Repository", temporary, "-Arch", "all", "-Config", "Release", "-Jobs", "2"],
                tools=tools, runner=runner,
            )

            self.assertEqual(code, 0)
            printed = [Path(line) for line in out.splitlines()]
            self.assertEqual(
                printed,
                [
                    artifact
                    for architecture in native.ARCHITECTURES
                    for artifact in native.artifacts(repository, architecture, "Release")
                ],
            )

    def test_restore_command_runs_only_the_restore_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            style = _tools(Path(temporary))
            runner = RecordingRunner()

            code, out, _ = self._invoke(
                ["restore", "-Repository", temporary, "-Arch", "x64"],
                tools=style, runner=runner,
            )

            self.assertEqual(code, 0)
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(out.strip(), str(native.restore_root(Path(temporary), "x64")))

    def test_test_command_runs_the_suite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            tools = _tools(repository)
            runner = RecordingRunner()

            code, out, _ = self._invoke(
                ["test", "-Repository", temporary, "-Arch", "x86,x64", "-Config", "Debug"],
                tools=tools, runner=runner,
            )

            self.assertEqual(code, 0)
            self.assertEqual(len(out.splitlines()), 2)
            self.assertTrue(runner.calls[-1].argv[0].endswith("tests.exe"))

    def test_usage_errors_and_tool_failures_become_nonzero_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            tools = _tools(repository)

            code, _, err = self._invoke(
                ["build", "-Repository", temporary, "-Config", "Release"],
                tools=tools, runner=RecordingRunner(),
            )
            self.assertEqual(code, 0)  # x64 Debug is the default selection

            code, _, err = self._invoke(
                ["test", "-Repository", temporary, "-Arch", "x64"],
                environ={}, tools=None, runner=None,
            )
            self.assertEqual(code, 2)
            self.assertIn("observer-native", err)

            failure = subprocess.CalledProcessError(3, "vcpkg.exe")
            code, _, err = self._invoke(
                ["restore", "-Repository", temporary, "-Arch", "x64"],
                tools=tools, runner=RecordingRunner(failure),
            )
            self.assertEqual(code, 3)
            self.assertIn("exit code 3", err)

    def test_an_invalid_selection_is_a_usage_error(self) -> None:
        with self.assertRaises(SystemExit):
            self._invoke(["build", "-Arch", "sparc"])


class ProjectContractTests(unittest.TestCase):
    def test_observer_proj_builds_every_project_through_one_msbuild_submission(self) -> None:
        project = ET.parse(REPOSITORY_ROOT / "build" / "Observer.proj").getroot()

        included = [
            Path(item.get("Include")).name
            for item in project.findall(f".//{MSBUILD}ObserverProject")
        ]
        self.assertEqual(
            included,
            ["renpy.vcxproj", "rpgmaker.vcxproj", "zanzarah.vcxproj", "tests.vcxproj"],
        )

        build = project.find(f".//{MSBUILD}Target[@Name='Build']/{MSBUILD}MSBuild")
        self.assertIsNotNone(build)
        self.assertEqual(build.get("BuildInParallel"), "$(BuildInParallel)")

        artifacts = project.find(f".//{MSBUILD}ArtifactsRoot")
        self.assertIsNotNone(artifacts)
        self.assertIn("out", artifacts.text)
        self.assertIn("native", artifacts.text)

    def test_tests_project_references_the_modules_without_linking_them(self) -> None:
        project = ET.parse(REPOSITORY_ROOT / "build" / "projects" / "tests.vcxproj").getroot()
        references = {
            Path(item.get("Include")).name: item
            for item in project.findall(f".//{MSBUILD}ProjectReference")
        }

        self.assertEqual(
            set(references),
            {"renpy.vcxproj", "rpgmaker.vcxproj", "zanzarah.vcxproj"},
        )
        for reference in references.values():
            self.assertEqual(
                reference.find(f"{MSBUILD}LinkLibraryDependencies").text, "false"
            )
            self.assertEqual(
                reference.find(f"{MSBUILD}ReferenceOutputAssembly").text, "false"
            )


if __name__ == "__main__":
    unittest.main()

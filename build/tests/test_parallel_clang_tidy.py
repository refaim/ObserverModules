"""MSBuild compile metadata must drive every pinned parallel clang-tidy invocation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402


class ParallelClangTidyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {"UNPACKRAT_CLANG_TIDY_CACHE": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def test_all_metadata_groups_and_files_keep_exact_vendor_options(self) -> None:
        first = str(self.root / "first.cpp")
        second = str(self.root / "second.cpp")
        third = str(self.root / "third.cpp")
        compiler = str(self.root / "CL.exe")
        options = '/nologo /I"C:\\foreign headers" /DVALUE=1 /c'
        metadata = json.dumps({
            "Properties": {"IncludePath": "C:\\sdk;C:\\vcpkg"},
            "TargetResults": {"GetCompileCommands": {"Items": [
                {"ToolPath": compiler, "WorkingDirectory": str(self.root),
                 "Identity": options, "Files": f"{first};{second}"},
                {"ToolPath": compiler, "WorkingDirectory": str(self.root),
                 "Identity": "/std:c++23 /c", "Files": third},
            ]}},
        })
        directory = self.root / "fresh database"
        self.assertEqual(main._tidy_database(metadata, directory), "C:\\sdk;C:\\vcpkg")
        database = json.loads((directory / "compile_commands.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["file"] for entry in database], [first, second, third])
        self.assertEqual([entry["directory"] for entry in database], [str(self.root)] * 3)
        self.assertEqual(database[0]["command"],
                         subprocess.list2cmdline([compiler]) + " " + options + " "
                         + subprocess.list2cmdline([first]))
        self.assertEqual(database[1]["command"],
                         subprocess.list2cmdline([compiler]) + " " + options + " "
                         + subprocess.list2cmdline([second]))

    def test_incomplete_metadata_fails_before_a_database_can_be_written(self) -> None:
        valid = {"Properties": {"IncludePath": "C:\\sdk"},
                 "TargetResults": {"GetCompileCommands": {"Items": [{
                     "ToolPath": str(self.root / "CL.exe"),
                     "WorkingDirectory": str(self.root), "Identity": "/c",
                     "Files": str(self.root / "unit.cpp"),
                 }]}}}
        bad = [
            {"Properties": valid["Properties"], "TargetResults": {}},
            {"Properties": {"IncludePath": ""}, "TargetResults": valid["TargetResults"]},
            {"Properties": valid["Properties"],
             "TargetResults": {"GetCompileCommands": {"Items": []}}},
            {"Properties": valid["Properties"],
             "TargetResults": {"GetCompileCommands": {"Items": [
                 {**valid["TargetResults"]["GetCompileCommands"]["Items"][0], "Identity": ""}]}}},
            {"Properties": valid["Properties"],
             "TargetResults": {"GetCompileCommands": {"Items": [
                 {**valid["TargetResults"]["GetCompileCommands"]["Items"][0],
                  "Files": str(self.root / "unit.cpp") + ";"}]}}},
            {"Properties": valid["Properties"],
             "TargetResults": {"GetCompileCommands": {"Items": [
                 {**valid["TargetResults"]["GetCompileCommands"]["Items"][0],
                  "WorkingDirectory": "relative"}]}}},
        ]
        for index, metadata in enumerate(bad):
            directory = self.root / f"invalid-{index}"
            with self.subTest(index=index), self.assertRaises((KeyError, main.BuildError)):
                main._tidy_database(json.dumps(metadata), directory)
            self.assertFalse((directory / "compile_commands.json").exists())

    def test_explicit_cache_uses_real_clang_cl_and_runner_bridge(self) -> None:
        tidy = self.root / "clang-tidy.exe"
        clang_cl = self.root / "clang-cl.exe"
        clang_cl.write_text("pinned compiler", encoding="utf-8")
        (self.root / "run-clang-tidy").write_text("pinned runner", encoding="utf-8")
        bridge = self.root / "clang-tidy-cache.cmd"
        bridge.write_text("@echo off\r\n", encoding="utf-8")
        project = self.root / "module.vcxproj"
        metadata = json.dumps({
            "Properties": {"IncludePath": "C:\\sdk"},
            "TargetResults": {"GetCompileCommands": {"Items": [{
                "ToolPath": "C:\\Windows\\System32\\CL.exe", "WorkingDirectory": str(self.root),
                "Identity": "/std:c++23 /c", "Files": str(self.root / "src.cpp"),
            }]}},
        })
        runner = mock.Mock(side_effect=[subprocess.CompletedProcess(["msbuild"], 0, metadata),
                                        subprocess.CompletedProcess(["tidy"], 0)])
        with (mock.patch.dict(os.environ, {"UNPACKRAT_CLANG_TIDY_CACHE": str(bridge)}),
              mock.patch.object(main, "_restore"),
              mock.patch.object(main, "_clang_tidy", return_value=tidy),
              mock.patch.object(main, "_analysis_projects", return_value=(project,)),
              mock.patch.object(main, "_analysis_command", return_value=("msbuild",)),
              mock.patch.object(main, "run", runner)):
            main._analyze(self.root, "x64", self.root / "msbuild.exe",
                          self.root / "vcpkg.exe", 4, tidy=True)
        argv = runner.call_args_list[1].args[0]
        self.assertEqual(argv[argv.index("-clang-tidy-binary") + 1], str(bridge))
        database = Path(argv[argv.index("-p") + 1]) / "compile_commands.json"
        entry = json.loads(database.read_text(encoding="utf-8"))[0]
        self.assertTrue(entry["command"].startswith(subprocess.list2cmdline([str(clang_cl)])))
        self.assertIn("/std:c++23 /c", entry["command"])
        self.assertIn("/clang:-frewrite-includes", entry["command"])
        self.assertTrue((main.artifacts_root(self.root) / "analysis" / "clang-tidy-x64.complete").is_file())

    def test_explicit_cache_refuses_missing_real_compiler_before_metadata(self) -> None:
        tidy = self.root / "clang-tidy.exe"
        (self.root / "run-clang-tidy").write_text("pinned runner", encoding="utf-8")
        bridge = self.root / "clang-tidy-cache.cmd"
        bridge.write_text("@echo off\r\n", encoding="utf-8")
        with (mock.patch.dict(os.environ, {"UNPACKRAT_CLANG_TIDY_CACHE": str(bridge)}),
              mock.patch.object(main, "_restore"),
              mock.patch.object(main, "_clang_tidy", return_value=tidy),
              mock.patch.object(main, "run") as runner):
            with self.assertRaisesRegex(FileNotFoundError, "clang-cl"):
                main._analyze(self.root, "x64", self.root / "msbuild.exe",
                              self.root / "vcpkg.exe", 4, tidy=True)
        runner.assert_not_called()

    def test_explicit_cache_refuses_missing_bridge_before_metadata(self) -> None:
        tidy = self.root / "clang-tidy.exe"
        (self.root / "run-clang-tidy").write_text("pinned runner", encoding="utf-8")
        (self.root / "clang-cl.exe").write_text("pinned compiler", encoding="utf-8")
        missing = self.root / "missing-cache-bridge.cmd"
        with (mock.patch.dict(os.environ, {"UNPACKRAT_CLANG_TIDY_CACHE": str(missing)}),
              mock.patch.object(main, "_restore"),
              mock.patch.object(main, "_clang_tidy", return_value=tidy),
              mock.patch.object(main, "run") as runner):
            with self.assertRaisesRegex(FileNotFoundError, "cache bridge"):
                main._analyze(self.root, "x64", self.root / "msbuild.exe",
                              self.root / "vcpkg.exe", 4, tidy=True)
        runner.assert_not_called()

    def test_runner_launch_preserves_native_newlines_arguments_and_exit_status(self) -> None:
        directory = self.root / "native ü quoted path"
        directory.mkdir()
        tidy = directory / "clang-tidy.exe"
        script = directory / "run-clang-tidy"
        script.write_text("# installed runner", encoding="utf-8")
        recorded = self.root / "quoted ü arguments.json"
        metadata = json.dumps({
            "Properties": {"IncludePath": "sdk"},
            "TargetResults": {"GetCompileCommands": {"Items": [{
                "ToolPath": str(self.root / "CL.exe"), "WorkingDirectory": str(self.root),
                "Identity": "/c", "Files": str(self.root / "source.cpp"),
            }]}},
        })
        runner = mock.Mock(side_effect=[subprocess.CompletedProcess(["msbuild"], 0, metadata),
                                        subprocess.CompletedProcess(["tidy"], 0)])
        with (mock.patch.object(main, "_restore"),
              mock.patch.object(main, "_clang_tidy", return_value=tidy),
              mock.patch.object(main, "_analysis_projects", return_value=(self.root / "module.vcxproj",)),
              mock.patch.object(main, "_analysis_command", return_value=("msbuild",)),
              mock.patch.object(main, "run", runner)):
            main._analyze(self.root, "x64", self.root / "msbuild.exe",
                          self.root / "vcpkg.exe", 2, tidy=True)
        argv = runner.call_args_list[1].args[0]
        for code in (0, 7):
            with self.subTest(code=code):
                script.write_text(
                    "import json, pathlib, sys\n"
                    f"pathlib.Path({str(recorded)!r}).write_text(json.dumps(sys.argv), encoding='utf-8')\n"
                    "print('fixture.cpp:1:1: error: diagnostic [test-check]\\r\\nint x;\\r\\n^\\r\\n', end='')\n"
                    "sys.stderr.write('diagnostic context\\r\\n')\n"
                    f"raise SystemExit({code})\n", encoding="utf-8")
                result = subprocess.run(argv, capture_output=True, check=False)
                self.assertEqual(result.returncode, code)
                self.assertEqual(result.stdout,
                                 b"fixture.cpp:1:1: error: diagnostic [test-check]\r\nint x;\r\n^\r\n")
                self.assertEqual(result.stderr, b"diagnostic context\r\n")
                expected = [str(script), "-p", str(main.artifacts_root(self.root) / "obj" / "x64"
                            / "Release" / "module" / "module.ClangTidy"), "-j", "2",
                            "-clang-tidy-binary", str(tidy), "-warnings-as-errors=*"]
                self.assertEqual(json.loads(recorded.read_text(encoding="utf-8")), expected)

    def test_missing_parallel_script_fails_before_any_project_runs(self) -> None:
        tidy = self.root / "clang-tidy.exe"
        with (mock.patch.object(main, "_restore"),
              mock.patch.object(main, "_clang_tidy", return_value=tidy),
              mock.patch.object(main, "run") as run):
            with self.assertRaises(FileNotFoundError):
                main._analyze(self.root, "x64", self.root / "msbuild.exe",
                              self.root / "vcpkg.exe", 4, tidy=True)
        run.assert_not_called()

    def test_runner_gets_fresh_database_include_jobs_and_checked_failure(self) -> None:
        tidy = self.root / "clang-tidy.exe"
        script = self.root / "run-clang-tidy"
        script.write_text("# installed LLVM script", encoding="utf-8")
        project = self.root / "build" / "projects" / "renpy.vcxproj"
        source = str(self.root / "src" / "renpy.cpp")
        metadata = json.dumps({
            "Properties": {"IncludePath": "C:\\sdk;C:\\vcpkg"},
            "TargetResults": {"GetCompileCommands": {"Items": [{
                "ToolPath": str(self.root / "CL.exe"),
                "WorkingDirectory": str(project.parent),
                "Identity": "/nologo /c", "Files": source,
            }]}}
        })
        runner = mock.Mock(side_effect=[
            subprocess.CompletedProcess(["msbuild"], 0, metadata),
            subprocess.CalledProcessError(7, [sys.executable, str(script)]),
        ])
        with (mock.patch.object(main, "_restore"),
              mock.patch.object(main, "_clang_tidy", return_value=tidy),
              mock.patch.object(main, "_analysis_projects", return_value=(project,)),
              mock.patch.object(main, "_analysis_command", return_value=("msbuild",)),
              mock.patch.object(main, "run", runner)):
            with self.assertRaises(subprocess.CalledProcessError):
                main._analyze(self.root, "x64", self.root / "msbuild.exe",
                              self.root / "vcpkg.exe", 4, tidy=True)
        self.assertEqual(runner.call_count, 2)
        self.assertTrue(runner.call_args_list[0].kwargs["capture"])
        args = runner.call_args_list[1]
        argv = args.args[0]
        self.assertEqual(argv[:4], (sys.executable, "-c", main._CLANG_TIDY_RUNNER_BOOTSTRAP, str(script)))
        self.assertEqual(argv[4:6], ("-p", str(main.artifacts_root(self.root) / "obj" / "x64"
                                                / "Release" / "renpy" / "renpy.ClangTidy")))
        self.assertEqual(argv[6:], ("-j", "4", "-clang-tidy-binary", str(tidy),
                                    "-warnings-as-errors=*"))
        self.assertEqual(args.kwargs["env"], {"INCLUDE": "C:\\sdk;C:\\vcpkg"})
        database = json.loads((Path(argv[argv.index("-p") + 1]) / "compile_commands.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["file"] for entry in database], [source])

    def test_runner_failure_still_scans_later_projects_before_marking_complete(self) -> None:
        tidy = self.root / "clang-tidy.exe"
        (self.root / "run-clang-tidy").write_text("fixture", encoding="utf-8")
        projects = (self.root / "first.vcxproj", self.root / "second.vcxproj",
                    self.root / "third.vcxproj")
        metadata = json.dumps({
            "Properties": {"IncludePath": "C:\\sdk"},
            "TargetResults": {"GetCompileCommands": {"Items": [{
                "ToolPath": str(self.root / "CL.exe"), "WorkingDirectory": str(self.root),
                "Identity": "/c", "Files": str(self.root / "source.cpp"),
            }]}},
        })
        failure = subprocess.CalledProcessError(7, [sys.executable, "run-clang-tidy"])
        runner = mock.Mock(side_effect=[
            subprocess.CompletedProcess(["msbuild"], 0, metadata), failure,
            subprocess.CompletedProcess(["msbuild"], 0, metadata),
            subprocess.CompletedProcess(["run-clang-tidy"], 0),
            subprocess.CompletedProcess(["msbuild"], 0, metadata),
            subprocess.CalledProcessError(9, [sys.executable, "run-clang-tidy"]),
        ])
        marker = main.artifacts_root(self.root) / "analysis" / "clang-tidy-x64.complete"
        with (mock.patch.object(main, "_restore"),
              mock.patch.object(main, "_clang_tidy", return_value=tidy),
              mock.patch.object(main, "_analysis_projects", return_value=projects),
              mock.patch.object(main, "_analysis_command", return_value=("msbuild",)),
              mock.patch.object(main, "run", runner)):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                main._analyze(self.root, "x64", self.root / "msbuild.exe",
                              self.root / "vcpkg.exe", 4, tidy=True)
        self.assertIs(caught.exception, failure)
        self.assertEqual(runner.call_count, 6)
        self.assertTrue(marker.is_file())

    def test_metadata_failure_clears_stale_completion_marker(self) -> None:
        tidy = self.root / "clang-tidy.exe"
        (self.root / "run-clang-tidy").write_text("fixture", encoding="utf-8")
        marker = main.artifacts_root(self.root) / "analysis" / "clang-tidy-x64.complete"
        marker.parent.mkdir(parents=True)
        marker.write_text("stale", encoding="utf-8")
        runner = mock.Mock(return_value=subprocess.CompletedProcess(["msbuild"], 0, "not JSON"))
        with (mock.patch.object(main, "_restore"),
              mock.patch.object(main, "_clang_tidy", return_value=tidy),
              mock.patch.object(main, "_analysis_projects", return_value=(self.root / "first.vcxproj",)),
              mock.patch.object(main, "_analysis_command", return_value=("msbuild",)),
              mock.patch.object(main, "run", runner)):
            with self.assertRaises(json.JSONDecodeError):
                main._analyze(self.root, "x64", self.root / "msbuild.exe",
                              self.root / "vcpkg.exe", 4, tidy=True)
        runner.assert_called_once()
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()

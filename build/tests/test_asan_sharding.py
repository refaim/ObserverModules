"""The x86 ASan suite runs every Catch2 shard in a fresh checked process."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402


class AsanShardingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.runtime = self.root / "asan.dll"
        self.runtime.write_bytes(b"asan")
        self.msbuild = self.root / "MSBuild.exe"
        self.vcpkg = self.root / "vcpkg.exe"
        self.llvm = main.Llvm(
            self.root / "llvm", self.root / "llvm/runtime",
            self.root / "llvm-cov.exe", self.root / "llvm-profdata.exe",
        )

    def _run(self, sanitizer: str, architecture: str, run: mock.Mock) -> Path:
        with (
            mock.patch.object(main, "_restore"),
            mock.patch.object(main, "_msbuild_command", return_value=(str(self.msbuild),)),
            mock.patch.object(main, "_asan_runtime", return_value=self.runtime),
            mock.patch.object(main, "run", run),
        ):
            return main._sanitizer(self.repo, sanitizer, architecture, self.msbuild, self.vcpkg, self.llvm)

    @staticmethod
    def _tests(run: mock.Mock):
        return [call for call in run.call_args_list if Path(call.args[0][0]).name == "tests.exe"]

    def test_x86_asan_checks_all_four_shards_with_identical_options_and_unique_reports(self) -> None:
        run = mock.Mock()
        result = self._run("asan", "x86", run)
        tests = self._tests(run)
        self.assertEqual(len(tests), 4)
        reports: list[Path] = []
        for index, call in enumerate(tests):
            command = call.args[0]
            self.assertEqual(command[command.index("--shard-count") + 1], "4")
            self.assertEqual(command[command.index("--shard-index") + 1], str(index))
            self.assertEqual(command[command.index("--rng-seed") + 1], "1")
            self.assertEqual(command[command.index("--order") + 1], "lex")
            self.assertEqual(call.kwargs["env"], {"ASAN_OPTIONS": main._ASAN_OPTIONS})
            report_argument = next(part for part in command if part.startswith("JUnit::out="))
            reports.append(Path(report_argument.removeprefix("JUnit::out=")))
        self.assertEqual(len(set(reports)), 4)
        self.assertTrue(all(report.parent.is_dir() for report in reports))
        self.assertEqual(result, reports[0])

    def test_x64_asan_and_ubsan_keep_one_unsharded_process(self) -> None:
        cases = (("asan", "x64", "ASAN_OPTIONS"), ("ubsan", "x64", "UBSAN_OPTIONS"))
        for sanitizer, architecture, env_name in cases:
            with self.subTest(sanitizer=sanitizer):
                run = mock.Mock()
                report = self._run(sanitizer, architecture, run)
                tests = self._tests(run)
                self.assertEqual(len(tests), 1)
                self.assertNotIn("--shard-count", tests[0].args[0])
                self.assertNotIn("--shard-index", tests[0].args[0])
                self.assertNotIn("--rng-seed", tests[0].args[0])
                self.assertIn(env_name, tests[0].kwargs["env"])
                self.assertEqual(report.name, "tests.xml")

    def test_x86_asan_stops_at_the_first_failed_shard(self) -> None:
        calls: list[tuple[str, ...]] = []

        def checked_run(command: tuple[str, ...], **_kwargs: object) -> None:
            calls.append(command)
            if "--shard-index" in command and command[command.index("--shard-index") + 1] == "1":
                raise subprocess.CalledProcessError(1, command)

        with self.assertRaises(subprocess.CalledProcessError):
            self._run("asan", "x86", mock.Mock(side_effect=checked_run))
        tests = [command for command in calls if Path(command[0]).name == "tests.exe"]
        self.assertEqual(len(tests), 2)
        self.assertEqual([command[command.index("--shard-index") + 1] for command in tests], ["0", "1"])


if __name__ == "__main__":
    unittest.main()

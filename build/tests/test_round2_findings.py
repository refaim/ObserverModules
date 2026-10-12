"""Focused regressions for the round-2 review findings (2026-10-13).

Each case pins one observable behaviour the minimal driver, leak worker, workflow or
documentation must have after this review round:

- finding 1: ``ci_arch`` forwards its nondefault ``-Jobs`` into both the Debug and the
  Release ``test`` calls it makes, not only into the later stages;
- finding 2: the leak worker tolerates snapshot/diff directories that already exist
  instead of failing on a repeated run into the same output root;
- finding 3: a stalled GFlags query and a contended GFlags file lock both fail closed
  under a bounded wait instead of hanging CI forever;
- finding 4: the two ``__main__`` guards are exercised through the real entry behaviour
  rather than hidden behind a coverage exclusion;
- finding 7: the BinSkim gate exempts only the documented BA2027 warning and honours a
  rule's ``defaultConfiguration`` level when a result omits its own level.

Everything here runs in-process; the package and leak entry tests use ``runpy`` so the
guard statements themselves are measured, and every native tool stays faked.
"""

from __future__ import annotations

from pathlib import Path
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from filelock import Timeout as FileLockTimeout  # noqa: E402

import leaks  # noqa: E402
import main  # noqa: E402
from leaks import LeakError  # noqa: E402


class _CleanProbe:
    """A probe whose stdout carries a full, passing marker stream."""

    def __init__(self, *, windows: int = 3) -> None:
        labels = ("baseline", *(f"window-{index}" for index in range(1, windows + 1)))
        self.stdout = _FakeStream(
            "\n".join(
                [
                    "UNPACKRAT_LEAK_PROBE|READY|pid=77|mode=operations|configuration=Release|scenarios=malformed",
                    *(f"UNPACKRAT_LEAK_PROBE|SNAPSHOT|{label}|pid=77|" for label in labels),
                    "UNPACKRAT_LEAK_PROBE|DONE|pid=77|",
                ]
            )
            + "\n"
        )
        self.stdin = _FakeStream()
        self.pid = 77
        self.returncode: int | None = None
        self.killed = False

    def wait(self, timeout: int = 0) -> int:
        self.returncode = 0
        return 0

    def poll(self) -> int | None:
        return self.returncode

    def children(self, *, recursive: bool) -> list[object]:
        return []

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _FakeStream:
    def __init__(self, text: str = "") -> None:
        self._stream = __import__("io").StringIO(text)
        self.was_closed = False

    def readline(self) -> str:
        return self._stream.readline()

    def read(self) -> str:
        return self._stream.read()

    def write(self, text: str) -> int:
        return self._stream.write(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.was_closed = True


def _diff_runner(argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
    if Path(argv[0]).name.lower() == "gflags.exe":
        settings = (
            "Current Registry Settings for leak-probe.exe executable are: 00000000"
            if len(argv) == 3
            else ""
        )
        return subprocess.CompletedProcess(list(argv), 0, settings)
    destination = Path(argv[-1].removeprefix("-f:"))
    destination.write_text("Total increase == 0\n" if "-d" in argv else "BackTrace\n", encoding="utf-8")
    return subprocess.CompletedProcess(list(argv), 0, "")


class CiArchJobsTests(unittest.TestCase):
    """Finding 1: the whole ci_arch pipeline shares one -Jobs value."""

    def test_ci_arch_forwards_jobs_into_both_test_calls(self) -> None:
        captured: list[tuple[str, str, int | None]] = []

        def fake_test(
            architecture: str, configuration: str, *,
            repository: object, corpus: object = None, jobs: int | None = None,
        ) -> tuple[()]:
            captured.append((architecture, configuration, jobs))
            return ()

        with mock.patch.multiple(
            main,
            test=fake_test,
            _native_tools=mock.Mock(return_value=(Path("MSBuild.exe"), Path("vcpkg.exe"))),
            _llvm=mock.Mock(return_value=mock.Mock()),
            _coverage=mock.Mock(),
            _sanitizer=mock.Mock(),
            _fuzz=mock.Mock(),
            _leaks=mock.Mock(),
            _audit=mock.Mock(),
            _binskim=mock.Mock(),
        ):
            main.ci_arch(["x64"], repository=Path("."), jobs=7)

        self.assertEqual(captured, [("x64", "Debug", 7), ("x64", "Release", 7)])


class LeakStableDirectoryTests(unittest.TestCase):
    """Finding 2: a repeated measure run into the same output root must not fail on mkdir."""

    def test_measure_reuses_existing_snapshot_and_diff_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "leak-probe.exe").touch()
            (root / "umdh.exe").touch()
            (root / "gflags.exe").touch()
            output = root / "evidence"
            (output / "snapshots").mkdir(parents=True)
            (output / "diffs").mkdir()
            with mock.patch.dict(os.environ, {"UNPACKRAT_OUT_DIR": str(output)}), mock.patch(
                "leaks.psutil.Popen", return_value=_CleanProbe()
            ), mock.patch("leaks.subprocess.run", side_effect=_diff_runner):
                leaks.main(("measure", str(root), str(root / "umdh.exe"), "operations", "malformed", "1", "2", "3", "0"))
            self.assertTrue((output / "diffs" / "overall.txt").is_file())
            self.assertTrue((output / "snapshots" / "window-3.txt").is_file())


class LeakGflagsBoundTests(unittest.TestCase):
    """Finding 3: the GFlags tool and the GFlags lock both have a finite bound."""

    def _environment(self, root: Path):
        for name in ("leak-probe.exe", "umdh.exe", "gflags.exe"):
            (root / name).touch()
        output = root / "evidence"
        output.mkdir(exist_ok=True)
        return mock.patch.dict(os.environ, {"UNPACKRAT_OUT_DIR": str(output)})

    def test_run_gflags_forwards_a_finite_timeout(self) -> None:
        with mock.patch("leaks.subprocess.run") as invoked:
            invoked.return_value = subprocess.CompletedProcess(["gflags"], 0, "")
            leaks._run_gflags(Path("gflags.exe"), "leak-probe.exe", "+ust")
        timeout = invoked.call_args.kwargs.get("timeout")
        self.assertIsInstance(timeout, (int, float))
        self.assertGreater(timeout, 0)

    def test_stalled_gflags_query_fails_closed_without_launching_the_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def runner(argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
                self.assertGreater(options.get("timeout") or 0, 0)
                raise subprocess.TimeoutExpired(list(argv), 1)

            with self._environment(root), mock.patch(
                "leaks.subprocess.run", side_effect=runner
            ), mock.patch("leaks.psutil.Popen") as popen, self.assertRaisesRegex(LeakError, "GFlags timed out"):
                leaks.main(("measure", str(root), str(root / "umdh.exe"), "operations", "malformed", "1", "2", "3", "0"))
            popen.assert_not_called()

    def test_contended_gflags_lock_fails_closed_with_a_bounded_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contention = mock.MagicMock()
            contention.__enter__ = mock.Mock(side_effect=FileLockTimeout("busy"))
            with self._environment(root), mock.patch(
                "leaks.FileLock", return_value=contention
            ) as lock_class, mock.patch("leaks.psutil.Popen") as popen, self.assertRaisesRegex(
                LeakError, "GFlags lock"
            ):
                leaks.main(("measure", str(root), str(root / "umdh.exe"), "operations", "malformed", "1", "2", "3", "0"))
            popen.assert_not_called()
            timeout = lock_class.call_args.kwargs.get("timeout")
            self.assertIsInstance(timeout, (int, float))
            self.assertGreater(timeout, 0)


def _build_package_repository(repo: Path) -> None:
    module = repo / "src" / "modules" / "renpy"
    module.mkdir(parents=True)
    (module / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (module / "observer_user.ini").write_bytes(b"[Modules]\n")
    (module / "ChangeLog.en.txt").write_bytes(b"Version 1.2.3\n-\n+ note\n")
    (module / "ChangeLog.ru.txt").write_bytes("\u0412\u0435\u0440\u0441\u0438\u044f 1.2.3\n-\n+ note\n".encode("utf-8"))
    (repo / "LICENSE.txt").write_bytes(b"MIT license text\n")
    licenses = repo / "licenses"
    licenses.mkdir()
    for name in ("Observer.txt", "GPL-3.0.txt", "rpatool.txt", "serde-pickle.txt", "zlib.txt"):
        (licenses / name).write_bytes(name.encode() + b" notice")
    for architecture in main.ARCHITECTURES:
        stage = repo / "out" / "native" / "bin" / architecture / "Release"
        stage.mkdir(parents=True)
        (stage / "renpy.so").write_bytes(b"binary")
        (stage / "renpy.pdb").write_bytes(b"symbols")


class ModuleEntryPointTests(unittest.TestCase):
    """Finding 4: the __main__ guards run for real instead of being excluded."""

    def test_main_entry_point_packages_the_selected_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            _build_package_repository(repo)
            output = root / "out"
            argv = [
                "main.py", "package",
                "-Repository", str(repo), "-Output", str(output), "-Module", "renpy",
            ]
            with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(BUILD_ROOT / "main.py"), run_name="__main__")
            self.assertEqual(raised.exception.code, 0)
            self.assertEqual(
                sorted(path.name for path in output.glob("*.zip")),
                ["renpy-1.2.3-bin-x64.zip", "renpy-1.2.3-bin-x86.zip", "renpy-1.2.3-pdb.zip"],
            )

    def test_leaks_entry_point_stages_the_probe_binaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "evidence"
            output.mkdir()
            sources = []
            for index, name in enumerate(leaks.BINARIES):
                directory = root / str(index)
                directory.mkdir()
                source = directory / name
                source.write_bytes(name.encode())
                sources.append(source)
            argv = ["leaks.py", "setup", *(str(path) for path in sources)]
            with mock.patch.dict(os.environ, {"UNPACKRAT_OUT_DIR": str(output)}), mock.patch.object(
                sys, "argv", argv
            ), self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(BUILD_ROOT / "leaks.py"), run_name="__main__")
            self.assertEqual(raised.exception.code, 0)
            self.assertTrue(all((output / name).is_file() for name in leaks.BINARIES))


class BinskimPolicyTests(unittest.TestCase):
    """Finding 7: only the BA2027 warning is exempt and omitted levels follow the rule default."""

    @staticmethod
    def _rule_default(rule: str, level: str) -> dict[str, object]:
        return {
            "tool": {"driver": {"rules": [{"id": rule, "defaultConfiguration": {"level": level}}]}},
            "results": [{"ruleId": rule}],
        }

    def test_explicit_ba2027_error_is_not_exempt(self) -> None:
        with self.assertRaises(main.BuildError):
            main.require_clean_binskim({"runs": [{"results": [{"ruleId": "BA2027", "level": "error"}]}]})

    def test_ba2027_warning_is_exempt(self) -> None:
        main.require_clean_binskim({"runs": [{"results": [{"ruleId": "BA2027", "level": "warning"}]}]})
        main.require_clean_binskim({"runs": [self._rule_default("BA2027", "warning")]})

    def test_omitted_level_uses_the_rule_default_configuration(self) -> None:
        main.require_clean_binskim({"runs": [self._rule_default("BA7000", "note")]})
        with self.assertRaises(main.BuildError):
            main.require_clean_binskim({"runs": [self._rule_default("BA7001", "error")]})

    def test_documents_without_runs_results_or_rules_are_clean(self) -> None:
        main.require_clean_binskim({})
        main.require_clean_binskim({"runs": [{}]})
        main.require_clean_binskim({"runs": [{"tool": {}, "results": [{"ruleId": "BA2001", "level": "note"}]}]})


if __name__ == "__main__":
    unittest.main()

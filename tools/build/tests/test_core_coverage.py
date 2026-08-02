from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.binary_audit import AuditError, require_release_pe  # noqa: E402
from core.execute import ExecutionError, Executor  # noqa: E402
from core.graph import Command, Graph, GraphError, Node  # noqa: E402
from core.node import NodeFactory  # noqa: E402
from core.paths import BuildPaths, PathSafetyError  # noqa: E402
from core.render import TemplateRenderer  # noqa: E402
from core.sarif import (  # noqa: E402
    SarifError,
    normalize_msvc,
    require_clean,
)
from core.store import CasStateError, CasStore  # noqa: E402
from core.toolchain import _existing, _output  # noqa: E402
from core.windows_process import WindowsProcessRunner, _reap  # noqa: E402


UID = "0123456789abcdef0123456789abcdef"
RUN_ID = "20260802T000000Z-coverage"


def node(
    name: str = "leaf", *, inputs: tuple[str, ...] = (), command: Command | None = None
) -> Node:
    return Node(
        name,
        hashlib.md5(name.encode(), usedforsecurity=False).hexdigest(),
        "cpu",
        command or Command(("tool",)),
        inputs,
    )


def write_sarif(path: Path, runs: list[object]) -> None:
    path.write_text(
        json.dumps({"version": "2.1.0", "runs": runs}), encoding="utf-8"
    )


class CoreValidationTests(unittest.TestCase):
    def test_release_audit_reports_an_unsupported_architecture(self) -> None:
        with self.assertRaisesRegex(AuditError, "unsupported.*riscv64") as raised:
            require_release_pe("riscv64", "", "", "")

        self.assertIsInstance(raised.exception.__cause__, KeyError)

    def test_command_and_node_reject_ambiguous_runtime_types(self) -> None:
        invalid_commands = (
            lambda: Command(()),
            lambda: Command((1,)),
            lambda: Command(("tool",), env=(("ONLY-KEY",),)),
            lambda: Command(("tool",), env=(("KEY", 1),)),
            lambda: Command(("tool",), env=(("KEY", "bad\0value"),)),
            lambda: Command(("tool",), env=((1, "value"),)),
        )
        for constructor in invalid_commands:
            with self.subTest(constructor=constructor), self.assertRaises(GraphError):
                constructor()  # type: ignore[call-arg]

        invalid_nodes = (
            lambda: Node(1, UID, "cpu", Command(("tool",))),
            lambda: Node("leaf", 1, "cpu", Command(("tool",))),
            lambda: Node("leaf", UID, 1, Command(("tool",))),
            lambda: Node("leaf", UID, "cpu", object()),
            lambda: Node("leaf", UID, "cpu", Command(("tool",)), (1,)),
        )
        for constructor in invalid_nodes:
            with self.subTest(constructor=constructor), self.assertRaises(GraphError):
                constructor()  # type: ignore[call-arg]

    def test_graph_rejects_non_integral_pool_capacity_and_sparse_cycle_error(
        self,
    ) -> None:
        with self.assertRaisesRegex(GraphError, "invalid pool capacity"):
            Graph((node(),), ("leaf",), {"cpu": "many"})  # type: ignore[dict-item]

        sorter = mock.Mock()
        sorter.prepare.side_effect = __import__("graphlib").CycleError("cycle")
        with (
            mock.patch("core.graph.TopologicalSorter", return_value=sorter),
            self.assertRaisesRegex(GraphError, r"dependency cycle:\s*$"),
        ):
            Graph((node(),), ("leaf",), {"cpu": 1})

    def test_node_factory_renders_signs_and_applies_runtime_overrides(self) -> None:
        factory = NodeFactory(
            TemplateRenderer(BUILD_ROOT / "templates"),
            Path(r"C:\repo\default-work"),
            {"compiler": "msvc-default"},
            (("DEFAULT", "environment"),),
        )
        dependency = node("restore")
        variables = {
            "pwsh": "pwsh.exe",
            "msbuild": r"C:\VS\MSBuild.exe",
            "project": r"C:\repo\renpy.vcxproj",
            "target": "Build",
            "configuration": "Release",
            "platform": "x64",
            "msbuild_args": [],
        }
        current = factory.make(
            "msbuild.ps1",
            "build-renpy-x64",
            "cpu",
            variables,
            files={"renpy.vcxproj": b"project bytes"},
            dependencies=(dependency,),
            config={"architecture": "x64"},
        )
        overridden = factory.make(
            "msbuild.ps1",
            "build-renpy-x64",
            "cpu",
            variables,
            files={"renpy.vcxproj": b"project bytes"},
            dependencies=(dependency,),
            config={"architecture": "x64"},
            identity={"compiler": "msvc-override"},
            environment=(("OVERRIDE", "environment"),),
            cwd=Path(r"C:\repo\override-work"),
        )

        self.assertEqual(current.inputs, ("restore",))
        self.assertEqual(current.command.env, (("DEFAULT", "environment"),))
        self.assertEqual(current.command.cwd, r"C:\repo\default-work")
        self.assertIn(b"C:\\VS\\MSBuild.exe", current.command.stdin)
        self.assertEqual(overridden.command.env, (("OVERRIDE", "environment"),))
        self.assertEqual(overridden.command.cwd, r"C:\repo\override-work")
        self.assertNotEqual(current.uid, overridden.uid)

    def test_path_policy_rejects_a_candidate_outside_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            repository.mkdir()
            paths = BuildPaths(repository)

            with self.assertRaisesRegex(PathSafetyError, "outside repository"):
                paths._reject_existing_reparse_points(repository.parent)

    def test_cas_cannot_publish_when_entry_exists_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            paths = BuildPaths(repository)
            paths.prepare()
            store = CasStore(paths, RUN_ID)
            current = node()
            entry = store.paths_for(current)
            entry.entry.mkdir()
            entry.log.touch()

            with self.assertRaisesRegex(CasStateError, "without output directory"):
                store.mark_complete(current)

            self.assertFalse(entry.touch.exists())

    def test_tool_discovery_explains_missing_paths_and_empty_tool_output(self) -> None:
        with self.assertRaisesRegex(FileNotFoundError, "required path not found: None"):
            _existing(None)

        completed = subprocess.CompletedProcess(["tool"], 0, stdout=" \r\n", stderr="")
        with (
            mock.patch("core.toolchain.subprocess.run", return_value=completed) as run,
            self.assertRaisesRegex(RuntimeError, "tool returned empty output: tool"),
        ):
            _output(["tool"])

        run.assert_called_once_with(
            ["tool"], check=True, capture_output=True, text=True
        )


class ExecutorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_rejects_reuse_after_a_successful_build(self) -> None:
        current = node()
        complete: set[str] = set()

        async def run(built: Node) -> None:
            self.assertEqual(built, current)

        executor = Executor(
            Graph((current,), (current.name,), {"cpu": 1}),
            is_complete=lambda candidate: candidate.uid in complete,
            runner=run,
            publish=lambda candidate: complete.add(candidate.uid),
        )
        await executor.run()

        with self.assertRaisesRegex(ExecutionError, "single-use"):
            await executor.run()


class SarifFailureTests(unittest.TestCase):
    def test_sarif_read_errors_retain_the_report_path_and_cause(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            malformed = root / "malformed.sarif"
            malformed.write_text("{", encoding="utf-8")
            missing = root / "missing.sarif"

            for path in (malformed, missing):
                with self.subTest(path=path), self.assertRaises(SarifError) as raised:
                    normalize_msvc(path, root / "output.sarif", "analysis/")
                self.assertIn(f"cannot read SARIF report {path}", str(raised.exception))
                self.assertIsNotNone(raised.exception.__cause__)

    def test_gate_rejects_a_non_list_or_non_object_results_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, results in (("mapping", {}), ("scalar", ["warning"])):
                report = root / f"{name}.sarif"
                write_sarif(report, [{"results": results}])
                with self.subTest(results=results), self.assertRaisesRegex(
                    SarifError, "results must be a list of objects"
                ):
                    require_clean((report,))

    def test_module_entry_point_exits_successfully_for_a_clean_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "clean.sarif"
            write_sarif(report, [{"results": []}])
            with (
                mock.patch.dict(os.environ, {"OBSERVER_OUT_DIR": str(root)}),
                mock.patch.object(sys, "argv", ["sarif.py", "gate", str(report)]),
                self.assertWarnsRegex(RuntimeWarning, "core.sarif"),
                self.assertRaises(SystemExit) as raised,
            ):
                runpy.run_module("core.sarif", run_name="__main__")

            self.assertEqual(raised.exception.code, 0)


class FakeProcess:
    def __init__(
        self,
        *,
        communicate_error: BaseException | None = None,
        leave_returncode_unset: bool = False,
        kill_error: BaseException | None = None,
    ) -> None:
        self._handle = 301
        self.returncode: int | None = None
        self.communicate_error = communicate_error
        self.leave_returncode_unset = leave_returncode_unset
        self.kill_error = kill_error
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.events: list[str] = []

    def resume(self) -> None:
        self.events.append("resume")

    def communicate(self, *, input: bytes) -> tuple[None, None]:
        self.events.append(f"communicate:{input!r}")
        self.started.set()
        self.release.wait(timeout=5)
        if self.communicate_error is not None:
            raise self.communicate_error
        if not self.leave_returncode_unset:
            self.returncode = 0
        return None, None

    def kill(self) -> None:
        self.events.append("kill")
        self.release.set()
        if self.kill_error is not None:
            raise self.kill_error

    def wait(self) -> int:
        self.events.append("wait")
        self.returncode = 1
        return self.returncode


class FakeJob:
    def __init__(
        self,
        process: FakeProcess,
        *,
        close_error: BaseException | None = None,
        terminate_error: BaseException | None = None,
    ) -> None:
        self.process = process
        self.close_error = close_error
        self.terminate_error = terminate_error

    def assign_process(self, process_handle: int) -> None:
        self.process.events.append(f"assign:{process_handle}")

    def close(self) -> None:
        self.process.events.append("close")
        self.process.release.set()
        if self.close_error is not None:
            raise self.close_error

    def terminate(self) -> None:
        self.process.events.append("terminate")
        self.process.release.set()
        if self.terminate_error is not None:
            raise self.terminate_error


class WindowsProcessCleanupTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def patched(process: FakeProcess, job: FakeJob):
        return (
            mock.patch("core.windows_process.psutil.Popen", return_value=process),
            mock.patch("core.windows_process.WindowsJob", return_value=job),
        )

    async def test_communication_failure_during_reaping_is_reported(self) -> None:
        process = FakeProcess(communicate_error=OSError("late pipe failure"))
        communication = asyncio.create_task(
            asyncio.to_thread(process.communicate, input=b"recipe")
        )
        primary = RuntimeError("runner failed")
        errors: list[BaseException] = []

        await _reap(process, communication, errors, primary)

        self.assertEqual([str(error) for error in errors], ["late pipe failure"])
        self.assertIn("wait", process.events)

    async def test_successful_communication_must_set_an_exit_code(self) -> None:
        process = FakeProcess(leave_returncode_unset=True)
        job = FakeJob(process)
        popen, windows_job = self.patched(process, job)
        with popen, windows_job, self.assertRaisesRegex(
            RuntimeError, "completed without an exit code"
        ):
            await WindowsProcessRunner().run(
                Command((sys.executable,), stdin=b"recipe"), log=io.BytesIO()
            )

        self.assertIn("wait", process.events)

    async def test_every_cleanup_failure_is_visible_after_process_success(self) -> None:
        process = FakeProcess(kill_error=OSError("root kill failed"))
        job = FakeJob(
            process,
            close_error=OSError("job close failed"),
            terminate_error=OSError("job terminate failed"),
        )
        popen, windows_job = self.patched(process, job)
        with popen, windows_job, self.assertRaisesRegex(
            OSError, "job close failed"
        ) as raised:
            await WindowsProcessRunner().run(
                Command((sys.executable,)), log=io.BytesIO()
            )

        self.assertEqual(process.events[-3:], ["close", "terminate", "kill"])
        notes = raised.exception.__notes__
        self.assertTrue(any("job terminate failed" in note for note in notes))
        self.assertTrue(any("root kill failed" in note for note in notes))


if __name__ == "__main__":
    unittest.main(verbosity=2)

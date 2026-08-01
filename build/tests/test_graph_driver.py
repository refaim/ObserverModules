from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BUILD_ROOT.parent
DRIVER_PATH = BUILD_ROOT / "graph_driver.py"
ARTIFACTS_ROOT = REPOSITORY_ROOT / ".artifacts"
ARTIFACTS_ROOT.mkdir(exist_ok=True)


def load_driver() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("observer_graph_driver", DRIVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load graph driver from {DRIVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


driver = load_driver()


def node(
    name: str,
    *,
    deps: tuple[str, ...] = (),
    run_after: tuple[str, ...] = (),
    resources: dict[str, int] | None = None,
    argv: tuple[str, ...] = ("synthetic",),
    inputs: tuple[str, ...] = (),
    writes: tuple[str, ...] = (),
    outputs: tuple[str, ...] = (),
    fingerprint: tuple[str, ...] = (),
    cacheable: bool = False,
) -> dict[str, object]:
    return {
        "name": name,
        "deps": list(deps),
        "run_after": list(run_after),
        "resources": resources or {"cpu": 1},
        "argv": list(argv),
        "inputs": list(inputs),
        "writes": list(writes),
        "outputs": list(outputs),
        "fingerprint": list(fingerprint),
        "cacheable": cacheable,
    }


class GraphDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            dir=ARTIFACTS_ROOT,
            prefix="graph-driver-tests-",
        )
        self.workspace = Path(self.temporary_directory.name)
        self.logs = self.workspace / "logs"
        self.state = self.workspace / "state"

    def tearDown(self) -> None:
        for attempt in range(5):
            try:
                self.temporary_directory.cleanup()
                return
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.02)

    def graph(
        self,
        nodes: list[dict[str, object]],
        *,
        resources: dict[str, int] | None = None,
        targets: list[str] | None = None,
        failure_policy: str = "continue",
    ):
        mapping = {
            "failure_policy": failure_policy,
            "resources": resources or {"cpu": 1},
            "targets": targets or [str(nodes[-1]["name"])],
            "nodes": nodes,
        }
        return driver.Graph.from_mapping("synthetic", mapping, self.workspace)

    def write_profile(self, graph: dict[str, object]) -> Path:
        path = self.workspace / "profile.json"
        path.write_text(
            json.dumps(
                {
                    "schema": 2,
                    "default_graph": "synthetic",
                    "graphs": {"synthetic": graph},
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_cycle_detection_reports_a_deterministic_cycle(self) -> None:
        graph = self.graph(
            [
                node("c", deps=("a",)),
                node("a", deps=("b",)),
                node("b", deps=("c",)),
            ],
            targets=["a"],
        )

        with self.assertRaisesRegex(driver.CycleError, r"a -> b -> c -> a"):
            graph.plan()

    def test_topological_plan_is_deterministic_across_declaration_order(self) -> None:
        declarations = [
            node("d", deps=("a", "c")),
            node("c", deps=("b",)),
            node("b"),
            node("a"),
        ]
        expected = ["a", "b", "c", "d"]

        first = self.graph(declarations, targets=["d"])
        second = self.graph(list(reversed(declarations)), targets=["d"])

        self.assertEqual([item.node.name for item in first.plan()], expected)
        self.assertEqual([item.node.name for item in second.plan()], expected)

    def test_unknown_graph_level_field_is_rejected(self) -> None:
        mapping = {
            "failure_policy": "continue",
            "resources": {"cpu": 1},
            "targets": ["safe"],
            "nodes": [node("safe")],
            "resoruces": {"typo": 1},
        }

        with self.assertRaisesRegex(driver.GraphValidationError, "unknown graph fields"):
            driver.Graph.from_mapping("synthetic", mapping, self.workspace)

    def test_workspace_run_lock_excludes_a_second_process(self) -> None:
        probe = """
import importlib.util
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("lock_probe_driver", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
try:
    with module._WorkspaceRunLock(Path(sys.argv[2])):
        pass
except module.GraphValidationError:
    raise SystemExit(0)
raise SystemExit(1)
"""

        with driver._WorkspaceRunLock(self.workspace):
            completed = subprocess.run(
                [sys.executable, "-c", probe, str(DRIVER_PATH), str(self.workspace)],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_graph_runner_holds_workspace_lock_for_the_entire_run(self) -> None:
        graph = self.graph([node("gate")])
        runner = driver.GraphRunner(graph, self.logs, self.state, max_workers=1)

        with driver._WorkspaceRunLock(self.workspace):
            with self.assertRaisesRegex(driver.GraphValidationError, "already running"):
                runner.run()

    def test_continue_policy_blocks_dependents_but_runs_independent_nodes(self) -> None:
        graph = self.graph(
            [
                node("failure"),
                node("dependent", deps=("failure",)),
                node("transitive", deps=("dependent",)),
                node("independent"),
            ],
            resources={"cpu": 4},
            targets=["transitive", "independent"],
        )
        executed: list[str] = []
        lock = threading.Lock()

        def execute(current, _workspace, stream, _cancellation) -> int:
            with lock:
                executed.append(current.name)
            stream.write(f"executed {current.name}\n")
            return 19 if current.name == "failure" else 0

        summary = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=4,
            executor=execute,
        ).run()

        self.assertEqual(summary.results["failure"].status, "failed")
        self.assertEqual(summary.results["dependent"].status, "blocked")
        self.assertEqual(summary.results["transitive"].status, "blocked")
        self.assertEqual(summary.results["independent"].status, "succeeded")
        self.assertCountEqual(executed, ["failure", "independent"])

    def test_order_only_finalizers_run_after_failure_while_dependents_block(self) -> None:
        graph = self.graph(
            [
                node("analyzer", writes=("raw",)),
                node("normalizer", run_after=("analyzer",), writes=("sarif",)),
                node("ordinary", deps=("analyzer",)),
                node(
                    "final",
                    deps=("normalizer",),
                    run_after=("analyzer",),
                    inputs=("sarif",),
                ),
            ],
            resources={"cpu": 2},
            targets=["ordinary", "final"],
        )
        executed: list[str] = []

        def execute(current, workspace, stream, _cancellation) -> int:
            executed.append(current.name)
            if current.name == "analyzer":
                (workspace / "raw").write_text("diagnostic", encoding="utf-8")
                return 9
            if current.name == "normalizer":
                (workspace / "sarif").write_text("normalized diagnostic", encoding="utf-8")
                return 0
            if current.name == "final":
                stream.write((workspace / "sarif").read_text(encoding="utf-8"))
                return 17
            return 0

        summary = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=2,
            executor=execute,
        ).run()

        self.assertEqual(executed, ["analyzer", "normalizer", "final"])
        self.assertEqual(summary.results["analyzer"].status, "failed")
        self.assertEqual(summary.results["normalizer"].status, "succeeded")
        self.assertEqual(summary.results["ordinary"].status, "blocked")
        self.assertEqual(summary.results["final"].status, "failed")
        self.assertIn("normalized diagnostic", summary.results["final"].log_path.read_text(encoding="utf-8"))

    def test_fail_fast_cancels_every_node_that_has_not_started(self) -> None:
        graph = self.graph(
            [
                node("a-failure"),
                node("b-dependent", deps=("a-failure",)),
                node("c-independent"),
            ],
            targets=["b-dependent", "c-independent"],
            failure_policy="fail-fast",
        )
        executed: list[str] = []

        def execute(current, _workspace, stream, _cancellation) -> int:
            executed.append(current.name)
            stream.write(current.name)
            return 7

        summary = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=1,
            executor=execute,
        ).run()

        self.assertEqual(executed, ["a-failure"])
        self.assertEqual(summary.results["a-failure"].status, "failed")
        self.assertEqual(summary.results["b-dependent"].status, "cancelled")
        self.assertEqual(summary.results["c-independent"].status, "cancelled")
        self.assertFalse(summary.succeeded)

    def test_fail_fast_signals_already_running_executors_through_the_cancellation_seam(self) -> None:
        graph = self.graph(
            [node("a-failure"), node("b-running")],
            resources={"cpu": 2},
            targets=["a-failure", "b-running"],
            failure_policy="fail-fast",
        )
        both_started = threading.Barrier(2)
        running_saw_cancellation = threading.Event()

        def execute(current, _workspace, stream, cancellation) -> int:
            both_started.wait(timeout=2)
            if current.name == "a-failure":
                return 23
            if cancellation.wait(timeout=2):
                running_saw_cancellation.set()
            stream.write("running node stopped\n")
            return 0

        summary = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=2,
            executor=execute,
        ).run()

        self.assertTrue(running_saw_cancellation.is_set())
        self.assertEqual(summary.results["a-failure"].status, "failed")
        self.assertEqual(summary.results["b-running"].status, "cancelled")

    def test_weighted_resources_are_acquired_and_released_atomically(self) -> None:
        declarations = [
            node("compile-a", resources={"cpu": 2, "toolchain": 1}),
            node("compile-b", resources={"cpu": 2, "toolchain": 1}),
            node("report", resources={"cpu": 1, "report-io": 1}),
        ]
        capacities = {"cpu": 3, "toolchain": 1, "report-io": 1}
        graph = self.graph(
            declarations,
            resources=capacities,
            targets=[str(item["name"]) for item in declarations],
        )
        active = {name: 0 for name in capacities}
        maximum = {name: 0 for name in capacities}
        maximum_processes = 0
        process_count = 0
        lock = threading.Lock()

        def execute(current, _workspace, stream, _cancellation) -> int:
            nonlocal maximum_processes, process_count
            demands = dict(current.resources)
            with lock:
                process_count += 1
                maximum_processes = max(maximum_processes, process_count)
                for resource, demand in demands.items():
                    active[resource] += demand
                    maximum[resource] = max(maximum[resource], active[resource])
            time.sleep(0.04)
            with lock:
                for resource, demand in demands.items():
                    active[resource] -= demand
                process_count -= 1
            stream.write(f"executed {current.name}\n")
            return 0

        summary = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=3,
            executor=execute,
        ).run()

        self.assertTrue(summary.succeeded)
        self.assertLessEqual(maximum["cpu"], capacities["cpu"])
        self.assertLessEqual(maximum["toolchain"], capacities["toolchain"])
        self.assertGreaterEqual(maximum_processes, 2)

    def test_resource_demand_cannot_exceed_capacity(self) -> None:
        with self.assertRaisesRegex(driver.GraphValidationError, "exceeds capacity"):
            self.graph(
                [node("too-heavy", resources={"cpu": 3})],
                resources={"cpu": 2},
            )

    def test_zero_max_workers_is_rejected(self) -> None:
        graph = self.graph([node("safe")])

        with self.assertRaisesRegex(driver.GraphValidationError, "max_workers must be positive"):
            driver.GraphRunner(graph, self.logs, self.state, max_workers=0)

    def test_ready_queue_preserves_age_when_new_lexical_predecessors_arrive(self) -> None:
        graph = self.graph(
            [
                node("a0"),
                node("a1", deps=("a0",)),
                node("a2", deps=("a1",)),
                node("z-old"),
            ],
            targets=["a2", "z-old"],
        )
        executed: list[str] = []

        def execute(current, _workspace, stream, _cancellation) -> int:
            executed.append(current.name)
            stream.write(current.name)
            return 0

        driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=1,
            executor=execute,
        ).run()

        self.assertEqual(executed, ["a0", "z-old", "a1", "a2"])

    def test_ready_queue_backfills_a_node_using_disjoint_resources(self) -> None:
        graph = self.graph(
            [
                node("a-hold", resources={"serial": 1}),
                node("b-wait", resources={"serial": 1}),
                node("c-other", resources={"other": 1}),
            ],
            resources={"serial": 1, "other": 1},
            targets=["a-hold", "b-wait", "c-other"],
        )
        started: list[str] = []
        active = 0
        maximum = 0
        lock = threading.Lock()

        def execute(current, _workspace, stream, _cancellation) -> int:
            nonlocal active, maximum
            with lock:
                started.append(current.name)
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            stream.write(current.name)
            return 0

        driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=2,
            executor=execute,
        ).run()

        self.assertEqual(started[0], "a-hold")
        self.assertIn("c-other", started[:2])
        self.assertEqual(maximum, 2)

    def test_unordered_overlapping_write_roots_are_rejected(self) -> None:
        graph = self.graph(
            [
                node("first", writes=("out/shared",)),
                node("second", writes=("out/shared/child",)),
            ],
            resources={"cpu": 2},
            targets=["first", "second"],
        )

        with self.assertRaisesRegex(driver.GraphValidationError, "overlapping writes"):
            graph.plan()

    def test_dependency_order_allows_overlapping_write_roots(self) -> None:
        graph = self.graph(
            [
                node("first", writes=("out/shared",)),
                node("second", deps=("first",), writes=("out/shared/child",)),
            ],
            targets=["second"],
        )

        self.assertEqual([item.node.name for item in graph.plan()], ["first", "second"])

    def test_shared_capacity_one_resource_allows_overlapping_write_roots(self) -> None:
        graph = self.graph(
            [
                node("first", resources={"cpu": 1, "staging": 1}, writes=("out/shared",)),
                node("second", resources={"cpu": 1, "staging": 1}, writes=("out/shared",)),
            ],
            resources={"cpu": 2, "staging": 1},
            targets=["first", "second"],
        )

        self.assertEqual(len(graph.plan()), 2)

    def test_shared_capacity_two_resource_does_not_protect_overlapping_writes(self) -> None:
        graph = self.graph(
            [
                node("first", resources={"staging": 1}, writes=("out/shared",)),
                node("second", resources={"staging": 1}, writes=("out/shared",)),
            ],
            resources={"staging": 2},
            targets=["first", "second"],
        )

        with self.assertRaisesRegex(driver.GraphValidationError, "overlapping writes"):
            graph.plan()

    def test_outputs_must_be_confined_below_a_declared_write_root(self) -> None:
        with self.assertRaisesRegex(driver.GraphValidationError, "declared write root"):
            self.graph(
                [
                    node(
                        "invalid",
                        writes=("out/owned",),
                        outputs=("out/elsewhere/result.txt",),
                        cacheable=True,
                    )
                ]
            )

    def test_graph_paths_and_runtime_directories_cannot_escape_workspace(self) -> None:
        with self.assertRaisesRegex(driver.GraphValidationError, "unsafe input"):
            self.graph([node("traversal", inputs=("../outside.txt",))])
        with self.assertRaisesRegex(driver.GraphValidationError, "unsafe write"):
            self.graph([node("absolute", writes=("C:/outside",))])

        graph = self.graph([node("safe")])
        with self.assertRaisesRegex(driver.GraphValidationError, "inside the workspace"):
            driver.GraphRunner(graph, self.workspace.parent / "logs", self.state)

    def test_cacheable_node_requires_explicit_outputs(self) -> None:
        with self.assertRaisesRegex(driver.GraphValidationError, "explicit outputs"):
            self.graph([node("invalid", cacheable=True)])

    def test_failed_rerun_invalidates_old_state_before_partial_output(self) -> None:
        graph = self.graph(
            [
                node(
                    "producer",
                    writes=("output.txt",),
                    outputs=("output.txt",),
                    fingerprint=("tool=v1",),
                    cacheable=True,
                )
            ]
        )
        attempts = 0

        def execute(_current, workspace, stream, _cancellation) -> int:
            nonlocal attempts
            attempts += 1
            (workspace / "output.txt").write_text(f"attempt={attempts}", encoding="utf-8")
            stream.write(f"attempt={attempts}\n")
            return 1 if attempts == 2 else 0

        runner = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=1,
            executor=execute,
        )
        first = runner.run()
        (self.workspace / "output.txt").unlink()
        failed = runner.run()
        recovered = runner.run()

        self.assertEqual(first.results["producer"].status, "succeeded")
        self.assertEqual(failed.results["producer"].status, "failed")
        self.assertEqual(recovered.results["producer"].status, "succeeded")
        self.assertEqual(attempts, 3)

    def test_output_content_tampering_invalidates_cache(self) -> None:
        graph = self.graph(
            [
                node(
                    "producer",
                    writes=("output.txt",),
                    outputs=("output.txt",),
                    cacheable=True,
                )
            ]
        )
        attempts = 0

        def execute(_current, workspace, stream, _cancellation) -> int:
            nonlocal attempts
            attempts += 1
            (workspace / "output.txt").write_text(f"attempt={attempts}", encoding="utf-8")
            stream.write(f"attempt={attempts}\n")
            return 0

        runner = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=1,
            executor=execute,
        )
        first = runner.run()
        warm = runner.run()
        (self.workspace / "output.txt").write_text("tampered", encoding="utf-8")
        repaired = runner.run()

        self.assertEqual(first.results["producer"].status, "succeeded")
        self.assertEqual(warm.results["producer"].status, "cached")
        self.assertEqual(repaired.results["producer"].status, "succeeded")
        self.assertEqual(attempts, 2)
        state = json.loads((self.state / "producer.json").read_text(encoding="utf-8"))
        self.assertEqual(state["schema"], 2)
        self.assertEqual(state["outputs"][0]["path"], "output.txt")
        self.assertIn("sha256", state["outputs"][0])

    def test_successful_cacheable_node_hashes_its_output_manifest_once(self) -> None:
        graph = self.graph(
            [
                node(
                    "producer",
                    writes=("output.txt",),
                    outputs=("output.txt",),
                    cacheable=True,
                )
            ]
        )

        def execute(_current, workspace, stream, _cancellation) -> int:
            (workspace / "output.txt").write_text("content", encoding="utf-8")
            stream.write("produced")
            return 0

        runner = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=1,
            executor=execute,
        )
        with mock.patch.object(runner, "_output_manifest", wraps=runner._output_manifest) as manifest:
            summary = runner.run()

        self.assertEqual(summary.results["producer"].status, "succeeded")
        self.assertEqual(manifest.call_count, 1)

    def test_save_failure_becomes_failed_node_and_leaves_no_cache_state(self) -> None:
        graph = self.graph(
            [
                node(
                    "producer",
                    writes=("output.txt",),
                    outputs=("output.txt",),
                    cacheable=True,
                )
            ]
        )

        def execute(_current, workspace, _stream, _cancellation) -> int:
            (workspace / "output.txt").write_text("content", encoding="utf-8")
            return 0

        runner = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=1,
            executor=execute,
        )
        with mock.patch.object(runner, "_save", side_effect=OSError("state disk full")):
            summary = runner.run()

        result = summary.results["producer"]
        self.assertEqual(result.status, "failed")
        self.assertIn("state disk full", result.detail)
        self.assertFalse((self.state / "producer.json").exists())

    def test_missing_manifest_output_is_a_failed_node_with_invalidated_state(self) -> None:
        graph = self.graph(
            [
                node(
                    "producer",
                    writes=("output.txt",),
                    outputs=("output.txt",),
                    cacheable=True,
                )
            ]
        )
        self.state.mkdir()
        (self.state / "producer.json").write_text("stale", encoding="utf-8")

        def execute(_current, _workspace, _stream, _cancellation) -> int:
            return 0

        summary = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=1,
            executor=execute,
        ).run()

        self.assertEqual(summary.results["producer"].status, "failed")
        self.assertFalse((self.state / "producer.json").exists())

    def test_ready_cache_validation_runs_outside_the_dispatch_loop(self) -> None:
        declarations = [
            node(
                name,
                writes=(f"{name}.txt",),
                outputs=(f"{name}.txt",),
                cacheable=True,
            )
            for name in ("first", "second")
        ]
        graph = self.graph(
            declarations,
            resources={"cpu": 2},
            targets=["first", "second"],
        )
        barrier = threading.Barrier(2)

        def validate(_item) -> bool:
            barrier.wait(timeout=2)
            return False

        def execute(current, workspace, _stream, _cancellation) -> int:
            (workspace / f"{current.name}.txt").write_text(current.name, encoding="utf-8")
            return 0

        runner = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=2,
            executor=execute,
        )
        with mock.patch.object(runner, "_cached", side_effect=validate):
            summary = runner.run()

        self.assertTrue(summary.succeeded)

    def test_cache_validation_obeys_capacity_one_output_lock(self) -> None:
        declarations = [
            node(
                name,
                resources={"shared-output": 1},
                writes=("shared",),
                outputs=("shared",),
                cacheable=True,
            )
            for name in ("first", "second")
        ]
        graph = self.graph(
            declarations,
            resources={"shared-output": 1},
            targets=["first", "second"],
        )
        active = 0
        maximum = 0
        lock = threading.Lock()

        def validate(_item) -> bool:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return True

        runner = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=2,
            executor=lambda *_args: 0,
        )
        with mock.patch.object(runner, "_cached", side_effect=validate):
            summary = runner.run()

        self.assertTrue(summary.succeeded)
        self.assertEqual(maximum, 1)

    def test_rebuilt_prerequisite_prevents_a_stale_dependent_cache_hit(self) -> None:
        graph = self.graph(
            [
                node(
                    "producer",
                    writes=("producer.txt",),
                    outputs=("producer.txt",),
                    cacheable=True,
                ),
                node(
                    "consumer",
                    deps=("producer",),
                    inputs=("producer.txt",),
                    writes=("consumer.txt",),
                    outputs=("consumer.txt",),
                    cacheable=True,
                ),
            ],
            targets=["consumer"],
        )
        attempts = {"producer": 0, "consumer": 0}

        def execute(current, workspace, stream, _cancellation) -> int:
            attempts[current.name] += 1
            if current.name == "producer":
                (workspace / "producer.txt").write_text(
                    f"producer={attempts[current.name]}",
                    encoding="utf-8",
                )
            else:
                source = (workspace / "producer.txt").read_text(encoding="utf-8")
                (workspace / "consumer.txt").write_text(source, encoding="utf-8")
            stream.write(current.name)
            return 0

        runner = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=1,
            executor=execute,
        )
        runner.run()
        warm = runner.run()
        (self.workspace / "producer.txt").write_text("tampered", encoding="utf-8")
        repaired = runner.run()

        self.assertEqual(warm.results["producer"].status, "cached")
        self.assertEqual(warm.results["consumer"].status, "cached")
        self.assertEqual(repaired.results["producer"].status, "succeeded")
        self.assertEqual(repaired.results["consumer"].status, "succeeded")
        self.assertEqual(attempts, {"producer": 2, "consumer": 2})

    def test_every_run_uses_a_unique_log_directory(self) -> None:
        graph = self.graph([node("gate")])
        attempts = 0

        def execute(_current, _workspace, stream, _cancellation) -> int:
            nonlocal attempts
            attempts += 1
            stream.write(f"attempt={attempts}\n")
            return 0

        runner = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=1,
            executor=execute,
        )
        first = runner.run().results["gate"]
        second = runner.run().results["gate"]

        self.assertNotEqual(first.log_path, second.log_path)
        self.assertIn("attempt=1", first.log_path.read_text(encoding="utf-8"))
        self.assertIn("attempt=2", second.log_path.read_text(encoding="utf-8"))

    def test_shared_input_is_hashed_only_once_per_plan(self) -> None:
        (self.workspace / "input.txt").write_text("content", encoding="utf-8")
        graph = self.graph(
            [
                node("first", inputs=("input.txt",)),
                node("second", inputs=("input.txt",)),
            ],
            resources={"cpu": 2},
            targets=["first", "second"],
        )

        with mock.patch.object(driver, "_sha256_file", wraps=driver._sha256_file) as digest:
            graph.plan()

        self.assertEqual(digest.call_count, 1)

    def test_matrix_expansion_is_deterministic_and_applies_exact_excludes(self) -> None:
        build_node = node(
            "build-{arch}",
            writes=("out/build/{arch}",),
        )
        build_node["matrix"] = {
            "axes": {"arch": ["x86", "x64"]},
            "exclude": [],
        }
        matrix_node = node(
            "test-{arch}-{config}",
            deps=("build-{arch}",),
            resources={"cpu": 1},
            argv=("tool", "--arch", "{arch}", "--config", "{config}"),
            writes=("out/{arch}/{config}",),
        )
        matrix_node["matrix"] = {
            "axes": {"config": ["Release", "Debug"], "arch": ["x64", "x86"]},
            "exclude": [{"arch": "x86", "config": "Release"}],
        }
        profile = self.write_profile(
            {
                "variables": {},
                "failure_policy": "continue",
                "resources": {"cpu": 2},
                "targets": ["test-x64-Debug", "test-x64-Release", "test-x86-Debug"],
                "nodes": [matrix_node, build_node],
            }
        )

        graph = driver.load_graph_profile(profile, None, self.workspace)

        self.assertEqual(
            sorted(graph.nodes),
            ["build-x64", "build-x86", "test-x64-Debug", "test-x64-Release", "test-x86-Debug"],
        )
        self.assertEqual(graph.nodes["test-x64-Debug"].argv, ("tool", "--arch", "x64", "--config", "Debug"))
        self.assertEqual(graph.nodes["test-x64-Debug"].deps, ("build-x64",))
        self.assertEqual(graph.nodes["test-x86-Debug"].writes, ("out/x86/Debug",))

    def test_matrix_axis_cannot_collide_with_graph_variable(self) -> None:
        matrix_node = node("test-{arch}")
        matrix_node["matrix"] = {"axes": {"arch": ["x64"]}, "exclude": []}
        profile = self.write_profile(
            {
                "variables": {"arch": "x64"},
                "resources": {"cpu": 1},
                "targets": ["test-x64"],
                "nodes": [matrix_node],
            }
        )

        with self.assertRaisesRegex(driver.GraphValidationError, "collides with graph variable"):
            driver.load_graph_profile(profile, None, self.workspace)

    def test_matrix_exclude_must_be_an_exact_known_assignment(self) -> None:
        matrix_node = node("test-{arch}-{config}")
        matrix_node["matrix"] = {
            "axes": {"arch": ["x64", "x86"], "config": ["Debug", "Release"]},
            "exclude": [{"arch": "x86"}],
        }
        profile = self.write_profile(
            {
                "variables": {},
                "resources": {"cpu": 1},
                "targets": ["test-x64-Debug"],
                "nodes": [matrix_node],
            }
        )

        with self.assertRaisesRegex(driver.GraphValidationError, "exact assignment"):
            driver.load_graph_profile(profile, None, self.workspace)

    def test_profile_schema_one_is_rejected_without_ambiguous_compatibility(self) -> None:
        profile = self.workspace / "v1.json"
        profile.write_text(json.dumps({"schema": 1, "graphs": {}}), encoding="utf-8")

        with self.assertRaisesRegex(driver.GraphValidationError, "profile schema must be 2"):
            driver.load_graph_profile(profile, None, self.workspace)

    def test_legacy_pool_field_is_rejected(self) -> None:
        legacy = node("legacy")
        legacy["pool"] = "cpu"

        with self.assertRaisesRegex(driver.GraphValidationError, "legacy pool"):
            self.graph([legacy])

    def test_default_executor_uses_argv_without_a_shell(self) -> None:
        graph = self.graph([node("safe", argv=("program", "argument with spaces"))])
        current = graph.nodes["safe"]
        completed = subprocess.CompletedProcess(current.argv, 0)

        with mock.patch.object(driver.subprocess, "run", return_value=completed) as run:
            return_code = driver.execute_subprocess(
                current,
                self.workspace,
                io.StringIO(),
                threading.Event(),
            )

        self.assertEqual(return_code, 0)
        positional, keyword = run.call_args
        self.assertEqual(positional[0], ["program", "argument with spaces"])
        self.assertIs(keyword["shell"], False)

    def test_default_runner_writes_stdout_and_stderr_to_per_node_log(self) -> None:
        graph = self.graph(
            [
                node(
                    "logged",
                    argv=(
                        sys.executable,
                        "-c",
                        "import sys; print('stdout-line'); print('stderr-line', file=sys.stderr)",
                    ),
                )
            ]
        )

        summary = driver.GraphRunner(
            graph,
            self.logs,
            self.state,
            max_workers=1,
        ).run()

        result = summary.results["logged"]
        self.assertEqual(result.status, "succeeded")
        log_text = result.log_path.read_text(encoding="utf-8")
        self.assertIn("stdout-line", log_text)
        self.assertIn("stderr-line", log_text)

    def test_json_plan_output_is_stable_and_normalized(self) -> None:
        graph = self.graph([node("b"), node("a")], resources={"cpu": 2}, targets=["a", "b"])

        first = driver.plan_as_json(graph.plan())
        second = driver.plan_as_json(graph.plan())

        self.assertEqual(first, second)
        parsed = json.loads(first)
        self.assertEqual([item["name"] for item in parsed], ["a", "b"])
        self.assertEqual(parsed[0]["resources"], {"cpu": 1})
        self.assertEqual(parsed[0]["writes"], [])
        self.assertNotIn("pool", parsed[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)

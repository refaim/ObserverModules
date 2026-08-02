from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import core.result_export as result_export  # noqa: E402
from core.graph import Command, Graph, Node, Result  # noqa: E402
from core.paths import BuildPaths  # noqa: E402
from core.result_export import ResultExportError, export_results  # noqa: E402
from core.store import CasStore  # noqa: E402


def node(name: str, *results: Result) -> Node:
    return Node(
        name,
        hashlib.md5(name.encode(), usedforsecurity=False).hexdigest(),
        "cpu",
        Command(("tool",)),
        results=results,
    )


class ResultExportTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[BuildPaths, CasStore]:
        repository = root / "repo"
        repository.mkdir()
        paths = BuildPaths(repository)
        paths.prepare()
        return paths, CasStore(paths, "export-test")

    @staticmethod
    def prepare(store: CasStore, current: Node, *, complete: bool = True) -> Path:
        cas = store.prepare_entry(current)
        cas.log.write_text(f"log for {current.name}\n", encoding="utf-8")
        if complete:
            store.mark_complete(current)
        return cas.output

    def test_files_and_directories_publish_by_logical_id_with_manifest_last(self) -> None:
        package = Result(
            "packages/x64/renpy.zip", "package", "application/zip", "artifacts/renpy.zip"
        )
        coverage = Result(
            "reports/coverage/x64", "report", "application/json", "coverage"
        )
        producer = node("publish", package, coverage)
        graph = Graph((producer,), (producer.name,), {"cpu": 1})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _paths, store = self.fixture(root)
            output = self.prepare(store, producer)
            (output / "artifacts").mkdir()
            (output / "artifacts/renpy.zip").write_bytes(b"archive")
            (output / "coverage/sub").mkdir(parents=True)
            (output / "coverage/index.json").write_bytes(b"{}")
            (output / "coverage/sub/detail.json").write_bytes(b'{"ok":true}')
            destination = root / "export"

            write_manifest = result_export._write_manifest
            manifest_order: list[Path] = []

            def observe(staging: Path, document: dict[str, object]) -> None:
                manifest_order.append(staging)
                root_staging = staging.parent if staging.name == "packages" else staging
                self.assertTrue((root_staging / "packages/x64/renpy.zip").is_file())
                self.assertTrue((root_staging / coverage.id / "sub/detail.json").is_file())
                self.assertFalse((root_staging / "manifest.json").exists())
                if staging.name == "packages":
                    self.assertEqual(
                        [entry["path"] for entry in document["results"]],
                        ["x64/renpy.zip"],
                    )
                else:
                    self.assertTrue((staging / "packages/manifest.json").is_file())
                    self.assertEqual(
                        [entry["path"] for entry in document["results"]],
                        [coverage.id],
                    )
                write_manifest(staging, document)

            with mock.patch.object(result_export, "_write_manifest", side_effect=observe) as write:
                published = export_results(graph, store, destination, "verify", "success")

            self.assertEqual(published, destination)
            self.assertEqual(write.call_count, 2)
            self.assertEqual(manifest_order[0], manifest_order[1] / "packages")
            self.assertEqual((destination / "packages/x64/renpy.zip").read_bytes(), b"archive")
            manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
            package_manifest = json.loads(
                (destination / "packages/manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["command"], "verify")
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["failures"], [])
        self.assertEqual(manifest["logs"], [])
        self.assertEqual([entry["path"] for entry in manifest["results"]], [coverage.id])
        self.assertEqual(package_manifest["schema"], 1)
        self.assertEqual(package_manifest["command"], "verify")
        self.assertEqual(package_manifest["status"], "success")
        self.assertEqual(package_manifest["failures"], [])
        self.assertEqual(package_manifest["logs"], [])
        self.assertEqual(
            [entry["path"] for entry in package_manifest["results"]],
            ["x64/renpy.zip"],
        )
        file_entry = package_manifest["results"][0]
        self.assertEqual(file_entry["id"], package.id)
        directory_entry = manifest["results"][0]
        self.assertEqual(file_entry["size"], 7)
        self.assertEqual(file_entry["sha256"], hashlib.sha256(b"archive").hexdigest())
        self.assertEqual(file_entry["object_type"], "file")
        self.assertEqual(directory_entry["size"], len(b"{}") + len(b'{"ok":true}'))
        self.assertEqual(directory_entry["object_type"], "directory")
        self.assertRegex(directory_entry["sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn(str(root), json.dumps(manifest))
        self.assertNotIn(str(root), json.dumps(package_manifest))

    def test_failed_export_includes_existing_logs_and_only_complete_results(self) -> None:
        report = Result("reports/ready.json", "report", "application/json", "ready.json")
        package = Result(
            "packages/x64/ready.zip", "package", "application/zip", "ready.zip"
        )
        partial = Result("reports/partial.json", "report", "application/json", "partial.json")
        ready, failed, pending = node("ready", report, package), node("failed", partial), node("pending")
        graph = Graph(
            (ready, failed, pending),
            (ready.name, failed.name, pending.name),
            {"cpu": 1},
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _paths, store = self.fixture(root)
            ready_output = self.prepare(store, ready)
            (ready_output / "ready.json").write_bytes(b"ready")
            (ready_output / "ready.zip").write_bytes(b"package")
            failed_output = self.prepare(store, failed, complete=False)
            (failed_output / "partial.json").write_bytes(b"partial")

            destination = root / "failed-export"
            export_results(
                graph,
                store,
                destination,
                "verify",
                "failed",
                failures=(failed.name,),
            )
            manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))

            self.assertTrue((destination / report.id).is_file())
            self.assertFalse((destination / "packages").exists())
            self.assertFalse((destination / partial.id).exists())
            self.assertEqual((destination / "logs/ready.log").read_text(), "log for ready\n")
            self.assertEqual((destination / "logs/failed.log").read_text(), "log for failed\n")

        self.assertEqual(manifest["failures"], [failed.name])
        self.assertEqual([entry["id"] for entry in manifest["results"]], [report.id])
        self.assertEqual(
            [entry["path"] for entry in manifest["logs"]],
            ["logs/ready.log", "logs/failed.log"],
        )

    def test_root_manifest_publishes_cache_identity_report(self) -> None:
        producer = node("cached")
        graph = Graph((producer,), (producer.name,), {"cpu": 1})
        cache = {
            "schema": 1,
            "summary": {"executed": 0, "failed": 0, "hit": 1, "incomplete": 0},
            "nodes": [{
                "duration_ms": 0,
                "name": producer.name,
                "state": "hit",
                "uid": producer.uid,
            }],
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _paths, store = self.fixture(root)
            self.prepare(store, producer)
            destination = root / "export"

            export_results(
                graph, store, destination, "verify", "success", cache=cache
            )
            manifest = json.loads(
                (destination / "manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(manifest["cache"], cache)

    def test_existing_destination_missing_result_and_path_collisions_are_rejected(self) -> None:
        cases = (
            "existing",
            "missing",
            "component",
            "collision",
            "reserved",
            "package-reserved",
            "packages-root",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _paths, store = self.fixture(root)
                results = {
                    "collision": (
                        Result("reports", "report", "application/json", "one.json"),
                        Result("reports/child", "report", "application/json", "two.json"),
                    ),
                    "reserved": (
                        Result("manifest.json", "report", "application/json", "one.json"),
                    ),
                    "package-reserved": (
                        Result(
                            "packages/manifest.json",
                            "package",
                            "application/json",
                            "one.json",
                        ),
                    ),
                    "packages-root": (
                        Result("packages", "report", "application/json", "one.json"),
                    ),
                }.get(case, (Result("report", "report", "application/json", "one.json"),))
                producer = node("producer", *results)
                graph = Graph((producer,), (producer.name,), {"cpu": 1})
                output = self.prepare(store, producer)
                if case != "missing":
                    (output / "one.json").write_bytes(b"one")
                if case == "component":
                    producer = node(
                        "nested",
                        Result("nested", "report", "application/json", "one.json/child.json"),
                    )
                    graph = Graph((producer,), (producer.name,), {"cpu": 1})
                    nested_output = self.prepare(store, producer)
                    (nested_output / "one.json").write_bytes(b"not a directory")
                if case == "collision":
                    (output / "two.json").write_bytes(b"two")
                destination = root / "export"
                if case == "existing":
                    destination.mkdir()
                    (destination / "sentinel").write_bytes(b"keep")

                with self.assertRaises(ResultExportError):
                    export_results(graph, store, destination, "verify", "success")

                if case == "existing":
                    self.assertEqual((destination / "sentinel").read_bytes(), b"keep")
                else:
                    self.assertFalse(destination.exists())

    def test_reparse_sources_and_publication_failure_leave_no_partial_destination(self) -> None:
        result = Result("reports/result.json", "report", "application/json", "result.json")
        package = Result("packages/x64/result.zip", "package", "application/zip", "result.zip")
        producer = node("producer", result, package)
        graph = Graph((producer,), (producer.name,), {"cpu": 1})

        for case in ("reparse", "manifest-failure"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _paths, store = self.fixture(root)
                output = self.prepare(store, producer)
                source = output / result.relative_path
                source.write_bytes(b"result")
                (output / package.relative_path).write_bytes(b"package")
                destination = root / "export"
                if case == "reparse":
                    patch = mock.patch.object(
                        result_export, "_is_reparse", side_effect=lambda path: path == source
                    )
                else:
                    write_manifest = result_export._write_manifest

                    def fail_root_manifest(
                        staging: Path, document: dict[str, object]
                    ) -> None:
                        if staging.name == "packages":
                            write_manifest(staging, document)
                            return
                        raise OSError("disk full")

                    patch = mock.patch.object(
                        result_export, "_write_manifest", side_effect=fail_root_manifest
                    )

                with patch, self.assertRaises(ResultExportError):
                    export_results(graph, store, destination, "verify", "success")

                self.assertFalse(destination.exists())
                self.assertEqual([child.name for child in root.iterdir()], ["repo"])

    def test_unsupported_entries_logs_and_destination_paths_are_rejected(self) -> None:
        directory = Result("tree", "report", "application/octet-stream", "tree")
        producer = node("producer", directory)
        graph = Graph((producer,), (producer.name,), {"cpu": 1})
        cases = ("root-special", "child-special", "log-directory", "missing-parent", "dest-reparse")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _paths, store = self.fixture(root)
                output = self.prepare(store, producer)
                (output / "tree").mkdir()
                special = output / "tree/special"
                special.write_bytes(b"special")
                destination = root / "export"
                if case == "root-special":
                    patch = mock.patch.object(
                        result_export,
                        "_source",
                        return_value=(output / "tree", SimpleNamespace(st_mode=stat.S_IFIFO)),
                    )
                elif case == "child-special":
                    checked = result_export._checked
                    patch = mock.patch.object(
                        result_export,
                        "_checked",
                        side_effect=lambda path: (
                            SimpleNamespace(st_mode=stat.S_IFIFO)
                            if path == special
                            else checked(path)
                        ),
                    )
                elif case == "log-directory":
                    cas = store.paths_for(producer)
                    cas.log.unlink()
                    cas.log.mkdir()
                    patch = mock.patch.object(store, "is_complete", return_value=False)
                elif case == "missing-parent":
                    destination = root / "missing/export"
                    patch = mock.patch.object(result_export, "_write_manifest", wraps=result_export._write_manifest)
                else:
                    patch = mock.patch.object(
                        result_export, "_is_reparse", side_effect=lambda path: path == root
                    )

                status = "failed" if case == "log-directory" else "success"
                with patch, self.assertRaises(ResultExportError):
                    export_results(graph, store, destination, "verify", status)

                self.assertFalse(destination.exists())

    def test_publication_race_does_not_replace_the_winner(self) -> None:
        only = node("only")
        graph = Graph((only,), (only.name,), {"cpu": 1})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _paths, store = self.fixture(root)
            destination = root / "export"
            lstat = result_export._lstat
            destination_checks = 0

            def raced(path: Path):
                nonlocal destination_checks
                if path == destination:
                    destination_checks += 1
                    if destination_checks == 2:
                        return SimpleNamespace(st_mode=stat.S_IFDIR)
                return lstat(path)

            with mock.patch.object(result_export, "_lstat", side_effect=raced):
                with self.assertRaisesRegex(ResultExportError, "already exists"):
                    export_results(graph, store, destination, "verify", "success")

            self.assertFalse(destination.exists())

    def test_manifest_inputs_are_closed_over_graph_names_and_status(self) -> None:
        only = node("only")
        graph = Graph((only,), (only.name,), {"cpu": 1})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _paths, store = self.fixture(root)
            invalid = (
                (42, "success", ()),
                (r"C:\absolute\command.exe", "success", ()),
                ("verify", "unknown", ()),
                ("verify", "success", ("missing",)),
                ("verify", "success", (only.name,)),
                ("verify", "failed", (only.name, only.name)),
            )
            for index, (command, status, failures) in enumerate(invalid):
                with self.subTest(command=command, status=status, failures=failures):
                    with self.assertRaises(ResultExportError):
                        export_results(
                            graph,
                            store,
                            root / f"export-{index}",
                            command,
                            status,
                            failures=failures,
                        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

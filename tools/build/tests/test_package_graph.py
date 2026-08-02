from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.graph import Command, Graph, Node  # noqa: E402
from core.package import PackageError, main as package_main  # noqa: E402
from core.paths import BuildPaths  # noqa: E402
from graphs.package import PackageArtifact, PackageSmokeArtifact, package_graph, package_outputs  # noqa: E402


MODULES = ("renpy", "rpgmaker", "zanzarah")
LICENSES = {
    "renpy": ("Observer.txt", "rpatool.txt", "serde-pickle.txt", "zlib.txt"),
    "rpgmaker": ("Observer.txt", "rgssad.txt"),
    "zanzarah": ("Observer.txt", "zanzapak.txt"),
}


def node(name: str, seed: str | None = None, *, inputs: tuple[str, ...] = ()) -> Node:
    return Node(name, hashlib.md5((seed or name).encode()).hexdigest(), "build", Command(("build",)), inputs)


class PackageGraphTests(unittest.TestCase):
    def repository(self, root: Path) -> Path:
        for module, licenses in LICENSES.items():
            registration = root / f"src/modules/{module}/observer_user.ini"
            registration.parent.mkdir(parents=True, exist_ok=True)
            registration.write_text(f"[{module}]\n", encoding="utf-8")
            for name in licenses:
                path = root / "licenses" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"license:{name}\n", encoding="utf-8")
        (root / "LICENSE.txt").write_text("project license\n", encoding="utf-8")
        return root

    def fixture(
        self, root: Path, architectures: tuple[str, ...] = ("x64", "arm64")
    ) -> tuple[Path, Graph, tuple[PackageArtifact, ...]]:
        repository = self.repository(root / "repo")
        paths = BuildPaths(repository)
        producers = tuple(
            node(f"build-{module}-{architecture}-release")
            for architecture in architectures
            for module in MODULES
        )
        audit_gates = tuple(
            node(f"audit-{kind}-{architecture}-{module}", inputs=(producer.name,))
            for producer, (architecture, module) in zip(
                producers,
                ((architecture, module) for architecture in architectures for module in MODULES),
                strict=True,
            )
            for kind in ("pe", "binskim")
        )
        upstream = Graph((*producers, *audit_gates), tuple(item.name for item in producers), {"build": 4})
        artifacts = tuple(
            PackageArtifact(
                architecture,
                module,
                producer,
                paths.cas(producer.uid, producer.name).output / f"{module}.so",
                paths.cas(producer.uid, producer.name).output / f"{module}.pdb",
            )
            for producer, (architecture, module) in zip(
                producers,
                ((architecture, module) for architecture in architectures for module in MODULES),
                strict=True,
            )
        )
        return repository, upstream, artifacts

    def test_package_units_fan_out_and_only_inherent_aggregates_fan_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, upstream, artifacts = self.fixture(Path(temporary))
            graph = package_graph(repository, upstream, artifacts, jobs=7)
            paths = BuildPaths(repository)
            outputs = package_outputs(repository, graph)
            with self.assertRaisesRegex(ValueError, "no package outputs"):
                package_outputs(repository, upstream)

        self.assertEqual(graph.pools, {"build": 4, "package": 7})
        self.assertEqual(len(graph.nodes) - len(upstream.nodes), 29)
        self.assertEqual(graph.targets, ("package-manifest",))
        for artifact in artifacts:
            suffix = f"{artifact.architecture}-{artifact.module}"
            stage = graph.node(f"package-stage-{suffix}")
            symbols = graph.node(f"package-symbol-stage-{suffix}")
            archive = graph.node(f"package-archive-{suffix}")
            validation = graph.node(f"package-validate-{suffix}")
            gates = (
                f"audit-pe-{artifact.architecture}-{artifact.module}",
                f"audit-binskim-{artifact.architecture}-{artifact.module}",
            )
            self.assertEqual(stage.inputs, (artifact.producer.name, *gates))
            self.assertEqual(symbols.inputs, (artifact.producer.name, *gates))
            self.assertEqual(archive.inputs, (stage.name, *gates))
            self.assertEqual(validation.inputs, (archive.name, stage.name))
            self.assertEqual(
                validation.command.argv[3:],
                ("validate-module", artifact.architecture, artifact.module,
                 str(paths.cas(archive.uid, archive.name).output / f"{artifact.module}-{artifact.architecture}-dll.zip"),
                 str(paths.cas(stage.uid, stage.name).output)),
            )
            self.assertEqual(stage.command.argv[:3], (sys.executable, "-m", "core.package"))
            self.assertIn(str(artifact.binary), stage.command.argv)
            self.assertIn(str(artifact.symbols), symbols.command.argv)
            self.assertIn(str(paths.cas(stage.uid, stage.name).output), archive.command.argv)

        for architecture in ("arm64", "x64"):
            combined = graph.node(f"package-symbols-{architecture}")
            validation = graph.node(f"package-symbols-validate-{architecture}")
            self.assertEqual(
                combined.inputs,
                tuple(f"package-symbol-stage-{architecture}-{module}" for module in MODULES),
            )
            self.assertEqual(
                validation.inputs,
                (combined.name, *combined.inputs),
            )
        aggregate = graph.node("package-manifest")
        self.assertEqual(len(aggregate.inputs), 8)
        self.assertTrue(all(name.startswith("package-validate-") or name.startswith("package-symbols-validate-")
                            for name in aggregate.inputs))
        self.assertEqual(
            {Path(argument).name for argument in aggregate.command.argv[4:]},
            {
                *(f"{module}-{architecture}-dll.zip" for architecture in ("arm64", "x64") for module in MODULES),
                *(f"observer-modules-{architecture}-pdb.zip" for architecture in ("arm64", "x64")),
            },
        )
        self.assertTrue(all(node.pool == "package" for node in graph.nodes[len(upstream.nodes) :]))
        self.assertEqual(
            outputs,
            tuple(
                paths.cas(graph.node(node_name).uid, node_name).output / archive_name
                for architecture in ("arm64", "x64")
                for node_name, archive_name in (
                    *( (f"package-archive-{architecture}-{module}", f"{module}-{architecture}-dll.zip")
                       for module in MODULES ),
                    (f"package-symbols-{architecture}", f"observer-modules-{architecture}-pdb.zip"),
                )
            ),
        )

    def test_repository_metadata_invalidates_only_consuming_package_partition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, upstream, artifacts = self.fixture(Path(temporary), ("x64",))
            before = package_graph(repository, upstream, artifacts)
            (repository / "src/modules/renpy/observer_user.ini").write_text("changed\n", encoding="utf-8")
            after = package_graph(repository, upstream, artifacts)

        changed = {current.name for current in before.nodes if current.uid != after.node(current.name).uid}
        self.assertEqual(
            changed,
            {
                "package-stage-x64-renpy", "package-archive-x64-renpy",
                "package-validate-x64-renpy", "package-manifest",
            },
        )

    def test_package_smokes_run_independently_against_exact_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, original, artifacts = self.fixture(Path(temporary), ("x64",))
            test_producer = node("build-tests-x64-release")
            upstream = Graph(
                original.nodes + (test_producer,),
                original.targets + (test_producer.name,),
                original.pools,
            )
            paths = BuildPaths(repository)
            executable = paths.cas(test_producer.uid, test_producer.name).output / "tests.exe"
            graph = package_graph(
                repository,
                upstream,
                artifacts,
                smoke_tests=(PackageSmokeArtifact("x64", test_producer, executable),),
            )

        smokes = tuple(graph.node(f"package-smoke-x64-{module}") for module in MODULES)
        for smoke, module in zip(smokes, MODULES, strict=True):
            self.assertEqual(
                smoke.inputs,
                (f"package-validate-x64-{module}", test_producer.name),
            )
            self.assertEqual(smoke.command.argv[3:6], ("smoke", "x64", module))
            archive = graph.node(f"package-archive-x64-{module}")
            self.assertEqual(
                Path(smoke.command.argv[6]),
                paths.cas(archive.uid, archive.name).output / f"{module}-x64-dll.zip",
            )
            self.assertIn(str(executable), smoke.command.argv)
        manifest_inputs = set(graph.node("package-manifest").inputs)
        self.assertEqual(
            manifest_inputs,
            {smoke.name for smoke in smokes}
            | {f"package-validate-x64-{module}" for module in MODULES}
            | {"package-symbols-validate-x64"},
        )

    def test_invalid_artifacts_sets_and_pool_contracts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, upstream, artifacts = self.fixture(Path(temporary), ("x64",))

            for jobs in (0, True, "four"):
                with self.subTest(jobs=jobs), self.assertRaisesRegex(ValueError, "positive integer"):
                    package_graph(repository, upstream, artifacts, jobs=jobs)  # type: ignore[arg-type]
            with self.assertRaisesRegex(ValueError, "at least one"):
                package_graph(repository, upstream, ())
            with self.assertRaisesRegex(ValueError, "duplicate"):
                package_graph(repository, upstream, artifacts + (artifacts[0],))
            with self.assertRaisesRegex(ValueError, "identity"):
                package_graph(
                    repository,
                    upstream,
                    (
                        PackageArtifact(
                            "mips", "renpy", artifacts[0].producer,
                            artifacts[0].binary, artifacts[0].symbols,
                        ),
                    ),
                )
            with self.assertRaisesRegex(ValueError, "identity"):
                package_graph(
                    repository,
                    upstream,
                    (
                        PackageArtifact(
                            "x64", "bad module", artifacts[0].producer,
                            artifacts[0].binary, artifacts[0].symbols,
                        ),
                    ),
                )
            with self.assertRaisesRegex(ValueError, "complete module set"):
                package_graph(repository, upstream, artifacts[:-1])

            missing_gate = Graph(
                tuple(node for node in upstream.nodes if node.name != "audit-pe-x64-renpy"),
                upstream.targets,
                upstream.pools,
            )
            with self.assertRaisesRegex(ValueError, "audit gates"):
                package_graph(repository, missing_gate, artifacts)
            pe_gate = upstream.node("audit-pe-x64-renpy")
            proof = node("audit-proof-x64-renpy", inputs=(artifacts[0].producer.name,))
            indirect_gate = Node(pe_gate.name, pe_gate.uid, pe_gate.pool, pe_gate.command, (proof.name,))
            indirect = Graph(
                (*tuple(item for item in upstream.nodes if item != pe_gate), proof, indirect_gate),
                upstream.targets,
                upstream.pools,
            )
            self.assertEqual(package_graph(repository, indirect, artifacts).targets, ("package-manifest",))
            unrelated_gate = Node(
                pe_gate.name, pe_gate.uid, pe_gate.pool, pe_gate.command, (artifacts[1].producer.name,)
            )
            unrelated = Graph(
                (*tuple(item for item in upstream.nodes if item != pe_gate), unrelated_gate),
                upstream.targets,
                upstream.pools,
            )
            with self.assertRaisesRegex(ValueError, "do not consume producer"):
                package_graph(repository, unrelated, artifacts)

            impostor = node(artifacts[0].producer.name, "impostor")
            bad_producer = PackageArtifact("x64", "renpy", impostor, artifacts[0].binary, artifacts[0].symbols)
            with self.assertRaisesRegex(ValueError, "exact producer CAS"):
                package_graph(repository, upstream, (bad_producer,) + artifacts[1:])
            wrong_path = PackageArtifact(
                "x64", "renpy", artifacts[0].producer, artifacts[0].binary.parent / "wrong.so", artifacts[0].symbols
            )
            with self.assertRaisesRegex(ValueError, "exact producer CAS"):
                package_graph(repository, upstream, (wrong_path,) + artifacts[1:])

            matching = Graph(upstream.nodes, upstream.targets, {"build": 4, "package": 4})
            self.assertEqual(package_graph(repository, matching, artifacts).pools["package"], 4)
            conflicting = Graph(upstream.nodes, upstream.targets, {"build": 4, "package": 1})
            with self.assertRaisesRegex(ValueError, "conflicting pool"):
                package_graph(repository, conflicting, artifacts)

    def test_invalid_package_smoke_artifacts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, original, artifacts = self.fixture(Path(temporary), ("x64",))
            producer = node("build-tests-x64-release")
            upstream = Graph(original.nodes + (producer,), original.targets, original.pools)
            paths = BuildPaths(repository)
            valid = PackageSmokeArtifact(
                "x64", producer, paths.cas(producer.uid, producer.name).output / "tests.exe"
            )
            cases = (
                ((valid, valid), "duplicate"),
                ((PackageSmokeArtifact("x86", producer, valid.executable),), "exact test producer"),
                (
                    (PackageSmokeArtifact("x64", node(producer.name, "impostor"), valid.executable),),
                    "exact test producer",
                ),
                (
                    (PackageSmokeArtifact("x64", producer, valid.executable.with_name("wrong.exe")),),
                    "exact test producer",
                ),
            )
            for smokes, message in cases:
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    package_graph(repository, upstream, artifacts, smoke_tests=smokes)


class PackageActionTests(unittest.TestCase):
    def repository(self, root: Path) -> Path:
        return PackageGraphTests().repository(root)

    @staticmethod
    def invoke(output: Path, *arguments: str) -> int:
        output.mkdir()
        with mock.patch.dict(os.environ, {"OBSERVER_OUT_DIR": str(output)}, clear=False):
            return package_main(arguments)

    def test_module_stage_archive_and_aggregate_are_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            binary = root / "renpy.so"
            binary.write_bytes(b"module")
            stage = root / "stage"
            self.assertEqual(0, self.invoke(stage, "stage-module", "x64", "renpy", str(binary), str(repository)))

            payload = stage / "payload"
            expected = {
                "renpy.so", "observer_user.ini", "docs/license.txt",
                *(f"docs/thirdparty/{name}" for name in LICENSES["renpy"]),
            }
            self.assertEqual(
                expected,
                {
                    path.relative_to(payload).as_posix()
                    for path in payload.rglob("*")
                    if path.is_file()
                },
            )
            document = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                ("x64", "module", "renpy"),
                (document["architecture"], document["kind"], document["module"]),
            )
            self.assertEqual(sorted(expected), [entry["name"] for entry in document["entries"]])

            archive_one, archive_two = root / "archive-one", root / "archive-two"
            self.invoke(archive_one, "archive-module", "x64", "renpy", str(stage))
            self.invoke(archive_two, "archive-module", "x64", "renpy", str(stage))
            first = archive_one / "renpy-x64-dll.zip"
            second = archive_two / "renpy-x64-dll.zip"
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(sorted(expected), archive.namelist())
                self.assertTrue(all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist()))
                self.assertEqual(b"module", archive.read("renpy.so"))

            validation = root / "validation"
            self.assertEqual(
                0,
                self.invoke(validation, "validate-module", "x64", "renpy", str(first), str(stage)),
            )
            proof = json.loads((validation / "validation.json").read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), proof["sha256"])
            self.assertEqual(sorted(expected), [entry["name"] for entry in proof["entries"]])

            tampered = root / "tampered.zip"
            with zipfile.ZipFile(first) as source, zipfile.ZipFile(tampered, "w") as target:
                for name in source.namelist():
                    target.writestr(name, b"changed" if name == "renpy.so" else source.read(name))
            with self.assertRaisesRegex(PackageError, "exact stage manifests"):
                self.invoke(root / "tampered-validation", "validate-module", "x64", "renpy", str(tampered), str(stage))
            duplicate = root / "duplicate.zip"
            with self.assertWarns(UserWarning), zipfile.ZipFile(duplicate, "w") as archive:
                archive.writestr("renpy.so", b"first")
                archive.writestr("renpy.so", b"second")
            for index, invalid in enumerate((duplicate, root / "invalid.zip")):
                if not invalid.exists():
                    invalid.write_bytes(b"not a zip")
                with self.subTest(invalid=invalid), self.assertRaisesRegex(PackageError, "exact stage manifests"):
                    self.invoke(root / f"invalid-validation-{index}", "validate-module",
                                "x64", "renpy", str(invalid), str(stage))

            aggregate = root / "aggregate"
            self.invoke(aggregate, "aggregate", str(first))
            packages = json.loads((aggregate / "packages.json").read_text(encoding="utf-8"))
            self.assertEqual("renpy-x64-dll.zip", packages[0]["name"])
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), packages[0]["sha256"])

    def test_symbol_stages_fan_into_one_deterministic_architecture_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stages = []
            for module in MODULES:
                symbol = root / f"{module}.pdb"
                symbol.write_bytes(module.encode())
                stage = root / f"stage-{module}"
                self.invoke(stage, "stage-symbol", "arm64", module, str(symbol))
                stages.append(stage)
            output = root / "symbols"
            self.invoke(output, "archive-symbols", "arm64", *(str(stage) for stage in stages))
            with zipfile.ZipFile(output / "observer-modules-arm64-pdb.zip") as archive:
                self.assertEqual([f"{module}.pdb" for module in MODULES], archive.namelist())
                self.assertEqual(b"zanzarah", archive.read("zanzarah.pdb"))
            validation = root / "symbols-validation"
            archive = output / "observer-modules-arm64-pdb.zip"
            self.assertEqual(
                0,
                self.invoke(validation, "validate-symbols", "arm64", str(archive), *(str(stage) for stage in stages)),
            )
            self.assertEqual(
                hashlib.sha256(archive.read_bytes()).hexdigest(),
                json.loads((validation / "validation.json").read_text())["sha256"],
            )

    def test_smoke_extracts_and_runs_the_exact_archived_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "renpy-x64-dll.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("renpy.so", b"exact packaged module")
            tests = root / "tests.exe"
            tests.write_bytes(b"test runner")
            output = root / "smoke"

            with mock.patch("core.package.subprocess.run") as run:
                self.invoke(output, "smoke", "x64", "renpy", str(archive), str(tests))

            module = output / "renpy.so"
            self.assertEqual(module.read_bytes(), b"exact packaged module")
            run.assert_called_once()
            arguments = run.call_args.args[0]
            self.assertEqual(arguments[:2], [str(tests), "[package-smoke]"])
            self.assertEqual(run.call_args.kwargs["cwd"], output)
            self.assertEqual(run.call_args.kwargs["env"]["OBSERVER_PACKAGE_MODULE"], str(module))
            self.assertEqual(run.call_args.kwargs["env"]["OBSERVER_PACKAGE_FORMAT"], "renpy")

            missing = root / "missing.zip"
            with zipfile.ZipFile(missing, "w") as package:
                package.writestr("other.so", b"wrong module")
            with self.assertRaisesRegex(PackageError, "archive has no renpy.so"):
                self.invoke(root / "missing-smoke", "smoke", "x64", "renpy", str(missing), str(tests))

    def test_manifest_mismatch_duplicate_archive_and_missing_output_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            binary = root / "renpy.so"
            binary.write_bytes(b"module")
            stage = root / "stage"
            self.invoke(stage, "stage-module", "x86", "renpy", str(binary), str(repository))
            (stage / "payload/extra.obj").write_bytes(b"unexpected")
            with self.assertRaisesRegex(PackageError, "manifest does not match"):
                self.invoke(root / "bad-archive", "archive-module", "x86", "renpy", str(stage))

            archive = root / "same.zip"
            archive.write_bytes(b"same")
            with self.assertRaisesRegex(PackageError, "duplicate archive name"):
                self.invoke(root / "bad-aggregate", "aggregate", str(archive), str(archive))

        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(PackageError, "OBSERVER_OUT_DIR"):
            package_main(("aggregate", "missing.zip"))

    def test_module_validation_and_module_entrypoint_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            symbol = root / "renpy.pdb"
            symbol.write_bytes(b"symbols")
            stage = root / "stage"
            self.invoke(stage, "stage-symbol", "x64", "renpy", str(symbol))
            with self.assertRaisesRegex(PackageError, "expected module set"):
                self.invoke(root / "bad-symbols", "archive-symbols", "x64", str(stage))

            output = root / "entrypoint"
            output.mkdir()
            with (
                mock.patch.dict(os.environ, {"OBSERVER_OUT_DIR": str(output)}, clear=False),
                mock.patch.object(sys, "argv", ["package.py", "aggregate", str(symbol)]),
                self.assertWarnsRegex(RuntimeWarning, "core.package"),
                self.assertRaises(SystemExit) as raised,
            ):
                runpy.run_module("core.package", run_name="__main__")
            self.assertEqual(0, raised.exception.code)


if __name__ == "__main__":
    unittest.main(verbosity=2)

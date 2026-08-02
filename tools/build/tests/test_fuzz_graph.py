from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))

from core.paths import BuildPaths  # noqa: E402
from graphs.fuzz import (  # noqa: E402
    FuzzCorpusArtifact,
    fuzz_corpus_artifacts,
    fuzz_dependency_discovery_slice,
    fuzz_graph,
)


TARGETS = ("pickle", "renpy", "rpgmaker", "zanzarah")


@dataclass(frozen=True)
class FakeToolchain:
    msbuild: Path
    pwsh: Path
    vcpkg_root: Path
    llvm_dir: Path
    environment: tuple[tuple[str, str], ...]
    identity: dict[str, str]


class FuzzGraphTests(unittest.TestCase):
    def repository(self, root: Path) -> Path:
        project = """<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup><ClCompile Include="$(RepositoryRoot)src/fuzz/{target}.cpp" /></ItemGroup>
</Project>
"""
        files = {
            "build/ObserverProjectConfigurations.props": "<Project />\n",
            "build/ObserverConfiguration.props": "<Project />\n",
            "build/ObserverProject.props": "<Project />\n",
            "build/ObserverFuzz.props": "<Project />\n",
            "vcpkg.json": '{"dependencies":["zlib"]}\n',
            "build/vcpkg/triplets/observer-x64-windows-static-asan.cmake": (
                "set(VCPKG_LIBRARY_LINKAGE static)\n"
            ),
        }
        for target in TARGETS:
            files[f"build/projects/fuzz-{target}.vcxproj"] = project.format(target=target)
            files[f"src/fuzz/{target}.cpp"] = f'#include "{target}.h"\n'
            files[f"src/fuzz/{target}.h"] = "#pragma once\n"
            files[f"src/fuzz/corpus/{target}/seed.hex"] = "00 7f ff\n"
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    def toolchain(self, root: Path) -> FakeToolchain:
        tools = root / "fake tools"
        llvm = tools / "llvm"
        llvm_bin = llvm / "bin"
        runtime = llvm / "lib/clang/19/lib/windows"
        vcpkg = tools / "vcpkg"
        llvm_bin.mkdir(parents=True)
        runtime.mkdir(parents=True)
        vcpkg.mkdir()
        (vcpkg / "vcpkg.exe").touch()
        msbuild, pwsh = tools / "MSBuild.exe", tools / "pwsh.exe"
        msbuild.touch()
        pwsh.touch()
        for name in ("clang-cl.exe", "clang-scan-deps.exe"):
            (llvm_bin / name).touch()
        return FakeToolchain(
            msbuild,
            pwsh,
            vcpkg,
            llvm,
            (("PATH", str(tools)), ("VCPKG_ROOT", "stale")),
            {"msbuild": "17.14", "llvm": "19.1.5", "vcpkg": "2026.07"},
        )

    def graph(self, repository: Path, toolchain: FakeToolchain, **kwargs):
        jobs = kwargs.get("jobs", 2)
        targets = kwargs.get("targets", TARGETS)
        discovery = fuzz_dependency_discovery_slice(
            repository, toolchain, jobs=jobs, targets=targets
        )
        manifests = {}
        for target, name in zip(targets, discovery.targets, strict=True):
            source = repository / f"src/fuzz/{target}.cpp"
            header = repository / f"src/fuzz/{target}.h"
            manifests[name] = json.dumps(
                {
                    "Data": {
                        "Source": str(source.resolve()),
                        "Includes": [str(header.resolve())],
                    }
                }
            ).encode()
        return discovery, fuzz_graph(
            repository,
            toolchain,
            discovery=discovery,
            manifests=manifests,
            **kwargs,
        )

    def test_discovery_names_are_confined_to_the_fuzz_configuration_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            discovery = fuzz_dependency_discovery_slice(
                repository, self.toolchain(root), targets=("pickle",)
            )

        self.assertTrue(discovery.targets[0].startswith(
            "discover-dependencies-x64-fuzz-fuzz-pickle-"
        ))
        scanned = discovery.node(discovery.targets[0])
        self.assertEqual(len(scanned.inputs), 1)
        self.assertTrue(scanned.inputs[0].startswith(
            "capture-clang-command-x64-fuzz-fuzz-pickle-"
        ))

    def test_target_subset_is_exact_and_preserves_existing_node_uids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            _full_discovery, full = self.graph(
                repository, toolchain, run_nonce="same"
            )
            subset_discovery, subset = self.graph(
                repository, toolchain, run_nonce="same",
                targets=("pickle", "zanzarah"),
            )

        self.assertEqual(
            subset.targets, ("fuzz-x64-pickle", "fuzz-x64-zanzarah")
        )
        self.assertEqual(
            fuzz_corpus_artifacts(subset),
            tuple(
                FuzzCorpusArtifact(target, subset.node(f"run-fuzz-x64-{target}").uid)
                for target in ("pickle", "zanzarah")
            ),
        )
        self.assertTrue(all("renpy" not in node.name and "rpgmaker" not in node.name
                            for node in subset.nodes))
        self.assertEqual(len(subset_discovery.targets), 2)
        for current in subset.nodes:
            self.assertEqual(current.uid, full.node(current.name).uid, current.name)

    def test_targets_must_be_a_unique_nonempty_supported_subset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            for targets, message in (
                ((), "must not be empty"),
                (("pickle", "pickle"), "duplicate fuzz target: pickle"),
                (("unknown",), "unsupported fuzz target: unknown"),
            ):
                with self.subTest(targets=targets), self.assertRaisesRegex(ValueError, message):
                    fuzz_dependency_discovery_slice(
                        repository, toolchain, targets=targets
                    )

    def test_four_targets_are_independent_build_replay_and_bounded_run_branches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            discovery, graph = self.graph(
                repository, self.toolchain(root), run_nonce="test-run", seconds=37,
                jobs=3, fuzz_jobs=2,
            )

        self.assertEqual(graph.pools, {"build": 3, "fuzz": 2, "restore": 1, "slot": 3})
        self.assertEqual(graph.targets, tuple(f"fuzz-x64-{target}" for target in TARGETS))
        self.assertEqual(
            fuzz_corpus_artifacts(graph),
            tuple(
                FuzzCorpusArtifact(target, graph.node(f"run-fuzz-x64-{target}").uid)
                for target in TARGETS
            ),
        )
        self.assertEqual(len(graph.nodes), 1 + len(TARGETS) * 6)
        restore = graph.node("restore-vcpkg-asan-x64")
        self.assertEqual(restore.pool, "restore")
        self.assertIn("observer-x64-windows-static-asan", restore.command.stdin.decode())
        self.assertEqual(dict(restore.command.env)["VCPKG_ROOT"], str(root / "fake tools/vcpkg"))

        for target in TARGETS:
            build = graph.node(f"build-fuzz-x64-{target}")
            replay = graph.node(f"replay-fuzz-x64-{target}")
            run = graph.node(f"run-fuzz-x64-{target}")
            gate = graph.node(f"fuzz-x64-{target}")
            expected_discovery = next(
                name for name in discovery.targets if f"-fuzz-{target}-" in name
            )
            self.assertEqual(build.inputs, (expected_discovery,))
            self.assertEqual(replay.inputs, (build.name,))
            self.assertEqual(run.inputs, (replay.name,))
            self.assertEqual(gate.inputs, (run.name,))
            self.assertEqual(
                (build.pool, replay.pool, run.pool, gate.pool),
                ("build", "fuzz", "fuzz", "fuzz"),
            )
            self.assertFalse(any(other in " ".join(run.inputs) for other in TARGETS if other != target))

    def test_build_and_run_recipes_preserve_the_current_fuzzer_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            _discovery, graph = self.graph(
                repository, toolchain, run_nonce="test-run", seconds=37
            )

            build = graph.node("build-fuzz-x64-pickle")
            replay = graph.node("replay-fuzz-x64-pickle")
            run = graph.node("run-fuzz-x64-pickle")
            gate = graph.node("fuzz-x64-pickle")

        build_script = build.command.stdin.decode("utf-8")
        self.assertIn(str(repository / "build/projects/fuzz-pickle.vcxproj"), build_script)
        self.assertIn("'/t:Build'", build_script)
        self.assertIn("'/p:Configuration=Fuzz'", build_script)
        self.assertIn("'/p:Platform=x64'", build_script)
        self.assertRegex(build_script, r"'/p:VcpkgInstalledDir=.*\\'")
        self.assertIn("'/p:LLVMInstallDir=", build_script)
        self.assertIn("'/p:LLVMRuntimeDir=", build_script)
        self.assertIn("fuzz-pickle.exe", build_script)

        replay_script = replay.command.stdin.decode("utf-8")
        run_script = run.command.stdin.decode("utf-8")
        for script in (replay_script, run_script):
            self.assertIn("$buildDir 'corpus'", script)
            self.assertIn("FromHexString", script)
            self.assertIn("-max_len=262144", script)
            self.assertIn("-rss_limit_mb=1024", script)
            self.assertIn("-timeout=10", script)
        self.assertNotIn("-max_total_time", replay_script)
        self.assertIn("Get-ChildItem -LiteralPath $corpus", replay_script)
        self.assertIn("-max_total_time=37", run_script)
        self.assertIn("-use_value_profile=1", run_script)
        self.assertIn("$outDir 'corpus'", run_script)
        self.assertIn("status.txt", run_script)
        self.assertIn("$PSNativeCommandUseErrorActionPreference = $false", run_script)
        self.assertNotIn("Invoke-Checked $fuzzer", run_script)
        gate_script = gate.command.stdin.decode("utf-8")
        self.assertIn("status.txt", gate_script)
        self.assertIn("Fuzzer exited with code", gate_script)
        runtime = str(toolchain.llvm_dir / "lib/clang/19/lib/windows")
        self.assertTrue(dict(run.command.env)["PATH"].startswith(runtime))
        self.assertEqual(
            dict(run.command.env)["ASAN_OPTIONS"],
            "halt_on_error=1:alloc_dealloc_mismatch=1",
        )
        parser = (
            "$tokens=$null;$errors=$null;"
            "[Management.Automation.Language.Parser]::ParseInput("
            "[Console]::In.ReadToEnd(),[ref]$tokens,[ref]$errors)|Out-Null;"
            "if($errors.Count){$errors|ForEach-Object ToString;exit 1}"
        )
        parsed = subprocess.run(
            [shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-Command", parser],
            input="\n".join((build_script, replay_script, run_script, gate_script)),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, parsed.returncode, parsed.stderr or parsed.stdout)

    def test_one_seed_change_invalidates_only_its_replay_and_run_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            _discovery, before = self.graph(repository, toolchain, run_nonce="test-run")
            (repository / "src/fuzz/corpus/pickle/seed.hex").write_text("01\n", encoding="utf-8")
            _discovery, after = self.graph(repository, toolchain, run_nonce="test-run")

        changed = {
            "replay-fuzz-x64-pickle",
            "run-fuzz-x64-pickle",
            "fuzz-x64-pickle",
        }
        for node in before.nodes:
            comparison = after.node(node.name)
            if node.name in changed:
                self.assertNotEqual(node.uid, comparison.uid, node.name)
            else:
                self.assertEqual(node.uid, comparison.uid, node.name)

    def test_actual_msbuild_contract_rejects_every_non_x64_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            for architecture in ("x86", "arm64"):
                with self.subTest(architecture=architecture):
                    with self.assertRaisesRegex(ValueError, "x64-only"):
                        fuzz_graph(
                            repository, toolchain, run_nonce="test-run",
                            discovery=None, manifests={},
                            architectures=(architecture,),
                        )

            with self.assertRaisesRegex(ValueError, "run nonce"):
                fuzz_graph(repository, toolchain, run_nonce="", discovery=None, manifests={})
            with self.assertRaisesRegex(ValueError, "dependency discovery is required"):
                fuzz_graph(
                    repository, toolchain, run_nonce="test-run",
                    discovery=None, manifests={},
                )

    def test_build_requires_one_compiler_manifest_per_project_tu(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            discovery = fuzz_dependency_discovery_slice(repository, toolchain)

            with self.assertRaisesRegex(ValueError, "missing dependency manifest"):
                fuzz_graph(
                    repository, toolchain, discovery=discovery, manifests={},
                    run_nonce="test-run",
                )

    def test_run_nonce_invalidates_only_the_four_bounded_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            _discovery, before = self.graph(repository, toolchain, run_nonce="run-one")
            _discovery, after = self.graph(repository, toolchain, run_nonce="run-two")

        for node in before.nodes:
            comparison = after.node(node.name)
            if node.name.startswith(("run-fuzz-x64-", "fuzz-x64-")):
                self.assertNotEqual(node.uid, comparison.uid, node.name)
            else:
                self.assertEqual(node.uid, comparison.uid, node.name)

    def test_prior_published_corpus_seeds_and_signs_only_its_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            _discovery, before = self.graph(repository, toolchain, run_nonce="same")
            producer = before.node("run-fuzz-x64-pickle")
            cas = BuildPaths(repository).cas(producer.uid, producer.name)
            (cas.output / "corpus").mkdir(parents=True)
            (cas.output / "corpus/evolved").write_bytes(b"first")
            cas.log.write_text("green\n", encoding="utf-8")
            cas.touch.touch()
            artifact = FuzzCorpusArtifact("pickle", producer.uid)

            _discovery, seeded = self.graph(
                repository, toolchain, run_nonce="same", prior_corpora=(artifact,)
            )
            (cas.output / "corpus/evolved").write_bytes(b"second")
            _discovery, content_changed = self.graph(
                repository, toolchain, run_nonce="same", prior_corpora=(artifact,)
            )

        changed = {"run-fuzz-x64-pickle", "fuzz-x64-pickle"}
        for node in before.nodes:
            comparison = seeded.node(node.name)
            if node.name in changed:
                self.assertNotEqual(node.uid, comparison.uid, node.name)
            else:
                self.assertEqual(node.uid, comparison.uid, node.name)
        self.assertNotEqual(
            seeded.node("run-fuzz-x64-pickle").uid,
            content_changed.node("run-fuzz-x64-pickle").uid,
        )
        script = seeded.node("run-fuzz-x64-pickle").command.stdin.decode()
        self.assertIn(str(cas.output / "corpus"), script)
        self.assertIn("Copy-Item", script)

    def test_prior_corpus_must_be_unique_and_complete_canonical_cas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            missing = FuzzCorpusArtifact("pickle", "0" * 32)
            with self.assertRaisesRegex(FileNotFoundError, "not published"):
                self.graph(
                    repository, toolchain, run_nonce="run", prior_corpora=(missing,)
                )
            with self.assertRaisesRegex(ValueError, "duplicate prior corpus"):
                self.graph(
                    repository,
                    toolchain,
                    run_nonce="run",
                    prior_corpora=(missing, missing),
                )
            with self.assertRaisesRegex(ValueError, "unsupported fuzz corpus target"):
                FuzzCorpusArtifact("unknown", "0" * 32)
            for invalid_uid in (42, "invalid"):
                with self.subTest(invalid_uid=invalid_uid), self.assertRaisesRegex(
                    ValueError, "canonical MD5"
                ):
                    FuzzCorpusArtifact("pickle", invalid_uid)

    def test_prior_corpus_rejects_empty_or_nonfile_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            toolchain = self.toolchain(root)
            paths = BuildPaths(repository)
            empty = paths.cas("1" * 32, "run-fuzz-x64-pickle")
            (empty.output / "corpus").mkdir(parents=True)
            empty.log.touch()
            empty.touch.touch()
            with self.assertRaisesRegex(ValueError, "corpus is empty"):
                self.graph(
                    repository,
                    toolchain,
                    run_nonce="run",
                    prior_corpora=(FuzzCorpusArtifact("pickle", "1" * 32),),
                )

            nonfile = paths.cas("2" * 32, "run-fuzz-x64-pickle")
            (nonfile.output / "corpus/directory").mkdir(parents=True)
            nonfile.log.touch()
            nonfile.touch.touch()
            with self.assertRaisesRegex(ValueError, "contains a non-file"):
                self.graph(
                    repository,
                    toolchain,
                    run_nonce="run",
                    prior_corpora=(FuzzCorpusArtifact("pickle", "2" * 32),),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)

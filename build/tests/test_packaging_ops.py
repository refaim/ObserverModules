from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import site
import shutil
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock
import zipfile


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))
VENV_SITE = BUILD_ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SITE.is_dir():
    site.addsitedir(str(VENV_SITE))

import core.package as core_package  # noqa: E402
import packaging_ops  # noqa: E402
from core import binary_audit  # noqa: E402
from core.binary_audit import AuditError  # noqa: E402
from core.host import DeferredGate  # noqa: E402
from native import NativeTools  # noqa: E402
from packaging_ops import (  # noqa: E402
    PackagingError,
    PackagingTools,
    PackageResult,
    audit_binaries,
    module_content_inputs,
    package,
)


MODULES = ("renpy", "rpgmaker", "zanzarah")
VERSIONS = {"renpy": "3.1.0", "rpgmaker": "1.1.0", "zanzarah": "2.1.0"}
LICENSES = {
    "renpy": ("Observer.txt", "rpatool.txt", "serde-pickle.txt", "zlib.txt"),
    "rpgmaker": ("Observer.txt", "rgssad.txt"),
    "zanzarah": ("Observer.txt", "zanzapak.txt"),
}
CHANGELOGS = ("ChangeLog.en.txt", "ChangeLog.ru.txt")


def changelog(module: str, version: str, language: str) -> str:
    header = f"{'Version' if language == 'en' else '\u0412\u0435\u0440\u0441\u0438\u044f'} {version}"
    return f"{header}\n{'-' * len(header)}\n * {module} {language} release.\n"


def write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def touch(root: Path, relative: str, content: bytes = b"binary") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    cwd: Path | None
    env: dict[str, str] | None
    captured: bool


def python(*arguments: str) -> tuple[str, ...]:
    return (sys.executable, "-m", *arguments)


def symlink(link: Path, target: Path) -> bool:
    """Create a real file symlink, reporting whether the host allowed it."""

    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        return False
    return True


def _writers(calls: list[Call]) -> list[Call]:
    """Return the run-binskim writer invocations from a recorded call list."""

    return [call for call in calls if len(call.argv) > 3 and call.argv[3] == "run-binskim"]


class ScriptedRunner:
    """A subprocess seam that answers dumpbin/MSBuild and runs core.package in process."""

    def __init__(self, tools: PackagingTools, inventory: dict[str, dict[str, tuple[str, ...]]]) -> None:
        self.tools = tools
        self.inventory = inventory
        self.calls: list[Call] = []

    def _machine(self, binary: str) -> bytes:
        architectures = {
            "x86": b" 14C machine (x86)\n",
            "x64": b" 8664 machine (x64)\n",
            "arm64": b" AA64 machine (ARM64)\n",
        }
        architecture = next(part for part in Path(binary).parts if part in architectures)
        return architectures[architecture]

    def __call__(self, argv, *, cwd=None, env=None, stdout=None) -> None:
        argv = tuple(str(item) for item in argv)
        self.calls.append(Call(argv, cwd, None if env is None else dict(env), stdout is not None))
        head = argv[0]
        if head == str(self.tools.dumpbin):
            mode = argv[1].lstrip("/")
            if stdout is not None:
                if mode == "headers":
                    stdout.write(self._machine(argv[2]))
                elif mode == "dependents":
                    stdout.write(b" KERNEL32.dll\n")
                elif Path(argv[2]).name == "leak-probe.exe":
                    # The real leak probe is a self-contained EXE with an empty export table.
                    stdout.write(b"  Summary\n")
                else:
                    stdout.write(b" 1 0 0001 LoadSubModule\n 2 1 0002 UnloadSubModule\n")
            return
        if head == str(self.tools.msbuild):
            document = {
                "Items": {
                    key: [{"Identity": value} for value in values]
                    for key, values in self.inventory[Path(argv[1]).stem].items()
                }
            }
            if stdout is not None:
                stdout.write(json.dumps(document).encode("utf-8"))
            return
        if argv[:3] == (sys.executable, "-m", "core.binary_audit"):
            if argv[3] == "pe":
                binary_audit.main(argv[3:])
            return
        if argv[:3] == (sys.executable, "-m", "core.package"):
            if argv[3] == "smoke":
                return
            with mock.patch.dict(os.environ, env or {}):
                core_package.main(argv[3:])
            return


class MutatingRunner(ScriptedRunner):
    """A runner that swaps one repository binary the moment its audit begins."""

    def __init__(self, tools: PackagingTools, inventory, *, target: str, swap) -> None:
        super().__init__(tools, inventory)
        self.target = target
        self.swap = swap
        self.swapped = False

    def __call__(self, argv, *, cwd=None, env=None, stdout=None) -> None:
        argv = tuple(str(item) for item in argv)
        if argv[0] == str(self.tools.dumpbin) and argv[2].endswith(self.target) and not self.swapped:
            self.swapped = True
            self.swap()
        super().__call__(argv, cwd=cwd, env=env, stdout=stdout)


class Fixture:
    def __init__(self, root: Path, *, architectures: tuple[str, ...] = ("x64",)) -> None:
        self.root = root
        self.repository = root / "repo"
        self.output = root / "output"
        self.output.mkdir(parents=True, exist_ok=True)
        self._repository()
        self._artifacts(architectures)

    def tool(self, relative: str) -> Path:
        path = self.root / "tools" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"tool")
        return path

    def tools(self) -> PackagingTools:
        return PackagingTools(
            msbuild=self.tool("MSBuild.exe"),
            vcpkg_root=self.tool("vcpkg/vcpkg.exe").parent,
            dumpbin=self.tool("dumpbin.exe"),
            binskim=self.tool("BinSkim.exe"),
        )

    def inventory(self) -> dict[str, dict[str, tuple[str, ...]]]:
        return {
            module: {
                "ClCompile": (str(self.repository / f"src/modules/{module}/{module}.cpp"),),
                "ClInclude": (str(self.repository / "src/modules/extractor.h"),),
            }
            for module in MODULES
        }

    def _repository(self) -> None:
        for module, licenses in LICENSES.items():
            write(self.repository, f"src/modules/{module}/observer_user.ini", f"[{module}]\n")
            write(self.repository, f"src/modules/{module}/{module}.cpp", f"// {module}\n")
            write(self.repository, f"src/modules/{module}/{module}.def", "EXPORTS\n")
            write(self.repository, f"src/modules/{module}/{module}_private.h", "#pragma once\n")
            write(self.repository, f"src/modules/{module}/VERSION", f"{VERSIONS[module]}\n")
            for name, language in zip(CHANGELOGS, ("en", "ru"), strict=True):
                write(
                    self.repository,
                    f"src/modules/{module}/{name}",
                    changelog(module, VERSIONS[module], language),
                )
            write(self.repository, f"build/projects/{module}.vcxproj", "<Project />\n")
            for name in licenses:
                write(self.repository, f"licenses/{name}", f"license:{name}\n")
        write(self.repository, "src/api.h", "#pragma once\n")
        write(self.repository, "src/common/shared.h", "#pragma once\n")
        write(self.repository, "src/core/bounded.h", "#pragma once\n")
        write(self.repository, "src/modules/extractor.h", "#pragma once\n")
        write(self.repository, "src/modules/version.rc", "VS_VERSION_INFO VERSIONINFO\n")
        for name in (
            "build/ObserverProjectConfigurations.props",
            "build/ObserverConfiguration.props",
            "build/ObserverProject.props",
            "build/ObserverModuleVersion.props",
        ):
            write(self.repository, name, "<Project />\n")
        write(self.repository, "vcpkg.json", '{"dependencies":["zlib"]}\n')
        write(
            self.repository,
            "build/vcpkg/triplets/observer-x64-windows-static.cmake",
            "static\n",
        )
        write(self.repository, "LICENSE.txt", "project license\n")

    def _artifacts(self, architectures: tuple[str, ...]) -> None:
        for architecture in architectures:
            for module in MODULES:
                touch(
                    self.repository,
                    f"out/native/bin/{architecture}/Release/{module}.so",
                    f"{module}-{architecture}-release".encode(),
                )
                touch(
                    self.repository,
                    f"out/native/bin/{architecture}/Release/{module}.pdb",
                    f"{module}-{architecture}-pdb".encode(),
                )
            touch(self.repository, f"out/native/bin/{architecture}/Release/tests.exe", b"tests")


class SelectionTests(unittest.TestCase):
    def test_module_selection_defaults_to_all_and_reorders(self) -> None:
        self.assertEqual(packaging_ops._modules(None), MODULES)
        self.assertEqual(packaging_ops._modules("all"), MODULES)
        self.assertEqual(packaging_ops._modules("renpy"), ("renpy",))
        self.assertEqual(packaging_ops._modules(("zanzarah", "renpy")), ("renpy", "zanzarah"))

    def test_module_selection_rejects_empty_unknown_and_duplicate(self) -> None:
        for selection, message in (
            ((), "must not be empty"),
            (("pickle",), "unknown module"),
            (("renpy", "renpy"), "must not repeat"),
        ):
            with self.subTest(selection=selection), self.assertRaisesRegex(ValueError, message):
                packaging_ops._modules(selection)

    def test_architecture_selection_is_unique_and_supported(self) -> None:
        self.assertEqual(packaging_ops._architectures(("arm64", "x86")), ("x86", "arm64"))
        for architectures, message in (
            ((), "at least one"),
            (("riscv64",), "unsupported architecture"),
            (("x64", "x64"), "must not repeat"),
        ):
            with self.subTest(architectures=architectures), self.assertRaisesRegex(ValueError, message):
                packaging_ops._architectures(architectures)

    def test_smoke_selection_must_be_a_selected_subset(self) -> None:
        self.assertEqual(packaging_ops._smoke(("x64", "arm64"), None), ("x64", "arm64"))
        self.assertEqual(packaging_ops._smoke(("x64", "arm64"), ("arm64",)), ("arm64",))
        for smokes, message in ((("x86",), "not selected"), (("x64", "x64"), "must not repeat")):
            with self.subTest(smokes=smokes), self.assertRaisesRegex(ValueError, message):
                packaging_ops._smoke(("x64", "arm64"), smokes)


class ConfinementTests(unittest.TestCase):
    def test_is_reparse_reports_missing_links_and_windows_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertFalse(packaging_ops._is_reparse(root / "missing"))
            link = types.SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            with mock.patch("packaging_ops.os.lstat", return_value=link):
                self.assertTrue(packaging_ops._is_reparse(root))
            junction = types.SimpleNamespace(
                st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
            with mock.patch("packaging_ops.os.lstat", return_value=junction):
                self.assertTrue(packaging_ops._is_reparse(root))
            plain = types.SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0)
            with mock.patch("packaging_ops.os.lstat", return_value=plain):
                self.assertFalse(packaging_ops._is_reparse(root))

    def test_confined_rejects_escaping_and_reparse_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            self.assertEqual(packaging_ops._confined(output / "deep", output), output / "deep")
            with self.assertRaisesRegex(PackagingError, "escapes"):
                packaging_ops._confined(root / "other", output)
            with mock.patch("packaging_ops._is_reparse", side_effect=lambda path: path == output):
                with self.assertRaisesRegex(PackagingError, "reparse point"):
                    packaging_ops._confined(output / "deep", output)

    def test_reset_directory_rebuilds_but_refuses_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            stale = output / "staging" / "inner"
            stale.mkdir(parents=True)
            (stale / "leftover.txt").write_text("stale", encoding="utf-8")
            target = packaging_ops._reset_directory(output / "staging", output)
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])
            blocker = output / "file"
            blocker.write_text("not a directory", encoding="utf-8")
            with self.assertRaisesRegex(PackagingError, "not a directory"):
                packaging_ops._reset_directory(blocker, output)

    def test_output_root_requires_an_existing_plain_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing"
            with self.assertRaisesRegex(PackagingError, "must be an existing directory"):
                packaging_ops._output_root(missing)
            output = root / "output"
            output.mkdir()
            self.assertEqual(packaging_ops._output_root(output), output)
            with mock.patch("packaging_ops._is_reparse", side_effect=lambda path: path == output):
                with self.assertRaisesRegex(PackagingError, "escapes|reparse point"):
                    packaging_ops._output_root(output)

    def test_leaf_refuses_a_reparse_or_directory_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(packaging_ops._leaf(root / "fresh.txt", root), root / "fresh.txt")
            directory = root / "taken"
            directory.mkdir()
            with self.assertRaisesRegex(PackagingError, "leaf is a directory"):
                packaging_ops._leaf(directory, root)
            with mock.patch("packaging_ops._is_reparse", side_effect=lambda path: path.name == "link.txt"):
                with self.assertRaisesRegex(PackagingError, "reparse point"):
                    packaging_ops._leaf(root / "link.txt", root)

    def test_reset_directory_refuses_to_remove_through_a_reparse_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            sentinel = output / "staging" / "inner" / "leftover.txt"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("stale", encoding="utf-8")
            with mock.patch(
                "packaging_ops._is_reparse", side_effect=lambda path: path.name == "leftover.txt"
            ):
                with self.assertRaisesRegex(PackagingError, "reparse point"):
                    packaging_ops._reset_directory(output / "staging", output)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "stale")

    def test_publish_replaces_a_previous_tree_and_refuses_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending = root / "staging" / "publish"
            (pending / "x64").mkdir(parents=True)
            (pending / "x64" / "renpy.zip").write_bytes(b"new")
            previous = root / "packages"
            previous.mkdir()
            (previous / "stale.txt").write_bytes(b"old")
            published = packaging_ops._publish(pending, previous, root)
            self.assertEqual(published, previous)
            self.assertEqual([item.name for item in published.iterdir()], ["x64"])
            blocker = root / "blocker"
            blocker.write_text("file", encoding="utf-8")
            with self.assertRaisesRegex(PackagingError, "not a directory"):
                packaging_ops._publish(pending, blocker, root)

    def test_publish_restores_the_previous_tree_when_replacement_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending = root / "staging" / "publish"
            (pending / "x64").mkdir(parents=True)
            (pending / "x64" / "renpy.zip").write_bytes(b"new")
            previous = root / "packages"
            (previous / "x64").mkdir(parents=True)
            (previous / "x64" / "renpy.zip").write_bytes(b"old-archive")
            (previous / "packages.json").write_bytes(b"old-manifest")
            real_replace = os.replace

            def flaky(source: str, target: str) -> None:
                if Path(source) == pending and Path(target) == previous:
                    raise PermissionError("sharing violation")
                real_replace(source, target)

            with mock.patch("packaging_ops.os.replace", side_effect=flaky):
                with self.assertRaises(PermissionError):
                    packaging_ops._publish(pending, previous, root)
            # The old manifest and archive bytes survive the failed replacement.
            self.assertEqual((previous / "packages.json").read_bytes(), b"old-manifest")
            self.assertEqual((previous / "x64" / "renpy.zip").read_bytes(), b"old-archive")
            # The freshly staged tree is untouched so the caller can retry.
            self.assertTrue((pending / "x64" / "renpy.zip").is_file())

    def test_publish_clears_a_stale_backup_before_swapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending = root / "staging" / "publish"
            (pending / "x64").mkdir(parents=True)
            (pending / "x64" / "renpy.zip").write_bytes(b"new")
            previous = root / "packages"
            previous.mkdir()
            (previous / "stale.txt").write_bytes(b"old")
            backup = root / "packages.previous"
            backup.mkdir()
            (backup / "leftover.txt").write_bytes(b"junk")
            published = packaging_ops._publish(pending, previous, root)
            self.assertEqual(published, previous)
            self.assertEqual([item.name for item in published.iterdir()], ["x64"])
            self.assertFalse(backup.exists())

    def test_publish_propagates_a_failure_without_a_previous_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending = root / "staging" / "publish"
            (pending / "x64").mkdir(parents=True)
            (pending / "x64" / "renpy.zip").write_bytes(b"new")
            target = root / "packages"
            real_replace = os.replace

            def flaky(source: str, destination: str) -> None:
                if Path(source) == pending:
                    raise PermissionError("sharing violation")
                real_replace(source, destination)

            with mock.patch("packaging_ops.os.replace", side_effect=flaky):
                with self.assertRaises(PermissionError):
                    packaging_ops._publish(pending, target, root)
            self.assertFalse(target.exists())
            self.assertTrue((pending / "x64" / "renpy.zip").is_file())

    def test_audit_refuses_a_real_leaf_symlink_without_touching_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            outside = Path(temporary) / "outside.txt"
            outside.write_text("sentinel", encoding="utf-8")
            directory = fixture.output / "audit" / "x64" / "renpy"
            directory.mkdir(parents=True)
            if not symlink(directory / "headers.txt", outside):
                self.skipTest("host does not support symlink creation")
            tools = fixture.tools()
            runner = ScriptedRunner(tools, fixture.inventory())
            with self.assertRaisesRegex(PackagingError, "reparse point"):
                audit_binaries(
                    fixture.repository, ("x64",), fixture.output, modules=("renpy",),
                    tools=tools, runner=runner,
                )
            self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel")
            self.assertEqual(runner.calls, [])

    def test_audit_refuses_a_mocked_reparse_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            directory = fixture.output / "audit" / "x64" / "renpy"
            directory.mkdir(parents=True)
            (directory / "headers.txt").write_text("plain", encoding="utf-8")
            tools = fixture.tools()
            runner = ScriptedRunner(tools, fixture.inventory())
            with mock.patch(
                "packaging_ops._is_reparse", side_effect=lambda path: path.name == "headers.txt"
            ):
                with self.assertRaisesRegex(PackagingError, "reparse point"):
                    audit_binaries(
                        fixture.repository, ("x64",), fixture.output, modules=("renpy",),
                        tools=tools, runner=runner,
                    )
            self.assertEqual(runner.calls, [])
            self.assertEqual((directory / "headers.txt").read_text(encoding="utf-8"), "plain")

    def test_audit_rejects_a_linked_binskim_report_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            outside = Path(temporary) / "outside.sarif"
            outside.write_text("sentinel", encoding="utf-8")
            directory = fixture.output / "audit" / "x64" / "renpy"
            directory.mkdir(parents=True)
            if not symlink(directory / "binskim.sarif", outside):
                self.skipTest("host does not support symlink creation")
            tools = fixture.tools()
            runner = ScriptedRunner(tools, fixture.inventory())
            with self.assertRaisesRegex(PackagingError, "reparse point"):
                audit_binaries(
                    fixture.repository, ("x64",), fixture.output, modules=("renpy",),
                    tools=tools, runner=runner,
                )
            self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel")
            self.assertEqual(_writers(runner.calls), [])

    def test_audit_rejects_a_directory_binskim_report_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            directory = fixture.output / "audit" / "x64" / "renpy"
            (directory / "binskim.sarif").mkdir(parents=True)
            tools = fixture.tools()
            runner = ScriptedRunner(tools, fixture.inventory())
            with self.assertRaisesRegex(PackagingError, "leaf is a directory"):
                audit_binaries(
                    fixture.repository, ("x64",), fixture.output, modules=("renpy",),
                    tools=tools, runner=runner,
                )
            self.assertEqual(_writers(runner.calls), [])

    def test_audit_removes_a_stale_binskim_report_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            directory = fixture.output / "audit" / "x64" / "renpy"
            directory.mkdir(parents=True)
            stale = directory / "binskim.sarif"
            stale.write_text("stale", encoding="utf-8")
            tools = fixture.tools()
            observed: dict[str, bool] = {}

            class Observing(ScriptedRunner):
                def __call__(self, argv, *, cwd=None, env=None, stdout=None) -> None:
                    if len(argv) > 3 and argv[3] == "run-binskim":
                        observed["present"] = stale.exists()
                    super().__call__(argv, cwd=cwd, env=env, stdout=stdout)

            runner = Observing(tools, fixture.inventory())
            audit_binaries(
                fixture.repository, ("x64",), fixture.output, modules=("renpy",),
                tools=tools, runner=runner,
            )
            self.assertFalse(observed.get("present", True))
            self.assertFalse(stale.exists())


class InventoryTests(unittest.TestCase):
    def test_item_paths_reads_identities_relative_to_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            source = touch(repository, "src/renpy.cpp")
            header = touch(repository, "src/extractor.h")
            outside = touch(repository.parent, "outside.h")
            text = json.dumps({
                "Items": {
                    "ClCompile": [{"Identity": str(source)}, str(header)],
                    "ClInclude": [{"Identity": str(outside)}],
                }
            })
            self.assertEqual(
                packaging_ops._item_paths(text, repository),
                ("src/extractor.h", "src/renpy.cpp"),
            )

    def test_item_paths_fails_closed_on_malformed_or_missing_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            touch(repository, "src/renpy.cpp")
            cases = (
                ("no json here", "no item inventory"),
                ("{not json", "is not JSON"),
                (json.dumps({"Properties": {}}), "has no Items"),
                (json.dumps({"Items": {"ClCompile": {}}}), "is not a list"),
                (json.dumps({"Items": {"ClCompile": [{}]}}), "non-string identity"),
                (json.dumps({"Items": {"ClCompile": [{"Identity": str(repository / 'gone.cpp')}]}}),
                 "is missing"),
                (json.dumps({"Items": {"ClCompile": []}}), "no project items"),
            )
            for text, message in cases:
                with self.subTest(message=message), self.assertRaisesRegex(PackagingError, message):
                    packaging_ops._item_paths(text, repository)

    def test_project_inventory_queries_msbuild_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            tools = fixture.tools()
            runner = ScriptedRunner(tools, fixture.inventory())
            scratch = fixture.output / "staging"
            scratch.mkdir()
            items = packaging_ops._project_inventory(fixture.repository, "renpy", tools, runner, scratch)
            self.assertEqual(
                items,
                (
                    "src/modules/extractor.h",
                    "src/modules/renpy/renpy.cpp",
                ),
            )
            argv = runner.calls[0].argv
            self.assertEqual(argv[0], str(tools.msbuild))
            self.assertIn(f"-p:VcpkgRoot={tools.vcpkg_root}", argv)

            with self.assertRaisesRegex(PackagingError, "project is missing"):
                packaging_ops._project_inventory(fixture.repository, "missing", tools, runner, scratch)


class ContentInputTests(unittest.TestCase):
    def test_content_inputs_cover_selected_tree_shared_and_conservative_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            inputs = module_content_inputs(
                fixture.repository, "renpy",
                ("src/modules/renpy/renpy.cpp", "src/modules/extractor.h"),
            )
            self.assertIn("src/modules/renpy/renpy.cpp", inputs)
            self.assertIn("src/modules/renpy/observer_user.ini", inputs)
            self.assertIn("src/modules/renpy/renpy_private.h", inputs)
            for name in CHANGELOGS:
                self.assertIn(f"src/modules/renpy/{name}", inputs)
            self.assertIn("src/api.h", inputs)
            self.assertIn("src/common/shared.h", inputs)
            self.assertIn("src/core/bounded.h", inputs)
            self.assertIn("src/modules/extractor.h", inputs)
            self.assertIn("LICENSE.txt", inputs)
            self.assertIn("licenses/rpatool.txt", inputs)
            self.assertNotIn("src/modules/rpgmaker/rpgmaker.cpp", inputs)
            self.assertNotIn("src/modules/renpy/VERSION", inputs)

            # A repository without the optional shared tree must still resolve every other input.
            shutil.rmtree(fixture.repository / "src" / "common")
            without_common = module_content_inputs(
                fixture.repository, "renpy", ("src/modules/renpy/renpy.cpp",)
            )
            self.assertNotIn("src/common/shared.h", without_common)
            self.assertIn("src/core/bounded.h", without_common)

    def test_content_inputs_include_the_project_and_shared_build_props(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            inputs = module_content_inputs(
                fixture.repository, "renpy", ("src/modules/renpy/renpy.cpp",)
            )
        self.assertIn("build/projects/renpy.vcxproj", inputs)
        for name in (
            "build/ObserverProjectConfigurations.props",
            "build/ObserverConfiguration.props",
            "build/ObserverProject.props",
            "build/ObserverModuleVersion.props",
        ):
            with self.subTest(name=name):
                self.assertIn(name, inputs)

    def test_shared_props_and_project_files_move_only_the_selected_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))

            def identities() -> dict[str, str]:
                return {
                    module: packaging_ops.content_identity(
                        fixture.repository, module_content_inputs(fixture.repository, module, ())
                    )
                    for module in MODULES
                }

            baseline = identities()
            (fixture.repository / "build/ObserverProject.props").write_text(
                "changed\n", encoding="utf-8"
            )
            shared_props = identities()
            (fixture.repository / "build/projects/renpy.vcxproj").write_text(
                "changed\n", encoding="utf-8"
            )
            project = identities()
            (fixture.repository / "src/modules/rpgmaker/rpgmaker_private.h").write_text(
                "#pragma once\n// moved\n", encoding="utf-8"
            )
            sibling = identities()
            (fixture.repository / "src/modules/rpgmaker/VERSION").write_text(
                "9.9.9\n", encoding="utf-8"
            )
            version = identities()
            (fixture.repository / "src/modules/renpy/ChangeLog.en.txt").write_text(
                changelog("renpy", "3.1.0", "en").replace("release.", "renamed note."),
                encoding="utf-8",
            )
            changelog_moved = identities()

        self.assertEqual(set(baseline), set(MODULES))
        # A shared project definition changes every module's compiled bytes.
        self.assertTrue(all(baseline[module] != shared_props[module] for module in MODULES))
        # Only the selected project definition moves that module's identity.
        self.assertNotEqual(shared_props["renpy"], project["renpy"])
        self.assertEqual(shared_props["rpgmaker"], project["rpgmaker"])
        # A sibling module's production tree moves only that module.
        self.assertNotEqual(project["rpgmaker"], sibling["rpgmaker"])
        self.assertEqual(project["renpy"], sibling["renpy"])
        # VERSION is never part of the content identity.
        self.assertEqual(sibling, version)
        # The module's own ChangeLog moves only that module's identity.
        self.assertNotEqual(version["renpy"], changelog_moved["renpy"])
        self.assertEqual(version["rpgmaker"], changelog_moved["rpgmaker"])
        self.assertEqual(version["zanzarah"], changelog_moved["zanzarah"])


class AuditTests(unittest.TestCase):
    def test_audit_runs_every_release_gate_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            tools = fixture.tools()
            runner = ScriptedRunner(tools, fixture.inventory())
            evidence = audit_binaries(
                fixture.repository, ("x64",), fixture.output, modules=("renpy",), tools=tools,
                runner=runner,
            )
            binary = fixture.repository / "out/native/bin/x64/Release/renpy.so"
            directory = fixture.output / "audit" / "x64" / "renpy"
            headers, dependents, exports = (
                directory / "headers.txt", directory / "dependents.txt", directory / "exports.txt"
            )
            self.assertEqual(
                evidence,
                (headers, dependents, exports, directory / "binskim.sarif"),
            )
            self.assertEqual(
                [call.argv for call in runner.calls],
                [
                    (str(tools.dumpbin), "/headers", str(binary)),
                    (str(tools.dumpbin), "/dependents", str(binary)),
                    (str(tools.dumpbin), "/exports", str(binary)),
                    (*python("core.binary_audit"), "pe", "x64",
                     str(headers), str(dependents), str(exports)),
                    (*python("core.binary_audit"), "run-binskim", str(tools.binskim), str(binary)),
                    (*python("core.binary_audit"), "binskim", str(directory / "binskim.sarif")),
                    (*python("core.module_version"), "verify", VERSIONS["renpy"], str(binary)),
                ],
            )
            self.assertTrue(runner.calls[0].captured)
            self.assertEqual(runner.calls[3].env, None)
            self.assertEqual(runner.calls[4].env, {"OBSERVER_OUT_DIR": str(directory)})
            self.assertEqual(runner.calls[4].cwd, packaging_ops.BUILD_ROOT)
            self.assertEqual(headers.read_text(encoding="utf-8"), " 8664 machine (x64)\n")

    def test_audit_includes_the_x64_leak_probe_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary), architectures=("x64", "arm64"))
            touch(fixture.repository, "out/native/bin/x64/Release/leak-probe.exe")
            tools = fixture.tools()
            runner = ScriptedRunner(tools, fixture.inventory())
            audit_binaries(
                fixture.repository, ("x64", "arm64"), fixture.output,
                modules=("renpy",), include_leak_probe=True, tools=tools, runner=runner,
            )
            probe_calls = [call for call in runner.calls if call.argv[0] == str(tools.dumpbin)]
            probe = [call for call in probe_calls if "leak-probe.exe" in call.argv[2]]
            self.assertEqual(len(probe), 3)
            self.assertTrue(all("x64" in call.argv[2] for call in probe))
            self.assertFalse(any("arm64" in call.argv[2] for call in probe))

    def test_audit_binds_the_executable_kind_to_the_leak_probe_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            touch(fixture.repository, "out/native/bin/x64/Release/leak-probe.exe")
            tools = fixture.tools()
            runner = ScriptedRunner(tools, fixture.inventory())
            audit_binaries(
                fixture.repository, ("x64",), fixture.output,
                modules=("renpy",), include_leak_probe=True, tools=tools, runner=runner,
            )
            pe_calls = [
                call.argv for call in runner.calls
                if call.argv[:3] == python("core.binary_audit") and call.argv[3] == "pe"
            ]
            probe = next(argv for argv in pe_calls if "leak-probe" in " ".join(argv))
            module = next(argv for argv in pe_calls if "leak-probe" not in " ".join(argv))
            self.assertEqual(
                probe[:6], (*python("core.binary_audit"), "pe", "--executable", "x64")
            )
            self.assertEqual(module[:5], (*python("core.binary_audit"), "pe", "x64"))
            self.assertNotIn("--executable", module)
            # The real validator ran over the faithful empty probe export table...
            self.assertNotIn("LoadSubModule", Path(probe[-1]).read_text(encoding="utf-8"))
            # ...while the shipped module contract still rejects an empty export table.
            with self.assertRaisesRegex(AuditError, "unexpected exports"):
                binary_audit.main(("pe", "x64", module[-3], module[-2], probe[-1]))

    def test_audit_fails_closed_when_a_release_artifact_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            (fixture.repository / "out/native/bin/x64/Release/renpy.so").unlink()
            tools = fixture.tools()
            runner = ScriptedRunner(tools, fixture.inventory())
            with self.assertRaisesRegex(PackagingError, "artifact is missing"):
                audit_binaries(
                    fixture.repository, ("x64",), fixture.output, modules=("renpy",),
                    tools=tools, runner=runner,
                )
            self.assertEqual(runner.calls, [])


class PackageTests(unittest.TestCase):
    def invoke(self, fixture: Fixture, **options) -> tuple[PackageResult, ScriptedRunner]:
        tools = fixture.tools()
        runner = ScriptedRunner(tools, fixture.inventory())
        result = package(
            fixture.repository, options.pop("architectures", ("x64",)), fixture.output,
            tools=tools, runner=runner, **options,
        )
        return result, runner

    def test_package_emits_one_module_zip_per_architecture_and_one_symbol_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            result, _runner = self.invoke(fixture)
            packages = fixture.output / "packages"
            expected_archives = {
                f"{module}-{VERSIONS[module]}-x64.zip" for module in MODULES
            }
            expected_symbols = {
                f"{module}-{VERSIONS[module]}-pdb.zip" for module in MODULES
            }
            self.assertEqual({path.name for path in (packages / "x64").iterdir()}, expected_archives)
            self.assertEqual(
                {path.name for path in packages.iterdir() if path.is_file()},
                expected_symbols | {"packages.json"},
            )
            self.assertEqual({path.name for path in result.archives}, expected_archives | expected_symbols)
            self.assertEqual(result.deferred, ())

            with zipfile.ZipFile(packages / "x64" / "renpy-3.1.0-x64.zip") as archive:
                self.assertEqual(
                    archive.namelist(),
                    ["ChangeLog.en.txt", "ChangeLog.ru.txt", "LICENSES.txt",
                     "observer_user.ini", "renpy.so"],
                )
            with zipfile.ZipFile(packages / "renpy-3.1.0-pdb.zip") as archive:
                self.assertEqual(archive.namelist(), ["x64/renpy.pdb"])

            document = json.loads((packages / "packages.json").read_text(encoding="utf-8"))
            self.assertEqual([record["module"] for record in document["modules"]], list(MODULES))
            self.assertEqual(
                {record["module"]: record["version"] for record in document["modules"]}, VERSIONS
            )
            self.assertEqual(
                {archive["name"] for archive in document["archives"]},
                expected_archives | expected_symbols,
            )
            for record in document["modules"]:
                self.assertRegex(record["content"], r"^[0-9a-f]{64}$")
            self.assertFalse((fixture.output / "staging").exists())

    def test_package_all_architectures_yields_nine_module_zips_and_three_symbol_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary), architectures=("x86", "x64", "arm64"))
            result, _runner = self.invoke(fixture, architectures=("x86", "x64", "arm64"))
            packages = fixture.output / "packages"
            module_zips = [
                path for architecture in ("x86", "x64", "arm64")
                for path in (packages / architecture).iterdir()
            ]
            symbol_zips = [
                path for path in packages.iterdir() if path.name.endswith("-pdb.zip")
            ]
            self.assertEqual(len(module_zips), 9)
            self.assertEqual(len(symbol_zips), 3)
            self.assertEqual(len(result.archives), 12)
            with zipfile.ZipFile(packages / "renpy-3.1.0-pdb.zip") as archive:
                self.assertEqual(
                    archive.namelist(),
                    ["arm64/renpy.pdb", "x64/renpy.pdb", "x86/renpy.pdb"],
                )

    def test_package_selects_one_module_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            result, _runner = self.invoke(fixture, modules=("zanzarah",))
            packages = fixture.output / "packages"
            self.assertEqual(
                {path.name for path in (packages / "x64").iterdir()},
                {"zanzarah-2.1.0-x64.zip"},
            )
            self.assertEqual(
                {path.name for path in packages.iterdir() if path.is_file()},
                {"zanzarah-2.1.0-pdb.zip", "packages.json"},
            )
            document = json.loads((packages / "packages.json").read_text(encoding="utf-8"))
        self.assertEqual([record["module"] for record in document["modules"]], ["zanzarah"])
        self.assertEqual(
            {archive["name"] for archive in document["archives"]},
            {"zanzarah-2.1.0-x64.zip", "zanzarah-2.1.0-pdb.zip"},
        )
        self.assertEqual({path.name for path in result.archives},
                         {"zanzarah-2.1.0-x64.zip", "zanzarah-2.1.0-pdb.zip"})

    def test_package_smokes_runnable_architectures_and_defers_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary), architectures=("x64", "arm64"))
            result, runner = self.invoke(
                fixture, architectures=("x64", "arm64"), host="x64",
            )
            smoke = [call.argv for call in runner.calls if len(call.argv) > 3 and call.argv[3] == "smoke"]
            tests = str(fixture.repository / "out/native/bin/x64/Release/tests.exe")
        self.assertEqual(
            result.deferred,
            (DeferredGate("package-runtime", "arm64", "host cannot execute arm64 package-runtime"),),
        )
        self.assertEqual(len(smoke), len(MODULES))
        for argv in smoke:
            self.assertEqual(argv[:5], (*python("core.package"), "smoke", "x64"))
            self.assertIn(argv[5], MODULES)
            self.assertTrue(argv[6].endswith(".zip"))
            self.assertEqual(argv[7], tests)

    def test_package_requires_the_test_runner_for_runnable_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            (fixture.repository / "out/native/bin/x64/Release/tests.exe").unlink()
            with self.assertRaisesRegex(PackagingError, "test runner is missing"):
                self.invoke(fixture, host="x64")

    def test_package_fails_closed_when_symbols_or_resources_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            (fixture.repository / "out/native/bin/x64/Release/renpy.pdb").unlink()
            with self.assertRaisesRegex(PackagingError, "symbols are missing"):
                self.invoke(fixture)
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            (fixture.repository / "src/modules/renpy/observer_user.ini").unlink()
            with self.assertRaises((PackagingError, FileNotFoundError, OSError)):
                self.invoke(fixture)
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            (fixture.repository / "src/modules/renpy/VERSION").unlink()
            with self.assertRaisesRegex(Exception, "unreadable"):
                self.invoke(fixture)

    def test_validate_zip_matches_staged_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / "renpy.pdb"
            payload.write_bytes(b"symbols")
            archive = root / "one.zip"
            packaging_ops._zip(archive, {"renpy.pdb": payload})
            packaging_ops._validate_zip(archive, {"renpy.pdb": payload})

            extra = root / "extra.zip"
            packaging_ops._zip(extra, {"renpy.pdb": payload, "rpgmaker.pdb": payload})
            with self.assertRaisesRegex(PackagingError, "does not match"):
                packaging_ops._validate_zip(extra, {"renpy.pdb": payload})

            changed = root / "changed.zip"
            packaging_ops._zip(changed, {"renpy.pdb": payload})
            tampered = root / "other.pdb"
            tampered.write_bytes(b"different")
            with self.assertRaisesRegex(PackagingError, "does not match"):
                packaging_ops._validate_zip(changed, {"renpy.pdb": tampered})

            duplicate = root / "duplicate.zip"
            with self.assertWarns(UserWarning), zipfile.ZipFile(duplicate, "w") as bundle:
                bundle.writestr("renpy.pdb", b"symbols")
                bundle.writestr("renpy.pdb", b"symbols")
            with self.assertRaisesRegex(PackagingError, "duplicate entries"):
                packaging_ops._validate_zip(duplicate, {"renpy.pdb": payload})

    def test_package_audits_and_publishes_the_same_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            tools = fixture.tools()
            source = fixture.repository / "out/native/bin/x64/Release/renpy.so"
            original = source.read_bytes()
            runner = MutatingRunner(
                tools, fixture.inventory(), target="renpy.so",
                swap=lambda: source.write_bytes(b"swapped-after-audit"),
            )
            package(
                fixture.repository, ("x64",), fixture.output, modules=("renpy",),
                tools=tools, runner=runner,
            )
            published = fixture.output / "packages" / "x64" / "renpy-3.1.0-x64.zip"
            with zipfile.ZipFile(published) as archive:
                self.assertEqual(archive.read("renpy.so"), original)
            # The repository copy really moved after the audit began.
            self.assertEqual(source.read_bytes(), b"swapped-after-audit")
            self.assertFalse(any("/t:Build" in " ".join(call.argv) for call in runner.calls))

    def test_package_replaces_a_previous_selected_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            self.invoke(fixture)
            packages = fixture.output / "packages"
            self.assertIn("renpy-3.1.0-pdb.zip", {item.name for item in packages.iterdir()})

            self.invoke(fixture, modules=("renpy",))
            self.assertEqual(
                {item.name for item in (packages / "x64").iterdir()},
                {"renpy-3.1.0-x64.zip"},
            )
            self.assertEqual(
                {item.name for item in packages.iterdir() if item.is_file()},
                {"renpy-3.1.0-pdb.zip", "packages.json"},
            )
            document = json.loads((packages / "packages.json").read_text(encoding="utf-8"))
        self.assertEqual([record["module"] for record in document["modules"]], ["renpy"])
        self.assertEqual(
            {archive["name"] for archive in document["archives"]},
            {"renpy-3.1.0-x64.zip", "renpy-3.1.0-pdb.zip"},
        )

    def test_package_rerun_drops_superseded_version_and_architecture_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary), architectures=("x64", "arm64"))
            self.invoke(fixture, architectures=("x64",), modules=("renpy",))
            (fixture.repository / "src/modules/renpy/VERSION").write_text(
                "3.2.0\n", encoding="utf-8"
            )
            for name, language in zip(CHANGELOGS, ("en", "ru"), strict=True):
                (fixture.repository / "src/modules/renpy" / name).write_text(
                    changelog("renpy", "3.2.0", language), encoding="utf-8"
                )
            self.invoke(fixture, architectures=("x64", "arm64"), modules=("renpy",))
            for architecture in ("x64", "arm64"):
                self.assertEqual(
                    {item.name for item in (fixture.output / "packages" / architecture).iterdir()},
                    {f"renpy-3.2.0-{architecture}.zip"},
                )
            self.assertEqual(
                {
                    item.name for item in (fixture.output / "packages").iterdir()
                    if item.is_file() and item.name.endswith("-pdb.zip")
                },
                {"renpy-3.2.0-pdb.zip"},
            )
            document = json.loads(
                (fixture.output / "packages" / "packages.json").read_text(encoding="utf-8")
            )
        self.assertEqual({record["version"] for record in document["modules"]}, {"3.2.0"})
        self.assertEqual(
            {archive["name"] for archive in document["archives"]},
            {
                "renpy-3.2.0-x64.zip", "renpy-3.2.0-arm64.zip",
                "renpy-3.2.0-pdb.zip",
            },
        )

    def test_package_failed_rerun_preserves_the_published_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            self.invoke(fixture)
            packages = fixture.output / "packages"
            before = (packages / "packages.json").read_text(encoding="utf-8")
            before_names = sorted(item.name for item in packages.iterdir())
            (fixture.repository / "out/native/bin/x64/Release/renpy.pdb").unlink()
            with self.assertRaisesRegex(PackagingError, "symbols are missing"):
                self.invoke(fixture)
            self.assertEqual((packages / "packages.json").read_text(encoding="utf-8"), before)
            self.assertEqual(sorted(item.name for item in packages.iterdir()), before_names)
            self.assertFalse((fixture.output / "staging").exists())


class ToolsTests(unittest.TestCase):
    def test_locate_packaging_tools_composes_native_and_quality_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            msbuild, vcpkg = root / "MSBuild.exe", root / "vcpkg/vcpkg.exe"
            dumpbin, binskim = root / "dumpbin.exe", root / "BinSkim.exe"
            native_tools = NativeTools(msbuild=msbuild, vcpkg=vcpkg)
            with mock.patch("packaging_ops.locate_tools", return_value=native_tools) as locate, \
                    mock.patch("packaging_ops.discover_msvc_toolchain", return_value="chain"), \
                    mock.patch("packaging_ops.resolve_dumpbin",
                               return_value=types.SimpleNamespace(path=dumpbin)), \
                    mock.patch("packaging_ops.resolve_binskim",
                               return_value=types.SimpleNamespace(path=binskim)):
                tools = packaging_ops.locate_packaging_tools(environ={"VCPKG_ROOT": str(root)})
        locate.assert_called_once_with(environ={"VCPKG_ROOT": str(root)})
        self.assertEqual(
            (tools.msbuild, tools.vcpkg_root, tools.dumpbin, tools.binskim),
            (msbuild, vcpkg.parent, dumpbin, binskim),
        )

    def test_default_runner_forwards_argv_cwd_env_and_stdout(self) -> None:
        with mock.patch("packaging_ops.subprocess.run") as run:
            packaging_ops._default_runner(
                (Path("tool.exe"),), cwd=Path("here"), env={"A": "1"}, stdout=None
            )
        run.assert_called_once_with(
            ["tool.exe"], cwd="here", env={**os.environ, "A": "1"}, check=True, stdout=None,
        )
        with mock.patch("packaging_ops.subprocess.run") as run:
            packaging_ops._default_runner((Path("tool.exe"),))
        run.assert_called_once_with(["tool.exe"], cwd=None, env=None, check=True, stdout=None)


if __name__ == "__main__":
    unittest.main(verbosity=2)

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


import sys

BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.quality_tools import (  # noqa: E402
    QualityTools,
    ResolvedDirectory,
    ResolvedTool,
    SanitizerRuntimes,
    discover_quality_tools,
    resolve_binskim,
    resolve_dumpbin,
    resolve_llvm,
    resolve_asan_runtimes,
    resolve_sanitizer_runtimes,
    resolve_tool,
    resolve_ubsan_runtime,
    resolve_umdh,
)


@dataclass(frozen=True)
class FakeToolchain:
    installation: Path
    llvm_dir: Path
    identity: tuple[tuple[str, str], ...]


class QualityToolsTests(unittest.TestCase):
    def fixture(self, root: Path, *, kit11: bool = True) -> tuple[FakeToolchain, Path, Path]:
        installation, llvm = root / "Visual Studio", root / "LLVM"
        version = "14.44.35207"
        paths = (
            llvm / "bin/clang-cl.exe",
            llvm / "bin/clang-scan-deps.exe",
            llvm / "bin/llvm-cov.exe",
            llvm / "bin/llvm-profdata.exe",
            installation / f"VC/Tools/MSVC/{version}/bin/Hostx64/x64/dumpbin.exe",
        )
        for index, path in enumerate(paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"tool-{index}".encode())
        program_files = root / "Program Files (x86)"
        kit = "11" if kit11 else "10"
        umdh = program_files / f"Windows Kits/{kit}/Debuggers/x64/umdh.exe"
        umdh.parent.mkdir(parents=True)
        umdh.write_bytes(b"umdh")
        binskim = root / "shims/BinSkim.exe"
        binskim.parent.mkdir()
        binskim.write_bytes(b"binskim")
        identity = (("clang_tidy_version", "19.1.5"), ("vc_tools_version", version))
        return FakeToolchain(installation, llvm, identity), binskim, program_files

    def runtime_fixture(
        self, toolchain: FakeToolchain, llvm_version: str = "19"
    ) -> tuple[Path, tuple[Path, Path]]:
        version = dict(toolchain.identity)["vc_tools_version"]
        asan = toolchain.installation / f"VC/Tools/MSVC/{version}/bin/Hostx64"
        for architecture, name in (
            ("x86", "clang_rt.asan_dynamic-i386.dll"),
            ("x64", "clang_rt.asan_dynamic-x86_64.dll"),
        ):
            path = asan / architecture / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"asan-{architecture}".encode())
        directory = toolchain.llvm_dir / f"lib/clang/{llvm_version}/lib/windows"
        libraries = tuple(
            directory / name
            for name in (
                "clang_rt.ubsan_standalone-x86_64.lib",
                "clang_rt.ubsan_standalone_cxx-x86_64.lib",
            )
        )
        directory.mkdir(parents=True)
        for index, library in enumerate(libraries):
            library.write_bytes(f"ubsan-{index}".encode())
        return directory, libraries  # type: ignore[return-value]

    def test_resolves_exact_consumers_with_canonical_streaming_sha256_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            toolchain, binskim, program_files = self.fixture(Path(temporary))
            with (
                mock.patch.dict(os.environ, {"ProgramFiles(x86)": str(program_files)}, clear=False),
                mock.patch("core.quality_tools.shutil.which", return_value=str(binskim)) as which,
                mock.patch.object(Path, "read_bytes", side_effect=AssertionError("must stream")),
            ):
                tools = discover_quality_tools(toolchain)  # type: ignore[arg-type]
                expected_digests = {}
                for tool in (
                    tools.clang_cl, tools.clang_scan_deps, tools.llvm_cov, tools.llvm_profdata,
                    tools.dumpbin, tools.binskim, tools.umdh,
                ):
                    with tool.path.open("rb") as stream:
                        expected_digests[tool.path] = hashlib.file_digest(stream, "sha256").hexdigest()

        which.assert_called_once_with("binskim")
        self.assertIsInstance(tools, QualityTools)
        expected_names = (
            "clang-cl.exe", "clang-scan-deps.exe", "llvm-cov.exe", "llvm-profdata.exe",
            "dumpbin.exe", "BinSkim.exe", "umdh.exe",
        )
        self.assertEqual(
            tuple(tool.path.name for tool in (
                tools.clang_cl, tools.clang_scan_deps, tools.llvm_cov, tools.llvm_profdata,
                tools.dumpbin, tools.binskim, tools.umdh,
            )),
            expected_names,
        )
        for tool in (
            tools.clang_cl, tools.clang_scan_deps, tools.llvm_cov, tools.llvm_profdata,
            tools.dumpbin, tools.binskim, tools.umdh,
        ):
            self.assertEqual(tool.identity[0], ("path", str(tool.path)))
            self.assertEqual(tool.identity[1][0], "sha256")
            self.assertEqual(tool.identity[1][1], expected_digests[tool.path])
        self.assertIn("Windows Kits\\11", str(tools.umdh.path))
        with self.assertRaises(FrozenInstanceError):
            tools.binskim = ResolvedTool(tools.binskim.path, tools.binskim.identity)  # type: ignore[misc]

    def test_windows_kit_10_is_the_deterministic_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            toolchain, binskim, program_files = self.fixture(Path(temporary), kit11=False)
            with mock.patch.dict(os.environ, {"ProgramFiles(x86)": str(program_files)}, clear=False), mock.patch(
                "core.quality_tools.shutil.which", return_value=str(binskim)
            ):
                tools = discover_quality_tools(toolchain)  # type: ignore[arg-type]
        self.assertIn("Windows Kits\\10", str(tools.umdh.path))

    def test_selective_resolvers_are_lazy_and_match_the_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            toolchain, binskim, program_files = self.fixture(Path(temporary))
            (toolchain.llvm_dir / "bin/llvm-cov.exe").unlink()
            with (
                mock.patch.dict(os.environ, {"ProgramFiles(x86)": str(program_files)}, clear=False),
                mock.patch("core.quality_tools.shutil.which", return_value=str(binskim)),
            ):
                self.assertEqual(resolve_llvm(toolchain, "clang-cl").path.name, "clang-cl.exe")
                self.assertEqual(
                    resolve_llvm(toolchain, "clang-scan-deps").path.name,
                    "clang-scan-deps.exe",
                )
                self.assertEqual(resolve_dumpbin(toolchain).path.name, "dumpbin.exe")
                self.assertEqual(resolve_binskim().path, binskim.resolve())
                self.assertEqual(resolve_umdh().path.name, "umdh.exe")
                with self.assertRaisesRegex(FileNotFoundError, "missing llvm-cov"):
                    discover_quality_tools(toolchain)  # type: ignore[arg-type]
                with self.assertRaisesRegex(ValueError, "unsupported LLVM tool: clang-tidy"):
                    resolve_llvm(toolchain, "clang-tidy")

    def test_sanitizer_runtimes_are_exact_typed_and_content_addressed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            toolchain, _binskim, _program_files = self.fixture(Path(temporary))
            directory, libraries = self.runtime_fixture(toolchain)
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("must stream")):
                runtimes = resolve_sanitizer_runtimes(toolchain)  # type: ignore[arg-type]

        self.assertIsInstance(runtimes, SanitizerRuntimes)
        self.assertEqual(runtimes.asan_x86.path.name, "clang_rt.asan_dynamic-i386.dll")
        self.assertEqual(runtimes.asan_x64.path.name, "clang_rt.asan_dynamic-x86_64.dll")
        self.assertIsInstance(runtimes.ubsan, ResolvedDirectory)
        self.assertEqual(runtimes.ubsan.path, directory.resolve())
        self.assertEqual(tuple(tool.path for tool in runtimes.ubsan.files), libraries)
        identity = dict(runtimes.ubsan.identity)
        self.assertEqual(identity["path"], str(directory.resolve()))
        for tool in runtimes.ubsan.files:
            name = tool.path.name
            self.assertEqual(identity[f"{name}.path"], str(tool.path))
            self.assertEqual(identity[f"{name}.sha256"], dict(tool.identity)["sha256"])
        with self.assertRaises(FrozenInstanceError):
            runtimes.asan_x86 = runtimes.asan_x64  # type: ignore[misc]

    def test_sanitizer_resolvers_load_only_the_requested_runtime_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            toolchain, _binskim, _program_files = self.fixture(Path(temporary))
            directory, libraries = self.runtime_fixture(toolchain)
            version = dict(toolchain.identity)["vc_tools_version"]
            asan_root = toolchain.installation / f"VC/Tools/MSVC/{version}/bin/Hostx64"

            libraries[0].unlink()
            selected = resolve_asan_runtimes(toolchain, ("x64",))  # type: ignore[arg-type]
            self.assertEqual(tuple(selected), (("x64", selected[0][1]),))
            self.assertEqual(selected[0][1].path.name, "clang_rt.asan_dynamic-x86_64.dll")
            with self.assertRaisesRegex(ValueError, "unsupported ASan architecture: arm64"):
                resolve_asan_runtimes(toolchain, ("arm64",))  # type: ignore[arg-type]

            libraries[0].write_bytes(b"ubsan-0")
            for runtime in asan_root.rglob("*.dll"):
                runtime.unlink()
            ubsan = resolve_ubsan_runtime(toolchain)  # type: ignore[arg-type]
            self.assertEqual(ubsan.path, directory.resolve())

    def test_sanitizer_runtime_errors_name_the_exact_missing_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            toolchain, _binskim, _program_files = self.fixture(root)
            directory, libraries = self.runtime_fixture(toolchain)
            version = dict(toolchain.identity)["vc_tools_version"]
            missing_asan = (
                toolchain.installation
                / f"VC/Tools/MSVC/{version}/bin/Hostx64/x86/clang_rt.asan_dynamic-i386.dll"
            )
            missing_asan.unlink()
            with self.assertRaisesRegex(FileNotFoundError, "missing MSVC ASan x86 runtime"):
                resolve_sanitizer_runtimes(toolchain)  # type: ignore[arg-type]
            missing_asan.write_bytes(b"asan-x86")
            libraries[1].unlink()
            with self.assertRaisesRegex(
                FileNotFoundError, "missing UBSan runtime clang_rt.ubsan_standalone_cxx-x86_64.lib"
            ):
                resolve_sanitizer_runtimes(toolchain)  # type: ignore[arg-type]
            libraries[0].unlink()
            directory.rmdir()
            with self.assertRaisesRegex(FileNotFoundError, "missing UBSan runtime directory"):
                resolve_sanitizer_runtimes(toolchain)  # type: ignore[arg-type]

            no_version = FakeToolchain(toolchain.installation, toolchain.llvm_dir, ())
            with self.assertRaisesRegex(FileNotFoundError, "missing MSVC vc_tools_version identity"):
                resolve_dumpbin(no_version)  # type: ignore[arg-type]

    def test_full_llvm_version_runtime_precedes_the_major_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            toolchain, _binskim, _program_files = self.fixture(Path(temporary))
            self.runtime_fixture(toolchain)
            directory, _libraries = self.runtime_fixture(toolchain, "19.1.5")
            runtimes = resolve_sanitizer_runtimes(toolchain)  # type: ignore[arg-type]
        self.assertEqual(runtimes.ubsan.path, directory.resolve())

    def test_each_missing_tool_is_named_precisely(self) -> None:
        cases = (
            ("clang-cl", "LLVM/bin/clang-cl.exe"),
            ("clang-scan-deps", "LLVM/bin/clang-scan-deps.exe"),
            ("llvm-cov", "LLVM/bin/llvm-cov.exe"),
            ("llvm-profdata", "LLVM/bin/llvm-profdata.exe"),
            ("dumpbin", "Visual Studio/VC/Tools/MSVC/14.44.35207/bin/Hostx64/x64/dumpbin.exe"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (name, relative) in enumerate(cases):
                toolchain, binskim, program_files = self.fixture(root / str(index))
                (root / str(index) / relative).unlink()
                with self.subTest(name=name), mock.patch.dict(
                    os.environ, {"ProgramFiles(x86)": str(program_files)}, clear=False
                ), mock.patch("core.quality_tools.shutil.which", return_value=str(binskim)), self.assertRaisesRegex(
                    FileNotFoundError, f"missing {name}"
                ):
                    discover_quality_tools(toolchain)  # type: ignore[arg-type]

            toolchain, _binskim, program_files = self.fixture(root / "binskim")
            with mock.patch.dict(os.environ, {"ProgramFiles(x86)": str(program_files)}, clear=False), mock.patch(
                "core.quality_tools.shutil.which", return_value=None
            ), self.assertRaisesRegex(FileNotFoundError, "missing BinSkim"):
                discover_quality_tools(toolchain)  # type: ignore[arg-type]

            toolchain, binskim, program_files = self.fixture(root / "umdh")
            (program_files / "Windows Kits/11/Debuggers/x64/umdh.exe").unlink()
            with mock.patch.dict(os.environ, {"ProgramFiles(x86)": str(program_files)}, clear=False), mock.patch(
                "core.quality_tools.shutil.which", return_value=str(binskim)
            ), self.assertRaisesRegex(FileNotFoundError, "missing UMDH"):
                discover_quality_tools(toolchain)  # type: ignore[arg-type]

    def test_resolve_tool_rejects_non_files_and_content_changes_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "tool.exe"
            path.write_bytes(b"one")
            before = resolve_tool(path, "example")
            path.write_bytes(b"two")
            after = resolve_tool(path, "example")
            with self.assertRaisesRegex(FileNotFoundError, "missing absent"):
                resolve_tool(None, "absent")
            with self.assertRaisesRegex(FileNotFoundError, "missing directory"):
                resolve_tool(root, "directory")
        self.assertNotEqual(before.identity, after.identity)


if __name__ == "__main__":
    unittest.main(verbosity=2)

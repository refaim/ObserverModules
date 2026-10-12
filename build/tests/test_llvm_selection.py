"""Behavioral contract for forwarding the selected LLVM tool-set version.

``Microsoft.Cpp.ClangCl.Common.props`` derives ``LLVMIncludeVersion`` from the major
component of ``LLVMToolsVersion`` (default 19.1.5).  When the driver points MSBuild at a
selected LLVM runtime it must also name the matching tool-set version, otherwise MSBuild
builds a global-version token that no installed layout provides and fails with MSB8073.

These tests drive the real ``main._msbuild_command`` against an on-disk LLVM fixture shaped
like ``<root>/lib/clang/<version>/lib/windows`` and assert the emitted ``/p:`` properties.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

import main


class LlvmSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.project = self.repository / "build" / "projects" / "tests.vcxproj"
        self.msbuild = self.root / "MSBuild.exe"
        self.vcpkg = self.root / "vcpkg.exe"

    def _llvm(self, version: str) -> main.Llvm:
        directory = self.root / "llvm"
        runtime = directory / "lib" / "clang" / version / "lib" / "windows"
        runtime.mkdir(parents=True, exist_ok=True)
        return main.Llvm(
            directory=directory,
            runtime=runtime,
            cov=self.root / "llvm-cov.exe",
            profdata=self.root / "llvm-profdata.exe",
        )

    def _command(self, llvm: main.Llvm) -> tuple[str, ...]:
        return main._msbuild_command(
            self.repository, self.project, "x64", "Debug", None,
            self.msbuild, self.vcpkg, llvm=llvm,
        )

    def test_selected_runtime_forwards_its_tool_set_version(self) -> None:
        for version in ("20", "19", "19.1.0"):
            with self.subTest(version=version):
                llvm = self._llvm(version)
                command = self._command(llvm)
                self.assertIn(f"/p:LLVMToolsVersion={version}", command)
                self.assertIn(f"/p:LLVMInstallDir={llvm.directory}", command)
                self.assertIn(f"/p:LLVMRuntimeDir={llvm.runtime}", command)

    def test_selected_runtime_normalises_the_install_directory(self) -> None:
        # The forwarded tool-set version is only the leaf component of the runtime path, so a
        # deep or trailing-separator install directory must not leak into the property.
        llvm = self._llvm("19.1.0")
        command = self._command(llvm)
        tools_version = f"/p:LLVMToolsVersion={llvm.runtime.parent.parent.name}"
        self.assertEqual(tools_version, "/p:LLVMToolsVersion=19.1.0")
        self.assertIn(tools_version, command)

    def test_without_a_selected_runtime_no_tool_set_version_is_forced(self) -> None:
        command = main._msbuild_command(
            self.repository, self.project, "x64", "Debug", None,
            self.msbuild, self.vcpkg,
        )
        self.assertFalse(
            [item for item in command if item.startswith("/p:LLVMToolsVersion=")]
        )


if __name__ == "__main__":
    unittest.main()

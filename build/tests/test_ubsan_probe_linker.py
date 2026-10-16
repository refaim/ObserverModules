"""Executable fixture tests for the UBSan probe's resolved-linker guard.

On Windows the clang-cl driver resolves ``-fuse-ld=lld`` through the ``-B`` directory and prints
the chosen linker with ``-###``. That text carries escaped Windows backslashes, may mix the final
separator and may omit the ``.exe`` suffix, so a bare substring search is not enough: the guard
must read the real ``-###`` output and accept exactly one absolute ``lld-link`` that equals the
paired binary the job supplied, rejecting a missing, duplicated, fallback or target-prefixed
resolution. These tests drive the guard against the real ``-###`` shape and controlled fixtures;
no compiler is run here.
"""

from __future__ import annotations

import re
from pathlib import Path
import subprocess
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
PREP_WORKFLOW = REPOSITORY / ".github" / "workflows" / "ci-instrumentation.yml"
GUARD_FUNCTION = "Assert-UbsanProbeLinker"

# The paired linker the job would supply, matching the real 21.1.8/23.1.3 tree shape.
PAIRED_LINKER = r"C:\Users\Roma\scoop\apps\llvm\23.1.3\bin\lld-link.exe"

# Real clang-cl ``-###`` output for the accepted ``-B`` selection: the compiler command comes
# first, then the linker command resolved to its paired absolute path with escaped backslashes,
# a mixed final slash and no ``.exe`` suffix.
ACCEPTED_PREVIEW = (
    ' "C:\\\\Users\\\\Roma\\\\scoop\\\\apps\\\\llvm\\\\23.1.3\\\\bin\\\\clang-cl.exe"'
    ' "-cc1" "-triple" "x86_64-pc-windows-msvc"\n'
    ' "C:\\\\Users\\\\Roma\\\\scoop\\\\apps\\\\llvm\\\\23.1.3\\\\bin/lld-link"'
    ' "-out:probe.exe" "-nologo" "/WX" "probe.obj"\n'
)

# Real clang-cl ``-###`` output for the unsafe absolute ``-fuse-ld`` path: the driver prefixes the
# target triple to the absolute path, so the first quoted token is not a valid absolute path.
TARGET_PREFIXED_PREVIEW = (
    ' "x86_64-pc-windows-msvc-coff-C:\\\\Users\\\\Roma\\\\scoop\\\\apps\\\\llvm\\\\23.1.3'
    '\\\\bin\\\\lld-link.exe" "-out:probe.exe" "-nologo" "/WX" "probe.obj"\n'
)


def brace_block(workflow: str, name: str) -> str:
    """Return the brace-balanced PowerShell ``function <name>`` definition from a workflow."""

    lines = workflow.splitlines()
    start = next(
        (index for index, line in enumerate(lines)
         if re.match(rf"^ *function {re.escape(name)} \{{\s*$", line)),
        None,
    )
    if start is None:
        raise AssertionError(f"PowerShell function not found: {name}")
    depth = 0
    for index in range(start, len(lines)):
        depth += lines[index].count("{") - lines[index].count("}")
        if depth == 0:
            return "\n".join(lines[start:index + 1])
    raise AssertionError(f"unterminated PowerShell function: {name}")


def escaped(path: str) -> str:
    """Render a Windows path the way clang's ``-###`` printer escapes it."""

    return path.replace("\\", "\\\\")


def command(executable: str, *arguments: str) -> str:
    """One printed command line: the first quoted token is the executable."""

    rendered = " ".join(f'"{argument}"' for argument in arguments)
    return f'"{escaped(executable)}" {rendered}'


def preview(*commands: str) -> str:
    return "".join(f" {line}\n" for line in commands)


def run_guard(workflow: str, output: str, expected: str) -> subprocess.CompletedProcess[str]:
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"{brace_block(workflow, GUARD_FUNCTION)}\n"
        f"$output = @'\n{output}'@\n"
        f"{GUARD_FUNCTION} -Output $output -ExpectedLinker '{expected}'\n"
    )
    return subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class HelperTests(unittest.TestCase):
    """The extraction and rendering helpers back every assertion below, so pin their shapes."""

    def test_brace_block_finds_the_definition_and_reports_both_malformed_inputs(self) -> None:
        workflow = PREP_WORKFLOW.read_text(encoding="utf-8")
        definition = brace_block(workflow, GUARD_FUNCTION)
        self.assertIn(f"function {GUARD_FUNCTION}", definition)
        self.assertTrue(definition.rstrip().endswith("}"))
        with self.assertRaises(AssertionError):
            brace_block(workflow, "Assert-NoSuchFunction")
        with self.assertRaises(AssertionError):
            brace_block("          function Broken {\n", "Broken")

    def test_command_and_preview_render_the_escaped_dry_run_shape(self) -> None:
        rendered = preview(command(PAIRED_LINKER, "-out:probe.exe"))
        self.assertTrue(rendered.startswith(' "C:\\\\Users'))
        self.assertTrue(rendered.endswith("\n"))
        self.assertIn('"-out:probe.exe"', rendered)


class ResolvedLinkerGuardTests(unittest.TestCase):
    """Only the one paired, absolute ``lld-link`` the job supplied may be accepted."""

    def setUp(self) -> None:
        self.workflow = PREP_WORKFLOW.read_text(encoding="utf-8")

    def _check(self, output: str, expected: str = PAIRED_LINKER) -> subprocess.CompletedProcess[str]:
        return run_guard(self.workflow, output, expected)

    def test_the_real_paired_preview_is_accepted(self) -> None:
        result = self._check(ACCEPTED_PREVIEW)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_paired_path_with_spaces_is_accepted(self) -> None:
        expected = r"C:\Program Files\LLVM 21.1.8\x64\bin\lld-link.exe"
        result = self._check(preview(command(expected, "-out:probe.exe")), expected)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_differently_cased_paired_path_is_accepted(self) -> None:
        expected = r"c:\program files\llvm 21.1.8\x64\bin\lld-link.exe"
        actual = r"C:\PROGRAM FILES\LLVM 21.1.8\X64\BIN\LLD-LINK.EXE"
        result = self._check(preview(command(actual, "-out:probe.exe")), expected)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_paired_path_without_the_extension_is_accepted(self) -> None:
        without_extension = PAIRED_LINKER[: -len(".exe")]
        result = self._check(preview(command(without_extension, "-out:probe.exe")))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_missing_linker_command_is_rejected(self) -> None:
        result = self._check(preview(command(r"C:\tools\llvm\bin\clang-cl.exe", "-cc1")))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one", result.stderr)

    def test_duplicate_linker_commands_are_rejected(self) -> None:
        result = self._check(preview(
            command(PAIRED_LINKER, "-out:probe.exe"),
            command(PAIRED_LINKER, "-out:probe.exe"),
        ))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one", result.stderr)

    def test_a_fallback_directory_is_rejected(self) -> None:
        fallback = r"C:\tools\msvc\bin\lld-link.exe"
        result = self._check(preview(command(fallback, "-out:probe.exe")))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("instead of the paired", result.stderr)

    def test_a_target_prefixed_path_is_rejected(self) -> None:
        result = self._check(TARGET_PREFIXED_PREVIEW)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-absolute", result.stderr)


if __name__ == "__main__":
    unittest.main()

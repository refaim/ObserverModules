"""The UBSan runtime configure step must hand CMake real absolute tool paths.

The provisioning job configures the LLVM ``runtimes`` tree with clang-cl, lld-link and the
MSVC archiver.  Those four ``-D`` arguments have to be quoted as whole native arguments: an
unquoted ``-DCMAKE_C_COMPILER=$clangCl`` reaches CMake with the literal, unexpanded spelling
instead of the real absolute path, so the configured cache never carries the tool the workflow
selected.  These tests extract the workflow's real multiline configure invocation, bind the tool
variables to owned paths that contain spaces, run it under ``pwsh`` against a tiny owned
``project(... LANGUAGES NONE)`` tree, and read the produced ``CMakeCache.txt``.  The real ``cmake``
and ``ninja`` on PATH are used; nothing is mocked, no compiler is executed and no native build is
performed.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
PREP_WORKFLOW = REPOSITORY / ".github" / "workflows" / "ci-instrumentation.yml"

# The verbatim configure invocation: the ``& cmake -S (Join-Path $source 'runtimes') ...`` line
# through the trailing ``-DCOMPILER_RT_INCLUDE_TESTS=OFF`` flag, backtick continuations included.
CONFIGURE_PATTERN = re.compile(
    r"(?ms)^[ \t]*& cmake -S \(Join-Path \$source 'runtimes'\) -B \$ownedBuild -G Ninja `\n"
    r".*?-DCOMPILER_RT_INCLUDE_TESTS=OFF$"
)

# The four native ``-D`` arguments carried by their tool variables and the tool each names.
TOOL_ARGUMENTS = (
    ("clangCl", "CMAKE_C_COMPILER"),
    ("clangCl", "CMAKE_CXX_COMPILER"),
    ("lldLink", "CMAKE_LINKER"),
    ("libExe", "CMAKE_AR"),
)

PROJECT_SOURCE = "cmake_minimum_required(VERSION 3.20)\nproject(unpackrat_tool_paths LANGUAGES NONE)\n"

GUARD = 'if ($LASTEXITCODE -ne 0) { throw "configure failed with exit code $LASTEXITCODE." }'


def configure_block(workflow: str) -> str:
    """Return the workflow's real configure invocation, dedented but otherwise verbatim."""

    return textwrap.dedent(CONFIGURE_PATTERN.search(workflow).group(0)).strip()


def configure_script(block: str, bindings: dict[str, str]) -> str:
    """Bind the tool variables to owned paths and append a fail-closed exit guard."""

    assignments = "\n".join(f"${name} = '{value}'" for name, value in bindings.items())
    return "\n".join(["$ErrorActionPreference = 'Stop'", assignments, block, GUARD])


def read_cache(path: Path) -> dict[str, str]:
    """Parse ``CMakeCache.txt`` into a ``NAME -> value`` mapping, dropping the type suffix."""

    lines = path.read_text(encoding="utf-8").splitlines()
    entries = (line.partition("=") for line in lines if "=" in line and not line.startswith("//"))
    return {name.split(":")[0]: value for name, _, value in entries}


class UbsanCmakeArgumentTests(unittest.TestCase):
    """The configure invocation transports each selected tool as its real absolute path."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = PREP_WORKFLOW.read_text(encoding="utf-8")
        cls.block = configure_block(cls.workflow)
        cls.base = Path(tempfile.mkdtemp(prefix="ubsan-cmake-args-"))
        toolchain = cls.base / "tool chain"
        (toolchain / "x64" / "bin").mkdir(parents=True)
        (toolchain / "archiver").mkdir()
        cls.bound = {
            "clangCl": str(toolchain / "x64" / "bin" / "clang-cl.exe"),
            "lldLink": str(toolchain / "x64" / "bin" / "lld-link.exe"),
            "libExe": str(toolchain / "archiver" / "lib.exe"),
            "source": str(cls.base / "llvm source"),
            "ownedBuild": str(cls.base / "owned build"),
        }
        for relative in ("x64/bin/clang-cl.exe", "x64/bin/lld-link.exe", "archiver/lib.exe"):
            (toolchain / relative).write_text("fixture\n", encoding="utf-8")
        runtimes = Path(cls.bound["source"]) / "runtimes"
        runtimes.mkdir(parents=True)
        (runtimes / "CMakeLists.txt").write_text(PROJECT_SOURCE, encoding="utf-8")
        cls.result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command",
             configure_script(cls.block, cls.bound)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        cls.cache = read_cache(Path(cls.bound["ownedBuild"]) / "CMakeCache.txt")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.base, ignore_errors=True)

    def test_configure_invocation_is_read_verbatim_from_the_workflow(self) -> None:
        self.assertIn("& cmake -S (Join-Path $source 'runtimes') -B $ownedBuild -G Ninja", self.block)
        for _, cache_key in TOOL_ARGUMENTS:
            self.assertIn(f"-D{cache_key}=", self.block)

    def test_configure_succeeds(self) -> None:
        self.assertEqual(
            self.result.returncode,
            0,
            f"pwsh configure failed: {self.result.stdout}\n{self.result.stderr}",
        )

    def test_cache_carries_each_real_absolute_tool_path(self) -> None:
        for variable, cache_key in TOOL_ARGUMENTS:
            with self.subTest(argument=cache_key):
                self.assertEqual(self.cache.get(cache_key), self.bound[variable])

    def test_cache_never_carries_a_literal_placeholder(self) -> None:
        for variable, cache_key in TOOL_ARGUMENTS:
            with self.subTest(argument=cache_key):
                self.assertNotIn("$", self.cache.get(cache_key, f"${variable}"))


if __name__ == "__main__":
    unittest.main()

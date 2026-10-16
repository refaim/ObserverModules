"""The UBSan runtime source is unpacked into the four component trees the rebuild needs.

The official ``llvm-project-21.1.8.src.tar`` carries six relative symlink entries under the
unrelated ``clang`` and ``lldb`` test trees.  7-Zip 26.04 refuses those "dangerous" link paths and
exits nonzero, which failed the hosted CI preparation before CMake ever ran.  The rebuild only
needs ``cmake``, ``compiler-rt``, ``runtimes`` and ``llvm``, so the provisioning job must hand the
installed archiver exactly those four whole component trees and never touch the rest.

These tests build a small in-memory tar fixture carrying every required tree, the two unrelated
trees and a representative relative link entry, then execute the real extraction command and
selection taken verbatim from the workflow.  The real ``7z`` on PATH is used; it is never mocked.
One case drives a failing selection into the workflow's own exit-code guard so an ignored archiver
error cannot pass, and one case pins that the reader reflects the workflow instead of injecting an
allowlist of its own.
"""

from __future__ import annotations

import io
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
PREP_WORKFLOW = REPOSITORY / ".github" / "workflows" / "ci-instrumentation.yml"

SOURCE_TREE = "llvm-project-21.1.8.src"
REQUIRED_TREES = ("cmake", "compiler-rt", "runtimes", "llvm")
EXCLUDED_TREES = ("clang", "lldb")
GUARD_MESSAGE = "LLVM source tar extraction failed."

INVOCATION_PATTERN = re.compile(r"(?m)^[ \t]*& 7z x \$tarball .*$")
SELECTOR_PATTERN = re.compile(r"(?m)^[ \t]*\$sourceTrees = @\(\n(?:.*\n)*?[ \t]*\)$")
GUARD_PATTERN = re.compile(
    r"(?m)^[ \t]*if \(\$LASTEXITCODE -ne 0\) \{ throw '" + re.escape(GUARD_MESSAGE) + r"' \}$"
)

# Nested files prove each kept tree is extracted whole rather than only its top directory.
REQUIRED_MEMBERS = (
    f"{SOURCE_TREE}/cmake/CMakeLists.txt",
    f"{SOURCE_TREE}/cmake/Modules/Platform/Windows.cmake",
    f"{SOURCE_TREE}/compiler-rt/CMakeLists.txt",
    f"{SOURCE_TREE}/compiler-rt/lib/ubsan/ubsan_handlers.cpp",
    f"{SOURCE_TREE}/compiler-rt/lib/sanitizer_common/sanitizer_common.cpp",
    f"{SOURCE_TREE}/runtimes/CMakeLists.txt",
    f"{SOURCE_TREE}/llvm/CMakeLists.txt",
    f"{SOURCE_TREE}/llvm/cmake/modules/LLVMConfig.cmake",
    f"{SOURCE_TREE}/llvm/include/llvm/Support/Host.h",
)
EXCLUDED_MEMBERS = (
    f"{SOURCE_TREE}/clang/lib/Sema/SemaDecl.cpp",
    f"{SOURCE_TREE}/lldb/test/API/foo/real/foo.h",
)
# The upstream archive ships these relative links; 7-Zip 26.04 calls them dangerous.
UPSTREAM_TREE_LINKS = {
    f"{SOURCE_TREE}/clang/test/Driver/Inputs/CUDA-symlinks/usr/bin/ptxas": "../../opt/cuda/bin/ptxas",
    f"{SOURCE_TREE}/lldb/test/API/foo/symlink1/foo.h": "../real/foo.h",
}
# A link inside a kept tree must still fail the job closed rather than be skipped silently.
SELECTED_TREE_LINK = (f"{SOURCE_TREE}/llvm/bin/pinned-tool", "../../opt/tool")


def write_source_tar(path: Path, extra_link: tuple[str, str] | None = None) -> Path:
    """Write the upstream-shaped tar fixture, optionally adding one link inside a kept tree."""

    links = dict(UPSTREAM_TREE_LINKS)
    if extra_link is not None:
        links[extra_link[0]] = extra_link[1]
    contents = {member: b"payload\n" for member in (*REQUIRED_MEMBERS, *EXCLUDED_MEMBERS)}
    contents.update({member: b"" for member in links})
    with tarfile.open(path, "w", format=tarfile.GNU_FORMAT) as tar:
        for member, payload in contents.items():
            info = tarfile.TarInfo(member)
            if member in links:
                info.type = tarfile.SYMTYPE
                info.linkname = links[member]
                info.mode = 0o777
                tar.addfile(info)
            else:
                info.size = len(payload)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(payload))
    return path


def tar_extraction_script(workflow: str) -> str:
    """Return the workflow's real tar-extraction snippet: allowlist, archiver argv and exit guard."""

    selector_block = SELECTOR_PATTERN.search(workflow)
    selection = [selector_block.group(0)] if selector_block else []
    parts = selection + [
        INVOCATION_PATTERN.search(workflow).group(0),
        GUARD_PATTERN.search(workflow).group(0),
    ]
    return textwrap.dedent("\n".join(parts))


def run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    """Run a ``pwsh`` snippet and capture its combined output and exit code."""

    return subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class SourceExtractionTests(unittest.TestCase):
    """The provisioning job unpacks the four verified component trees and nothing else."""

    def setUp(self) -> None:
        self.workflow_text = PREP_WORKFLOW.read_text(encoding="utf-8")
        self.script = tar_extraction_script(self.workflow_text)
        self.scratch = Path(tempfile.mkdtemp(prefix="ubsan-source-extract-"))
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.source_root = self.scratch / "source"

    def extract(self, extra_link: tuple[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        tarball = write_source_tar(self.scratch / "fixture.tar", extra_link)
        self.source_root.mkdir(exist_ok=True)
        return run_powershell(
            "$ErrorActionPreference = 'Stop'\n"
            f"$tarball = '{tarball}'\n"
            f"$sourceRoot = '{self.source_root}'\n"
            f"{self.script}\n"
        )

    def test_the_four_required_component_trees_are_extracted_whole(self) -> None:
        result = self.extract()
        self.assertEqual(result.returncode, 0, result.stderr)
        for member in REQUIRED_MEMBERS:
            with self.subTest(member=member):
                self.assertTrue((self.source_root / member).is_file(), member)

    def test_the_unrelated_clang_and_lldb_trees_are_never_extracted(self) -> None:
        result = self.extract()
        self.assertEqual(result.returncode, 0, result.stderr)
        for tree in EXCLUDED_TREES:
            with self.subTest(tree=tree):
                self.assertFalse((self.source_root / SOURCE_TREE / tree).exists(), tree)
        for member in EXCLUDED_MEMBERS:
            with self.subTest(member=member):
                self.assertFalse((self.source_root / member).exists(), member)

    def test_the_archiver_argv_is_exactly_the_four_component_allowlist(self) -> None:
        for tree in REQUIRED_TREES:
            with self.subTest(tree=tree):
                self.assertIn(f"{SOURCE_TREE}/{tree}/*", self.script)
        for tree in EXCLUDED_TREES:
            with self.subTest(tree=tree):
                self.assertNotIn(f"{SOURCE_TREE}/{tree}/", self.script)

    def test_a_link_inside_a_kept_tree_fails_the_job_closed(self) -> None:
        result = self.extract(extra_link=SELECTED_TREE_LINK)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(GUARD_MESSAGE, result.stderr)

    def test_the_verified_asset_guard_precedes_the_selected_extraction(self) -> None:
        extraction = self.workflow_text.index("& 7z x $tarball")
        self.assertLess(self.workflow_text.index("Pinned LLVM source SHA-256 validation failed."), extraction)
        self.assertLess(self.workflow_text.index("Unexpected LLVM source asset size."), extraction)
        self.assertLess(extraction, self.workflow_text.index("Unexpected LLVM source layout."))


class ExtractionReaderTests(unittest.TestCase):
    """The reader reflects the workflow's real argv instead of injecting its own allowlist."""

    def test_a_workflow_without_a_allowlist_yields_a_whole_archive_extraction(self) -> None:
        legacy = textwrap.dedent(
            """\
                  - name: Rebuild the UBSan C++ runtime with the DLL CRT
                    shell: pwsh
                    run: |
                      & 7z x $tarball "-o$sourceRoot" '-y'
                      if ($LASTEXITCODE -ne 0) { throw 'LLVM source tar extraction failed.' }
            """
        )
        self.assertEqual(
            tar_extraction_script(legacy),
            "& 7z x $tarball \"-o$sourceRoot\" '-y'\n"
            "if ($LASTEXITCODE -ne 0) { throw 'LLVM source tar extraction failed.' }",
        )

    def test_the_reader_keeps_the_allowlist_before_the_archiver_argv(self) -> None:
        script = tar_extraction_script(PREP_WORKFLOW.read_text(encoding="utf-8"))
        self.assertLess(script.index("$sourceTrees = @("), script.index("& 7z x $tarball"))
        self.assertIn("@sourceTrees", script)


if __name__ == "__main__":
    unittest.main()

"""The official x64 UBSan C++ runtime is rebuilt from verified source with the DLL CRT.

The matched LLVM 21.1.8 toolchain ships ``clang_rt.ubsan_standalone_cxx-x86_64.lib`` built with
the static CRT, which cannot link into our ``/MD`` UBSan executables. compiler-rt's top-level
``CMakeLists.txt`` forces ``CMAKE_MSVC_RUNTIME_LIBRARY MultiThreaded`` for the whole runtime while
``sanitizer_common`` overrides it back to the DLL CRT only for its own subdirectory. The
provisioning job downloads the hash-verified official source, patches that single literal, builds
only that one archive through the official runtimes entrypoint, replaces only the x64 C++ library
and records provenance.

These tests execute the real decision logic (source patch, CRT-directive guard, diagnosis guard,
toolset-environment guard, cached-provenance guard) against controlled fixtures and pin the
observable provisioning contract.  No LLVM is downloaded, built or installed here; only harmless
``pwsh`` snippets and repository metadata are used.
"""

from __future__ import annotations

import re
from pathlib import Path
import subprocess
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
PREP_WORKFLOW = REPOSITORY / ".github" / "workflows" / "ci-instrumentation.yml"

SOURCE_ASSET = "llvm-project-21.1.8.src.tar.xz"
SOURCE_SHA = "4633a23617fa31a3ea51242586ea7fb1da7140e426bd62fc164261fe036aa142"
SOURCE_SIZE = "158958576"
RUNTIME_PATCH = "ubsan-runtime-md-crt-v1"
PATCH_FROM = "set(CMAKE_MSVC_RUNTIME_LIBRARY MultiThreaded)"
PATCH_TO = "set(CMAKE_MSVC_RUNTIME_LIBRARY MultiThreadedDLL)"
X86_SHA = "0492c5c292e515ebbbc2533671e54c5895fe209e534812313f64257407233181"
X64_SHA = "7a5386c26497db1691f320121e5b113364dd0274b98e55f15f4dbc00c0450113"
CACHE_KEY = (
    "llvm-instrumentation-21.1.8-lld-link-v1-"
    f"{X86_SHA}-{X64_SHA}-{RUNTIME_PATCH}-{SOURCE_SHA[:16]}-"
    "v145-${{ steps.ubsan-build-environment.outputs.cache_environment }}"
)

SELECT_STEP = "Select the UBSan runtime build environment"
SELECT_ID = "ubsan-build-environment"
REBUILD_STEP = "Rebuild the UBSan C++ runtime with the DLL CRT"
VERIFY_STEP = "Verify the pinned LLVM 21.1.8 toolchain"
PROVENANCE_NAME = "ubsan-runtime-provenance.json"
PATCH_FUNCTION = "Set-UbsanRuntimeSourcePatch"
DIRECTIVE_FUNCTION = "Assert-UbsanRuntimeCrtDirectives"
DIAGNOSIS_FUNCTION = "Assert-UbsanRuntimeDiagnosis"
TOOLSET_ENV_FUNCTION = "Assert-UbsanToolsetEnvironment"
PROVENANCE_FUNCTION = "Assert-UbsanRuntimeProvenance"
PROBE_LINKER_FUNCTION = "Assert-UbsanProbeLinker"


def step_block(workflow: str, name: str) -> str:
    """Return the line block of one ``- name:`` step, leading indentation included."""

    lines = workflow.splitlines()
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped != f"- name: {name}":
            continue
        indent = line[: len(line) - len(stripped)]
        block = [line]
        for follow in lines[index + 1:]:
            if follow.startswith(f"{indent}- name: "):
                break
            block.append(follow)
        return "\n".join(block)
    raise AssertionError(f"workflow step not found: {name}")


def function_block(workflow: str, name: str) -> str:
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


def run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    """Run a harmless ``pwsh`` snippet and capture its combined output and exit code."""

    return subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def function_runner(workflow: str, name: str, call: str) -> subprocess.CompletedProcess[str]:
    return run_powershell(
        "$ErrorActionPreference = 'Stop'\n"
        f"{function_block(workflow, name)}\n"
        f"{call}\n"
    )


class HelperTests(unittest.TestCase):
    """The extraction helpers back every behavioral assertion, so pin both of their shapes."""

    def setUp(self) -> None:
        self.workflow = PREP_WORKFLOW.read_text(encoding="utf-8")

    def test_step_block_returns_the_named_step_and_fails_on_an_unknown_name(self) -> None:
        block = step_block(self.workflow, REBUILD_STEP)
        self.assertIn(f"- name: {REBUILD_STEP}", block)
        self.assertEqual(block.count("- name:"), 1)
        with self.assertRaises(AssertionError):
            step_block(self.workflow, "a step that does not exist")

    def test_step_block_returns_a_terminal_step_up_to_the_end_of_the_file(self) -> None:
        # The following-lines loop can reach EOF without meeting another ``- name:``, so a step
        # that is last in the file must still return its exact body and nothing before it.
        preceding = "      - name: A preceding step\n        run: echo preceding\n"
        terminal = (
            "      - name: The requested terminal step\n"
            "        uses: actions/checkout@v4\n"
            "        with:\n"
            "          fetch-depth: 1\n"
        )
        header_only = "      - name: A trailing header-only step\n"
        for name, expected, tail in (
            ("The requested terminal step", terminal, terminal),
            ("A trailing header-only step", header_only, header_only),
        ):
            with self.subTest(step=name):
                block = step_block(
                    "jobs:\n  build:\n    steps:\n" + preceding + tail, name
                )
                self.assertEqual(block, expected.rstrip("\n"))
                self.assertEqual(block.count("- name:"), 1)
                self.assertNotIn("A preceding step", block)

    def test_function_block_returns_the_definition_and_fails_on_missing_or_unbalanced(self) -> None:
        definition = function_block(self.workflow, PATCH_FUNCTION)
        self.assertIn(f"function {PATCH_FUNCTION}", definition)
        self.assertTrue(definition.rstrip().endswith("}"))
        with self.assertRaises(AssertionError):
            function_block(self.workflow, "Set-NoSuchFunction")
        with self.assertRaises(AssertionError):
            function_block("          function Broken {\n", "Broken")


class SourcePatchTests(unittest.TestCase):
    """The single static-CRT literal is patched fail-closed."""

    def setUp(self) -> None:
        self.workflow = PREP_WORKFLOW.read_text(encoding="utf-8")

    def _patch(self, text: str) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "CMakeLists.txt"
            target.write_text(text, encoding="utf-8")
            result = function_runner(
                self.workflow, PATCH_FUNCTION,
                f"{PATCH_FUNCTION} -Path '{target}'\n"
                f"Get-Content -Raw -LiteralPath '{target}'",
            )
            return result, target.read_text(encoding="utf-8")

    def test_exactly_one_literal_is_rewritten_to_the_dll_crt(self) -> None:
        # A file that already carries the Debug spelling alongside the static literal must have
        # exactly the static literal rewritten and every other byte preserved.
        debug = "  set(CMAKE_MSVC_RUNTIME_LIBRARY MultiThreadedDebug)\n"
        original = (
            "cmake_policy(SET CMP0091 NEW)\n"
            "if (MSVC)\n"
            "  set(CMAKE_MSVC_RUNTIME_LIBRARY MultiThreaded)\n"
            f"{debug}"
            "endif()\n"
        )
        result, patched = self._patch(original)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(patched, original.replace(PATCH_FROM, PATCH_TO))
        self.assertIn(debug, patched)
        self.assertNotIn(PATCH_FROM, patched)

    def test_zero_matches_fail_closed(self) -> None:
        # An input that already carries the DLL spelling, or no CRT literal at all, has nothing to
        # patch: the guard must fail closed and leave the file byte for byte unchanged.
        for original in (
            "set(CMAKE_MSVC_RUNTIME_LIBRARY MultiThreadedDLL)\n",
            "project(compiler-rt LANGUAGES C CXX)\n",
        ):
            with self.subTest(original=original):
                result, patched = self._patch(original)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Expected exactly one", result.stderr)
                self.assertEqual(patched, original)

    def test_ambiguous_matches_fail_closed(self) -> None:
        duplicate = PATCH_FROM + "\n" + PATCH_FROM + "\n"
        result, patched = self._patch(duplicate)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("found 2", result.stderr)
        self.assertEqual(patched, duplicate)


class CrtDirectiveGuardTests(unittest.TestCase):
    """The rebuilt archive must declare the DLL CRT and never the static one."""

    def setUp(self) -> None:
        self.workflow = PREP_WORKFLOW.read_text(encoding="utf-8")

    def _check(self, directives: str) -> subprocess.CompletedProcess[str]:
        return function_runner(
            self.workflow, DIRECTIVE_FUNCTION,
            f"{DIRECTIVE_FUNCTION} -Directives '{directives}'",
        )

    def test_dll_crt_archive_is_accepted(self) -> None:
        directives = 'LINK : /FAILIFMISMATCH:"RuntimeLibrary=MD_DynamicRelease" /DEFAULTLIB:msvcrt'
        result = self._check(directives)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_static_crt_archive_is_rejected(self) -> None:
        directives = 'LINK : /FAILIFMISMATCH:"RuntimeLibrary=MT_StaticRelease" /DEFAULTLIB:libcmt'
        result = self._check(directives)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("static CRT", result.stderr)

    def test_archive_without_the_dll_marker_is_rejected(self) -> None:
        result = self._check("/DEFAULTLIB:msvcrt\nFile Type: LIBRARY")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not declare the DLL CRT", result.stderr)


class DiagnosisGuardTests(unittest.TestCase):
    """A signed-overflow probe must be diagnosed, not confused with any other nonzero exit."""

    def setUp(self) -> None:
        self.workflow = PREP_WORKFLOW.read_text(encoding="utf-8")

    def _check(self, exit_code: int, output: str) -> subprocess.CompletedProcess[str]:
        return function_runner(
            self.workflow, DIAGNOSIS_FUNCTION,
            f"{DIAGNOSIS_FUNCTION} -ExitCode {exit_code} -Output '{output}'",
        )

    def test_ubsan_overflow_report_is_accepted(self) -> None:
        output = "runtime error: signed integer overflow: 2147483647 + 1 cannot be represented"
        result = self._check(1, output)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_successful_exit_is_rejected(self) -> None:
        result = self._check(0, "undiagnosed")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exited successfully", result.stderr)

    def test_a_nonzero_exit_without_a_diagnostic_is_rejected(self) -> None:
        result = self._check(3, "probe crashed")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("without a UBSan diagnostic", result.stderr)

    def test_a_diagnostic_that_is_not_an_overflow_is_rejected(self) -> None:
        result = self._check(1, "runtime error: load of misaligned address")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a signed-overflow report", result.stderr)


class ToolsetEnvironmentGuardTests(unittest.TestCase):
    """The imported developer environment must be the exact selected servicing toolset."""

    def setUp(self) -> None:
        self.workflow = PREP_WORKFLOW.read_text(encoding="utf-8")

    def _check(self, expected_version: str, expected_root: str,
               actual_version: str, actual_root: str) -> subprocess.CompletedProcess[str]:
        return function_runner(
            self.workflow, TOOLSET_ENV_FUNCTION,
            f"{TOOLSET_ENV_FUNCTION} -ExpectedVersion '{expected_version}' "
            f"-ExpectedRoot '{expected_root}' -ActualVersion '{actual_version}' "
            f"-ActualRoot '{actual_root}'",
        )

    def test_matching_toolset_is_accepted_even_with_a_trailing_separator(self) -> None:
        result = self._check(
            "14.51.36231", r"C:\VS\VC\Tools\MSVC\14.51.36231",
            "14.51.36231", r"C:\VS\VC\Tools\MSVC\14.51.36231" + "\\",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_wrong_servicing_version_is_rejected(self) -> None:
        result = self._check(
            "14.51.36231", r"C:\VS\VC\Tools\MSVC\14.51.36231",
            "14.51.36230", r"C:\VS\VC\Tools\MSVC\14.51.36231",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VCToolsVersion", result.stderr)

    def test_a_wrong_toolset_root_is_rejected(self) -> None:
        result = self._check(
            "14.51.36231", r"C:\VS\VC\Tools\MSVC\14.51.36231",
            "14.51.36231", r"C:\VS\VC\Tools\MSVC\14.51.36231\backup",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VCToolsInstallDir", result.stderr)

    def test_a_prefix_sibling_root_is_rejected(self) -> None:
        # A descending substring family selection can land on this sibling, which a plain prefix
        # compare would wrongly accept, so the guard compares normalized paths exactly.
        result = self._check(
            "14.51.36231", r"C:\VS\VC\Tools\MSVC\14.51.36231",
            "14.51.36231", r"C:\VS\VC\Tools\MSVC\14.51.362311",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VCToolsInstallDir", result.stderr)


class ProvenanceGuardTests(unittest.TestCase):
    """Cached provenance must describe the toolset and compiler the current runner selected."""

    CURRENT_VERSION = "14.51.36231"
    CURRENT_IDENTITY = "clang version 21.1.8"
    STANDALONE_HASH = "AA"
    CXX_HASH = "BB"

    def setUp(self) -> None:
        self.workflow = PREP_WORKFLOW.read_text(encoding="utf-8")

    def _record(self, **overrides: str) -> str:
        fields = {
            "toolsetVersion": self.CURRENT_VERSION,
            "compilerIdentity": self.CURRENT_IDENTITY,
            "standaloneSha256": self.STANDALONE_HASH,
            "rebuiltCxxSha256": self.CXX_HASH,
        }
        fields.update(overrides)
        body = "; ".join(f"{key}='{value}'" for key, value in fields.items())
        return f"[pscustomobject]@{{ {body} }}"

    def _check(self, record: str) -> subprocess.CompletedProcess[str]:
        return function_runner(
            self.workflow, PROVENANCE_FUNCTION,
            f"$record = {record}\n"
            f"{PROVENANCE_FUNCTION} -Record $record -CurrentToolsetVersion '{self.CURRENT_VERSION}' "
            f"-CurrentCompilerIdentity '{self.CURRENT_IDENTITY}' "
            f"-StandaloneHash '{self.STANDALONE_HASH}' -CxxHash '{self.CXX_HASH}'",
        )

    def test_matching_provenance_is_accepted(self) -> None:
        result = self._check(self._record())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_cached_toolset_mismatch_is_rejected(self) -> None:
        result = self._check(self._record(toolsetVersion="14.51.36230"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("toolset", result.stderr)

    def test_a_cached_compiler_identity_mismatch_is_rejected(self) -> None:
        result = self._check(self._record(compilerIdentity="clang version 21.1.7"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compiler identity", result.stderr)

    def test_a_changed_standalone_archive_is_rejected(self) -> None:
        result = self._check(self._record(standaloneSha256="CC"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("standalone archive changed", result.stderr)

    def test_a_changed_cxx_archive_is_rejected(self) -> None:
        result = self._check(self._record(rebuiltCxxSha256="DD"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C++ archive does not match", result.stderr)


class WorkflowContractTests(unittest.TestCase):
    """The provisioning and verification contract an independent reviewer can check."""

    def setUp(self) -> None:
        self.prep = PREP_WORKFLOW.read_text(encoding="utf-8")
        self.select = step_block(self.prep, SELECT_STEP)
        self.rebuild = step_block(self.prep, REBUILD_STEP)
        self.verify = step_block(self.prep, VERIFY_STEP)

    def test_the_source_asset_is_hash_and_size_verified_before_extraction(self) -> None:
        for token in (SOURCE_ASSET, SOURCE_SHA, SOURCE_SIZE, "gh release download llvmorg-21.1.8",
                      "Get-FileHash", "SHA256"):
            with self.subTest(token=token):
                self.assertIn(token, self.rebuild)
        self.assertLess(self.rebuild.index("Get-FileHash"), self.rebuild.index("& 7z x $asset"))
        self.assertLess(self.rebuild.index("& 7z x $asset"), self.rebuild.index("& 7z x $tarball"))

    def test_the_official_runtimes_entrypoint_builds_only_the_x64_cxx_archive(self) -> None:
        for token in (
            "$source = Join-Path $sourceRoot 'llvm-project-21.1.8.src'",
            "-S (Join-Path $source 'runtimes') -B $ownedBuild -G Ninja",
            "-DCMAKE_C_COMPILER=$clangCl", "-DCMAKE_CXX_COMPILER=$clangCl",
            "-DCMAKE_LINKER=$lldLink", "-DCMAKE_AR=$libExe",
            "-DCMAKE_C_COMPILER_TARGET=x86_64-pc-windows-msvc",
            "-DCMAKE_CXX_COMPILER_TARGET=x86_64-pc-windows-msvc",
            "-DLLVM_DEFAULT_TARGET_TRIPLE=x86_64-pc-windows-msvc",
            "-DLLVM_ENABLE_RUNTIMES=compiler-rt", "-DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=OFF",
            "-DCOMPILER_RT_DEFAULT_TARGET_ONLY=ON", "-DCOMPILER_RT_BUILD_SANITIZERS=ON",
            "'-DCOMPILER_RT_SANITIZERS_TO_BUILD='",
            "-DCOMPILER_RT_BUILD_BUILTINS=OFF", "-DCOMPILER_RT_BUILD_LIBFUZZER=OFF",
            "-DCOMPILER_RT_BUILD_PROFILE=OFF", "-DCOMPILER_RT_BUILD_XRAY=OFF",
            "-DCOMPILER_RT_INCLUDE_TESTS=OFF",
            "cmake --build $ownedBuild --target clang_rt.ubsan_standalone_cxx-x86_64 --parallel 2",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.rebuild)
        # No homegrown compiler or source list and no fake LLVM config may appear.
        for forbidden in ("LLVM_CONFIG_PATH", "COMPILER_RT_DEFAULT_TARGET_TRIPLE", "LLVM_RUNTIMES_BUILD"):
            self.assertNotIn(forbidden, self.rebuild)

    def test_only_the_unique_x64_cxx_archive_is_replaced(self) -> None:
        self.assertIn("$cxx = Join-Path $windowsLibs 'clang_rt.ubsan_standalone_cxx-x86_64.lib'", self.rebuild)
        self.assertIn("$standalone = Join-Path $windowsLibs 'clang_rt.ubsan_standalone-x86_64.lib'", self.rebuild)
        self.assertIn("Get-ChildItem -LiteralPath $ownedBuild -Recurse -File -Filter 'clang_rt.ubsan_standalone_cxx-x86_64.lib'", self.rebuild)
        self.assertIn("if ($rebuilt.Count -ne 1)", self.rebuild)
        self.assertIn("Copy-Item -LiteralPath $rebuilt[0].FullName -Destination $cxx -Force", self.rebuild)
        self.assertEqual(self.rebuild.count("Copy-Item "), 1)

    def test_the_select_step_matches_the_driver_toolset_policy(self) -> None:
        # The one selection step mirrors the driver's VS18 install and exact numeric v145
        # servicing choice, and imports the toolset's full version rather than the family flag.
        for token in ("vswhere.exe", "'[18.0,19.0)'", "^14\\.51(?:\\.\\d+)+$",
                      "vcvarsall.bat", "$sdkVersion = '10.0.26100.0'",
                      "-vcvars_ver=$($toolset.Name)"):
            with self.subTest(token=token):
                self.assertIn(token, self.select)
        # The rebuild consumes that selection from the persisted environment and keeps its own
        # build-prerequisite gate; neither step may bind PowerShell's read-only ``$HOME``.
        for token in ("$toolsetRoot = $env:VCToolsInstallDir",
                      "foreach ($prerequisite in @('cmake', 'ninja'))", "Get-Command $prerequisite"):
            with self.subTest(token=token):
                self.assertIn(token, self.rebuild)
        self.assertNotIn("$home", self.select)
        self.assertNotIn("$home", self.rebuild)
        # The family flag would be selected by descending substring and could pick a backup or
        # wrong servicing toolset, so it must never appear.
        self.assertNotIn("-vcvars_ver=14.51", self.select)
        self.assertNotIn("-vcvars_ver=14.51", self.rebuild)

    def test_the_select_step_publishes_the_exact_toolset_and_env_outputs(self) -> None:
        self.assertIn(f"id: {SELECT_ID}", self.select)
        self.assertIn("Assert-UbsanToolsetEnvironment -ExpectedVersion $toolset.Name", self.select)
        # ``GITHUB_OUTPUT`` lines are emitted as double-quoted strings so PowerShell interpolates
        # the resolved toolset name and SDK version instead of writing the raw expression.
        for token in ('"toolset_version=$($toolset.Name)"',
                      '"cache_environment=$($toolset.Name)-sdk$sdkVersion"'):
            with self.subTest(token=token):
                self.assertIn(token, self.select)
        # Only the named developer variables are persisted; the inherited dev shell is never
        # dumped wholesale into GITHUB_ENV, which is protected from runner/GitHub secrets.
        self.assertIn("$env:GITHUB_ENV", self.select)
        self.assertIn("$env:GITHUB_OUTPUT", self.select)
        self.assertIn("$persisted = @(", self.select)
        for variable in ("'VCToolsVersion'", "'VCToolsInstallDir'", "'INCLUDE'", "'LIB'", "'LIBPATH'"):
            with self.subTest(variable=variable):
                self.assertIn(variable, self.select)
        for forbidden in ("Get-ChildItem Env:", "Get-ChildItem env:"):
            self.assertNotIn(forbidden, self.select)

    def test_provenance_records_source_patch_and_archive_hashes(self) -> None:
        for token in (PROVENANCE_NAME, "schema = 'unpackrat/ubsan-runtime-provenance'",
                      "sourceAssetSha256 = $sourceSha", "runtimePatch = $runtimePatch",
                      "originalCxxSha256 = $originalCxxHash", "standaloneSha256 = $standaloneHash",
                      "rebuiltCxxSha256 =", "toolsetVersion = $env:VCToolsVersion",
                      "compilerIdentity = $compilerIdentity"):
            with self.subTest(token=token):
                self.assertIn(token, self.rebuild)
        self.assertLess(self.rebuild.index("$standaloneHash = "), self.rebuild.index("Copy-Item "))

    def test_the_rebuild_is_gated_on_a_cache_miss_and_the_verify_step_never_is(self) -> None:
        self.assertIn("if: steps.llvm-instrumentation.outputs.cache-hit != 'true'", self.rebuild)
        self.assertNotIn("if:", self.verify)
        self.assertNotIn("cache-hit", self.verify)
        self.assertLess(self.prep.index(REBUILD_STEP), self.prep.index(VERIFY_STEP))

    def test_the_cache_key_encodes_source_digest_patch_and_resolved_toolset(self) -> None:
        keys = re.findall(r"^          key: (llvm-instrumentation-.*)$", self.prep, re.MULTILINE)
        self.assertEqual(keys, [CACHE_KEY] * 2)
        for component in (X86_SHA, X64_SHA, RUNTIME_PATCH, SOURCE_SHA[:16], "v145",
                          "${{ steps." + SELECT_ID + ".outputs.cache_environment }}"):
            with self.subTest(component=component):
                self.assertIn(component, CACHE_KEY)
        # The resolved toolset identity is part of the key and the full key stays well under
        # GitHub's 512-character limit.
        self.assertLess(len(keys[0]), 512)

    def test_the_toolset_is_resolved_before_the_cache_lookup(self) -> None:
        # The cache identity depends on the resolved toolset, so the selection step runs before
        # the cache restore and before the cache-miss rebuild.
        self.assertLess(self.prep.index(SELECT_STEP), self.prep.index("actions/cache/restore@"))
        self.assertLess(self.prep.index(SELECT_STEP), self.prep.index(REBUILD_STEP))

    def test_the_verify_step_recomputes_the_recorded_archive_hashes(self) -> None:
        for token in (PROVENANCE_NAME, "'unpackrat/ubsan-runtime-provenance'", SOURCE_SHA,
                      RUNTIME_PATCH, PATCH_FROM, PATCH_TO, "standaloneSha256", "rebuiltCxxSha256",
                      "Get-FileHash"):
            with self.subTest(token=token):
                self.assertIn(token, self.verify)
        self.assertIn("The official UBSan standalone archive changed.", self.verify)
        self.assertIn("The rebuilt UBSan C++ archive does not match its provenance.", self.verify)

    def test_the_verify_step_inspects_directives_and_compiles_a_ubsan_probe(self) -> None:
        for token in ("dumpbin", "/directives", f"{DIRECTIVE_FUNCTION} -Directives $directives",
                      "'/nologo', '/MD', '/WX', '/EHsc', '/clang:-std=c++23'",
                      "'/clang:-fsanitize=undefined', '/clang:-fno-sanitize-recover=all'",
                      "volatile int high = 2147483647", "volatile int step = sink + 1",
                      "sink = high + step", 'std::strcmp(argv[1], "overflow") == 0',
                      "& $probeExe clean", "$overflow = & $probeExe overflow",
                      f"{DIAGNOSIS_FUNCTION} -ExitCode $LASTEXITCODE -Output $overflow"):
            with self.subTest(token=token):
                self.assertIn(token, self.verify)

    def test_the_probe_names_the_paired_lld_link_and_both_warning_gates(self) -> None:
        # clang-cl defaults to the MSVC link.exe, which ignores our /MD runtime, so the probe
        # points the driver's -B search at the paired bin, asks for the lld-link family and gates
        # warnings on both the compile and the link edge.  The Windows 21 driver rejects a generic
        # --ld-path and prefixes the target triple to an absolute -fuse-ld path, so neither may
        # appear; the -### dry run must prove the exact paired linker before the identical link.
        for token in (
            "$pairedBin = Join-Path $llvmCompilerHome 'bin'",
            "$pairedLld = Join-Path $pairedBin 'lld-link.exe'",
            '"/clang:-B$pairedBin/"',
            "/clang:-fuse-ld=lld",
            "'/nologo', '/MD', '/WX', '/EHsc', '/clang:-std=c++23'",
            "'/link', '/WX'",
            "$driverArguments = @(",
            "$dryRunOutput = (& $pairedClang '-###' @driverArguments 2>&1 | Out-String)",
            f"{PROBE_LINKER_FUNCTION} -Output $dryRunOutput -ExpectedLinker $pairedLld",
            "& $pairedClang @driverArguments",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.verify)
        self.assertNotIn("--ld-path", self.verify)
        # The object file is written under the probe directory, never the repository root.
        self.assertIn("$probeObject = Join-Path $probeDirectory 'probe.obj'", self.verify)
        self.assertIn("/Fo:$probeObject", self.verify)

    def test_the_verify_step_binds_the_cached_provenance_to_the_current_selection(self) -> None:
        for token in (
            PROVENANCE_FUNCTION,
            "$toolsetRoot = $env:VCToolsInstallDir",
            "Join-Path $toolsetRoot 'bin/Hostx64/x64/dumpbin.exe'",
            f"{PROVENANCE_FUNCTION} -Record $record -CurrentToolsetVersion $env:VCToolsVersion",
            "-StandaloneHash (Get-FileHash -LiteralPath $standalone -Algorithm SHA256).Hash",
            "-CxxHash (Get-FileHash -LiteralPath $cxx -Algorithm SHA256).Hash",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.verify)
        self.assertNotIn("-vcvars_ver=14.51", self.verify)
        self.assertNotIn("vswhere.exe", self.verify)


if __name__ == "__main__":
    unittest.main()

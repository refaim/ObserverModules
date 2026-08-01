# Current build-system handoff

This is the working handoff for continuing the MSBuild/toolchain migration in a new Codex chat. It records the
repository state verified on 2026-08-01, what is implemented, and what remains. Treat concrete command evidence below
as authoritative; older counts in `autonomous-work-log.md` are historical snapshots.

## Repository state

- Repository: `C:\Users\Roma\Dev\ObserverModules`
- Branch: `codex/msbuild-toolchain`
- The worktree intentionally contains the complete migration and is not yet committed or pushed.
- `CLAUDE.md` has been replaced by repository-level `AGENTS.md`.
- The old CMake/IDE entry points are being removed. CMake remains acceptable only inside vcpkg ports.
- Do not create a background Goal for this work. In the previous chat Goal cards repeatedly became unavailable.
- Do not install or update software. Ask the owner when another tool is required.
- Keep verbose command output in `.artifacts/*.log` and report only concise results in chat; verbose dynamic-check
  output repeatedly triggered a Codex UI display filter, although commands and filesystem changes continued normally.

## Fixed engineering requirements

- Native MSVC release modules for x86, x64, and ARM64.
- Static CRT (`/MT`) and static third-party dependencies; release packages must not require the VC redistributable or
  adjacent dependency DLLs.
- The console entry point is `build.ps1`/`build.cmd`, with direct MSBuild project files under `build/`.
- `Release` is the shippable optimized-with-symbols configuration: `/O2`, `/GL`, `/LTCG`, `/MT`, `/Zi`, and a full
  linker PDB. There is intentionally no separate `RelWithDebInfo` configuration.
- TDD, 100% production line and branch coverage, mutation testing, deterministic tests, format-aware dynamic testing,
  leak checks, binary inspection, and hermetic CI.
- Clean architecture and strict C/C++ boundaries. Application and test code must not call a C API directly; a C API is
  contained in a dedicated C++ adapter. Owning manual allocation in C++ is forbidden; use RAII and smart pointers.
- No compliance or certification profile is planned.

## Implemented system

- Direct MSBuild graph and the console commands documented by `build.ps1 help`:
  `doctor`, `restore`, `build`, `test`, `source-checks`, `compiler-analysis`, `test-coverage`, `test-asan`,
  `test-ubsan`, `test-leaks`, `fuzz`, `audit-binaries`, `package`, `verify`, and `clean`.
- Static vcpkg triplets for x86/x64/ARM64 plus sanitizer-specific variants. The manifest baseline is
  `9e593bb18ea69cc5095e012465dcd675a822ed0d`; current direct versions are Catch2 3.15.3,
  nlohmann/json 3.12.0#2, xxHash 0.8.3, and zlib 1.3.2#1.
- clang-format, clang-tidy, Cppcheck, PSScriptAnalyzer, MSVC `/analyze`, ASan, clang-cl UBSan, LLVM coverage, libFuzzer,
  UMDH scaffolding, dumpbin, BinSkim, and GitHub CodeQL workflow scaffolding.
- One combined PDB archive per architecture is implemented in the packaging source.
- Hermetic generated fixtures cover Ren'Py RPA 2.0/RPA 3.0, RPG Maker RGSS3A, and Zanzarah PAK. The multi-gigabyte
  corpus at `M:\observer\_test` is optional compatibility/stress input, not a mandatory CI checkout.
- Format-aware executable targets exist for Pickle, Ren'Py, RPG Maker, and Zanzarah. `-FuzzTarget` can select one or
  all targets.
- Resource bounds exist for ABI-representable paths, entry metadata, actual remaining input, and expanded Ren'Py
  metadata. There is no arbitrary whole-archive size limit.
- zstr has been removed. Production zlib calls are contained in `src/core/compression/zlib_codec.cpp`; fixture
  compression is separately contained in `src/tests/support/zlib_fixture.cpp`. Other production/test code sees only
  C++ APIs, so a later decompressor replacement is local.
- Ren'Py, RPG Maker, and Zanzarah share `observer::io::bounded_stream` for checked positioning and exact reads.

## Latest verified evidence

All commands below completed successfully after the latest Pickle regression fix.

| Gate | Result |
| --- | --- |
| MSVC x64 Debug deterministic suite | 35 test cases, 639 assertions |
| LLVM production line coverage | 1126/1126, 100% |
| LLVM production branch coverage | 358/358, 100% |
| LLVM production functions | 91/91, 100% |
| Short Pickle format run | 151,692 executions in 15 seconds |
| Short Ren'Py format run | 345,037 executions in 15 seconds |
| Short RPG Maker format run | 219,383 executions in 15 seconds |
| Short Zanzarah format run | 418,998 executions in 15 seconds |

The short all-format command returned exit code 0. Detailed local evidence is in:

- `.artifacts/regression-test.log`
- `.artifacts/coverage-check.log`
- `.artifacts/format-check.log`
- `.artifacts/coverage/x64/coverage.json`
- `.artifacts/coverage/x64/coverage.lcov`

The dynamic Pickle run found an invalid mark-position transition after the first 100% coverage result. A minimized
unit regression now requires the parser to reject it as a normal parse error, `pop_to_mark()` validates its invariant,
and both 100% coverage and the all-format short run passed again.

## Required next work, in order

1. Run `source-checks` and fix clang-format, Cppcheck, and PSScriptAnalyzer findings introduced by the latest changes.
   Do not weaken rules to make the gate green.
2. Check in a minimized seed for the new Pickle regression, replay every checked-in seed, then define a longer
   all-format CI schedule. Seed the four targets with representative examples derived from the external corpus without
   committing the multi-gigabyte corpus.
3. Rework `test-leaks` and `leak-probe.vcxproj` to test the exact optimized x64 `Release` (`/MT`) DLLs. The current CI
   leak job is mandatory but the script still hard-codes a Debug probe. Expand cases beyond small happy paths to cover
   parse failure, cancellation, read/write failure, repeated DLL lifecycle, and large/sparse metadata workloads.
4. Add package smoke verification: unpack each produced ZIP, assert its exact contents, load the DLL from the unpacked
   package, and exercise the Observer API. Run packaging for x86/x64/ARM64 to prove the new one-PDB-ZIP-per-architecture
   layout. Ensure each module package contains only its own applicable third-party documents.
5. Make `verify` a truthful aggregate gate. It currently omits coverage, sanitizers, leak checks, all-format runs, and
   package smoke. Split build-only ARM64 verification from executable tests when the host cannot run ARM64 binaries;
   `verify -Arch all` must not fail merely because an x64 host cannot execute ARM64.
6. Run the clean build/test matrix: MSVC x86 and x64 Debug/Release tests; ARM64 Debug/Release builds locally and tests on
   the native GitHub runner. Then run ASan x86/x64 and clang-cl UBSan x64 after the latest parser changes.
7. Run `/analyze` and clang-tidy for modules, tests, format targets, and the leak probe on all supported architectures.
   Currently the aggregate analysis graph omits the format targets and leak probe.
8. Finish GitHub report integration. MSVC analysis, Cppcheck, CodeQL, and BinSkim should upload SARIF. clang-tidy still
   needs a reliable SARIF conversion/upload path. Preserve mandatory job semantics even when reports use
   `continue-on-error` for artifact collection.
9. Run `audit-binaries` for x86/x64/ARM64 and verify the exact Release DLLs with dumpbin and BinSkim: `/MT`, expected
   architecture and mitigations, no debug CRT, no unexpected imports, no adjacent dependency DLLs. BA2027 about absent
   SourceLink remains an explicit owner decision, not a silently suppressed result.
10. Implement mutation testing. The leading approach is to extract a portable parser core and run Mull in Linux CI;
    no reached non-equivalent surviving mutant is acceptable. Do not install Mull locally without owner approval.
11. Update the permanent docs with final evidence, inspect the complete diff, commit on `codex/msbuild-toolchain`, push,
    and report any CI-only assumptions that still require the first GitHub run.

After the mandatory gates above, the authorized stretch work is IWYU integration and extraction of a portable parser
core statically linked into the existing module DLLs. This must not add a runtime DLL or change the public Observer ABI.
After that core boundary is established, add a supported WSL2/Linux developer workflow for mutation testing and the
Linux sanitizer/tooling surface that Windows cannot provide. The WSL2 build is an additional quality backend for the
portable core, not a replacement for the shipping MSVC/Windows DLL matrix.

## Known decisions and open questions

- MSBuild remains the native compile/link backend. The Python DAG pilot may replace outer verification orchestration
  only; it must not grow into a direct `cl.exe`/`link.exe` driver. Reconsider a Ninja-backed native prototype only if
  build-only measurements show at least a 15% critical-path opportunity or MSBuild no-op evaluation is both above two
  seconds and above 25% of build-only time.
- Owner decision: the production DAG must expose the smallest safe independent work units instead of wrapping whole
  `build.ps1` gates. This includes project/configuration builds, test shards, analyzer project/translation-unit work,
  SARIF normalization and merge, fuzz targets, leak scenarios/modes, audit tools/modules, and package smoke units.
  Resource pools and isolated write roots—not artificial phase-wide dependencies—must constrain parallelism.
- Keep zlib 1.3.2 for now behind the C++ adapter. Replacing it remains possible, but the removed zstr layer—not zlib
  itself—was the source of the earlier incompatibility.
- The exact metadata budgets are safety controls, not whole-file limits. Revisit values using real corpus statistics,
  but do not remove checked arithmetic and actual-input bounds.
- `vcpkg.json` says `LGPL-3.0-or-later`, while the repository README/license identify GPLv3. The owner must decide the
  correct manifest metadata.
- A test currently exercises defensive handling of a host callback that throws across the DLL boundary. Decide whether
  callbacks should instead be documented as non-throwing and tested through an internal seam.
- UBSan instruments first-party clang-cl code but not the MSVC-built vcpkg dependencies. This limitation must be stated
  accurately in CI evidence.
- MSan/TSan are not mandatory for the current Windows plugin architecture. Reconsider TSan only if meaningful
  concurrent parser code is introduced; use UMDH/ASan and bounded-memory tests for the current memory requirements.
  Once the portable parser core and WSL2 workflow exist, evaluate Linux ASan/UBSan/LSan routinely and add MSan/TSan
  only where their platform and program-model prerequisites make their results meaningful.

## Locally available tools reported by the owner

The owner installed Cppcheck, PSScriptAnalyzer, LLVM/clang tools, MSVC AddressSanitizer, Spectre libraries, and BinSkim
(on `%PATH%`). vcpkg is managed exclusively through Scoop. Do not replace or self-update this setup. IWYU and a local
CodeQL CLI have not been established; GitHub Actions may use the official CodeQL action without a local install.

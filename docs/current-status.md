# Current build-system handoff

This is the working handoff for continuing the MSBuild/toolchain migration in a new Codex chat. It records the
repository state verified on 2026-08-01, what is implemented, and what remains. Treat concrete command evidence below
as authoritative; older counts in `autonomous-work-log.md` are historical snapshots.

## Repository state

- Repository: `C:\Users\Roma\Dev\ObserverModules`
- Branch: `codex/msbuild-toolchain`
- Checkpoint commit `294b584` preserves the complete MSBuild migration and DAG experiments before the approved
  IX-derived replacement work. The branch has not been pushed.
- `CLAUDE.md` has been replaced by repository-level `AGENTS.md`.
- The old CMake/IDE entry points are being removed. CMake remains acceptable only inside vcpkg ports.
- Do not create a background Goal for this work. In the previous chat Goal cards repeatedly became unavailable.
- Do not install or update global/system tools. Project-local Python dependencies may be added and locked with `uv`;
  report missing external developer tools instead of installing them.
- Keep verbose command output in `.artifacts/*.log` and report only concise results in chat; verbose dynamic-check
  output repeatedly triggered a Codex UI display filter, although commands and filesystem changes continued normally.

## Fixed engineering requirements

- Native MSVC release modules for x86, x64, and ARM64.
- Static CRT (`/MT`) and static third-party dependencies; release packages must not require the VC redistributable or
  adjacent dependency DLLs.
- The console entry point is `build.ps1`/`build.cmd`, with direct MSBuild project files under `build/`.
- `Release` is the shippable optimized-with-symbols configuration: `/O2`, `/GL`, `/LTCG`, `/MT`, compiler-embedded
  `/Z7` symbols, and a full linker PDB. `/Z7` removes the shared compiler-PDB service from parallel isolated build
  leaves; the linker still emits the distributable PDB. There is intentionally no separate `RelWithDebInfo`.
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

### IX-derived replacement pilot

- `tools/build` now contains the working Python 3.14/Jinja replacement core: canonical MD5 identities, demand DAG
  execution, named pools, native interprocess locks through `filelock`, repository-local `out/cas`, confined work
  paths, and Windows process-tree cancellation through `psutil` plus `pywin32` Job Objects.
- The project environment exact-pins `coverage==7.15.2`; its blocking gate measures all first-party `core`, `graphs`,
  and CLI code with 100% line and branch coverage and no production exclusions.
- The production analysis graph now covers every supported first-party project/TU occurrence on x86, x64, and ARM64.
  It contains 533 nodes: 262 independent raw analyzer leaves, 262 independent normalizers, three shared restore leaves,
  and per-architecture deterministic merge/semantic gates. The x64-only leak probe is deliberately absent from the
  cross-architecture graphs. Every analyzer has its own object root.
- Raw TU identities currently use a deliberately restricted literal-include closure. Computed includes,
  `#include_next`, and `__has_include` are rejected instead of being under-signed; the first-party path namespace is
  also signed. The planned generic form is a two-phase compiler-derived resolver using MSVC `/sourceDependencies` and
  `clang-scan-deps`, so this bootstrap scanner is not presented as a general C++ preprocessor.
- Official `vswhere.exe` and `VsDevCmd.bat` discovery fingerprints MSBuild 17.14.51.32402, MSVC 14.44.35207,
  clang-tidy 19.1.5, Windows SDK 10.0.26100.0, and the resolved vcpkg root. Discovery does not mutate the parent
  environment or install tools. The signed developer-environment delta deliberately excludes inherited/transport
  `PATH`; runtime overlays append the host path case-insensitively, so CAS identities are stable across launch modes.
- The native graph splits project/configuration/architecture builds and Catch2 shards into isolated CAS leaves.
  x64 Debug and Release matrices have run successfully with four concurrent MSBuild project leaves. Compiler debug
  data uses `/Z7`, avoiding cross-Job `mspdbsrv` RPC failures while the linker still emits a full PDB.
- Fine-grained fuzz and release-binary audit graphs are implemented. Each fuzz target has independent build, corpus
  replay, and nonce-signed timed execution; each module audit has three independent dumpbin leaves, a PE policy gate,
  a BinSkim leaf, and a separate SARIF policy gate.
- The earlier 1,248-line pilot count is obsolete now that real graph families have replaced projections. Re-measure
  production LOC after parity and the required structural reduction; compare the final replacement, not an incomplete
  slice, with the 2,321-line old PowerShell production surface.
- The root `build.ps1` contract has deliberately not switched. Run the pilot from the repository root with:

  ```powershell
  uv run --project tools/build --frozen --no-sync python tools/build/main.py analysis-slice --repository . --arch all
  uv run --project tools/build --frozen --no-sync python tools/build/main.py native --repository . --arch x64 --config Debug
  ```

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
| Replacement Python suite before leak/package completion | 143 tests; 1305 statements and 380 branches, all 100% |
| Complete 191-node x64 graph, first cold run | 134 seconds; one real C26498 finding reached only the semantic gate |
| x64 incremental after the one-line `constexpr` fix | 17.7 seconds |
| Complete 191-node x64 graph, warm | 2.91 seconds |
| Cold addition of x86 and ARM64 | about 200 seconds for 168 new raw analyzer leaves at 12-way concurrency |
| Complete 533-node three-architecture graph, warm | 4.38 seconds |
| x64 Debug native graph, four project leaves and four test shards | cold run green; no C1090 after `/Z7` |
| x64 Release native graph, including leak-probe build | cold run green with `/MT`, `/GL`, `/LTCG`, and linker PDB |
| x64 Debug native graph, warm | 1.66 seconds |

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

1. Complete the in-progress split of source checks, builds/tests, fuzz targets, leak scenarios/modes, audit work,
   packaging, and remaining gates
   into the smallest safe nodes with measured pool capacities.
2. Replace the restricted include-closure bootstrap with the recorded compiler-derived two-phase resolver before the
   CAS is treated as generic for arbitrary future C++ include forms.
3. Preserve the current `build.ps1` implementation and old DAG experiments until the replacement has full result
   parity and measured cold/warm local evidence. Only then switch the entry point. After the switch is verified and
   checkpointed, delete the superseded PowerShell orchestration, experimental DAGs, their legacy-only tests, and stale
   build documentation; retain the MSBuild projects/props/targets because they remain the native backend.
4. After the portable parser core is established, add the documented WSL2/Linux local backend for Mull and the Linux
   sanitizer surface. CI repeats locally runnable commands; it is not the only place those gates may run.

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

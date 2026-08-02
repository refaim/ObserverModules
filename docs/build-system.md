# Build system and engineering workflow

## Status

This document records the agreed target design. The MSBuild migration is implemented alongside it; coverage growth and
additional fuzz targets remain ongoing engineering work.

## Non-negotiable release contract

- Windows-only native C++23, compiled with the MSVC `cl.exe` toolchain.
- Release modules are built for x86, x64, and ARM64.
- The MSVC runtime and all third-party libraries are linked statically (`/MT` and static vcpkg triplets).
- A release archive contains no redistributable runtime and no non-system DLL dependencies.
- CMake is not part of this repository's build graph. vcpkg ports may use CMake internally.
- `/clr` is not used. Managed-library integrations require a separate future design that preserves the native,
  self-contained release contract.

## Layers

```text
build.ps1 / build.cmd        stable command-line entry point
        |
        v
build/build.ps1             environment discovery and task orchestration
        |
        v
build/ObserverModules.proj  aggregate MSBuild targets
        |
        v
build/projects/*.vcxproj    compile/link graph
        |
        v
cl.exe / link.exe / lib.exe / rc.exe
```

PowerShell is not the build engine. The root script is intentionally tiny and contains no source list, compiler flags,
or dependency graph. NMAKE was rejected because it would require hand-maintaining the platform/configuration matrix,
header dependency tracking, vcpkg integration, and project graph while still needing another tool for testing,
coverage, binary auditing, and packaging.

## Supported commands

```powershell
.\build.ps1 doctor
.\build.ps1 restore -Arch x64
.\build.ps1 build -Arch x86,x64,arm64 -Config Release
.\build.ps1 test -Arch x64 -Config Debug
.\build.ps1 source-checks -Arch x86,x64,arm64
.\build.ps1 compiler-analysis -Arch x64
.\build.ps1 test-coverage -Arch x64
.\build.ps1 test-asan -Arch x64
.\build.ps1 test-ubsan -Arch x64
.\build.ps1 test-leaks -Arch x64
.\build.ps1 fuzz -Arch x64 -FuzzTarget all -FuzzSeconds 60
.\build.ps1 audit-binaries -Arch x86,x64,arm64
.\build.ps1 package -Arch x86,x64,arm64
.\build.ps1 verify -Arch x64
```

`build.cmd` is a convenience shim for `cmd.exe`; both entry points execute the same PowerShell implementation.
Use `-FuzzTarget pickle|renpy|rpgmaker|zanzarah` for a focused local regression run; the default `all` runs every
format target.

The IX-derived replacement is intentionally separate until it reaches complete command parity. Its current 533-node
analysis graph runs MSVC `/analyze` and clang-tidy independently per supported project/TU/architecture, normalizes each
report independently, and then executes deterministic per-architecture SARIF merge and semantic clean gates. From the
repository root, run the pinned environment without syncing or downloading:

```powershell
uv run --project tools/build --frozen --no-sync python tools/build/main.py analysis-slice --repository . --arch all
```

Results are printed as exact paths below `out/cas`; a second identical run is served from touch-marker cache entries.
This pilot does not replace any documented `build.ps1` command yet.

`verify` is the complete host-capable aggregate. It builds Debug and Release for every requested architecture, runs
deterministic tests only where the current host can execute them, and then runs source/compiler analysis, coverage,
the supported sanitizer/leak/fuzz gates, binary audit, package-content validation, and package runtime smoke. A
non-native runtime check is reported explicitly as deferred and must be completed on its native CI runner; it is not
reported as executed locally. The verify fuzz phase always covers all four format targets; `-FuzzSeconds` controls its
bounded duration.

## Configurations

| Configuration | Purpose | CRT | Distributed |
|---|---|---|---|
| Debug | development and deterministic tests | `/MTd` | no |
| Release | optimized modules and packages | `/MT` | yes |
| Coverage | clang-cl instrumentation and llvm-cov reporting | `/MTd` | no |
| ASan | AddressSanitizer tests against release-only instrumented dependencies | `/MT` | no |
| UBSan | clang-cl undefined-behavior tests | `/MT` | no |
| Fuzz | libFuzzer plus AddressSanitizer | `/MT` | no |
| Leak | UMDH against optimized x64 Release modules and probe | `/MT` | no |

ASan and fuzzing are supported only on x86 and x64. Their runtime DLLs are test-only dependencies and must never be
copied into release packages.

## Dependency management

Library dependencies are declared by `vcpkg.json` in manifest mode. Repository-owned overlay triplets explicitly set
both `VCPKG_CRT_LINKAGE` and `VCPKG_LIBRARY_LINKAGE` to `static` for all three architectures. The vcpkg baseline is
pinned in source control and packages are restored into architecture-specific directories below
`.artifacts/vcpkg_installed`; global vcpkg integration is not required. Separate install roots keep manifest-mode vcpkg
from pruning another architecture while switching targets.

Normal public commands restore their required dependencies by default. The experimental DAG uses one serial
`restore -RestoreFlavor all` before its parallel fork; `all` prepares both default and ASan dependency flavors (and
skips the unsupported ARM64 ASan flavor). Its later leaf commands pass `-SkipDependencyRestore`, which is reserved for
orchestration that has already completed that prerequisite. Calling `restore` itself with the skip switch is rejected.

Developer tools are not library dependencies and are discovered by `doctor`:

- Visual Studio Build Tools 2022 with MSVC x86/x64 and ARM64 tools plus a Windows SDK;
- PowerShell 7.4 or newer;
- vcpkg;
- LLVM tools (`clang-format` and `clang-tidy`);
- Cppcheck;
- PSScriptAnalyzer for PowerShell sources.

## Compiler and static-analysis policy

Normal compilation uses `/W4 /WX /permissive-`, the conforming preprocessor, correct `__cplusplus`, SDL checks, and
external-header warning suppression. Release additionally enables optimization and link-time code generation.

The blocking analysis stack is:

1. clang-format in check-only mode;
2. MSVC warnings as errors;
3. MSVC native code analysis (`/analyze` through MSBuild);
4. clang-tidy;
5. Cppcheck for the Release configuration of x86, x64, and ARM64;
6. PSScriptAnalyzer for PowerShell files.

PVS-Studio is explicitly out of scope. Include What You Use is deferred: it is useful for direct/minimal include
hygiene, but its Windows mappings, LLVM version coupling, and false-positive cost do not justify making it a gate yet.
Header self-containment checks and clang-tidy's include diagnostics come first.

Cppcheck suppressions must be narrow and documented. Repository-wide suppression of a diagnostic is not acceptable.

### CI analysis evidence

Analysis gates and report publication are deliberately separate. CI lets each analyzer finish, retains its report even
when the gate fails, and only then enforces the analyzer exit status. Cppcheck emits one SARIF file per architecture.
MSVC `/analyze` keeps one raw SARIF file per first-party project and assigns every run a stable
`msvc-analyze/<arch>/<project>/` identity before the architecture directory is uploaded. The clang-tidy logs produced
by that same compile-only graph are converted into a deduplicated first-party SARIF file with the stable identity
`clang-tidy/<arch>/`.

CodeQL uploads through its native action and retains both raw and post-processed SARIF as workflow artifacts. BinSkim
emits and uploads one release-binary SARIF file per architecture. Cppcheck, MSVC, clang-tidy, CodeQL, and BinSkim use
distinct code-scanning categories, so a later upload cannot replace another engine or architecture. Third-party SARIF
uploads are skipped for untrusted fork pull requests where the workflow token cannot write security events; their
reports are still archived as ordinary workflow evidence.

### Future analysis backlog

The following tools are deliberately recorded for later work so that they are not lost while the build and test
architecture is being stabilized:

- **Coverity Scan:** add an independent Windows x64 deep-analysis pass after the repository is eligible and registered
  with the service. Run it on a manual or scheduled cadence rather than as a pull-request gate because submissions are
  external and rate-limited. Keep its project token in CI secrets and treat findings as an additional engine alongside
  CodeQL, not as a replacement for the blocking local analyzers.
- **Infer:** evaluate it only after the portable parser-core boundary can be compiled with Clang on Linux. Start with a
  non-blocking parser-core job and publish its SARIF output; do not add a second build path for the Windows DLL adapters
  merely to accommodate Infer.
- **Include What You Use:** introduce it after parser/header separation has stabilized. Pin an IWYU release compatible
  with the selected LLVM version, generate its compile commands from the canonical build graph, and review suggestions
  rather than applying fixes automatically. It checks direct/minimal include ownership, not runtime correctness.
- **CBMC:** use it for small, security-critical units with explicit bounds, especially the bounded byte reader and
  offset/size/allocation arithmetic. Write focused harnesses that prove properties such as no overflow and no
  out-of-range access; whole-project model checking is not a goal.

PC-lint Plus has been considered and explicitly rejected for this project. Do not add it to the required toolchain or
CI matrix.

## Tests

Catch2 remains the test framework. Tests are split conceptually into:

- deterministic unit tests for parsers, decryption, size/offset arithmetic, and error handling;
- Observer ABI contract tests, including invalid inputs and cancellation;
- small synthetic archive end-to-end tests committed to the repository;
- the large external golden corpus, selected with `OBSERVER_TEST_CORPUS` or `-Corpus`;
- package smoke tests that load the exact binary that will be distributed.

An absent external corpus skips only corpus tests; it must not hide failures in deterministic tests.

### Future parser-core boundary

The current format parsing code is coupled to the Observer DLL adapter, filesystem operations, and parts of the test
harness. A future refactoring should separate a portable parser core from those concerns. This is an architectural
boundary, not a new shared runtime DLL: the core remains statically linked into each self-contained module, and the
published Observer exports and package layout remain unchanged.

The intended responsibilities are:

- the Observer adapter owns the public C ABI, Windows types, module lifetime, callbacks, and error translation;
- the parser core consumes a small bounded byte-source abstraction and produces validated archive metadata and entry
  descriptions;
- format-specific implementations remain independent behind a factory or another narrow dispatch boundary; a virtual
  base class is optional and should be introduced only if it simplifies the actual call sites and tests;
- extraction I/O is kept outside pure metadata parsing where practical, while decompression and format algorithms
  remain testable without loading a plugin DLL.

This boundary is expected to provide the following benefits:

- deterministic unit tests for valid, malformed, truncated, and adversarial inputs without creating files or loading
  DLLs;
- direct fuzz targets for every format parser instead of fuzzing through Observer or a large integration harness;
- substantially cheaper growth toward 100% branch coverage, with failures attributable to a single parser;
- one implementation of bounded reads, checked offset arithmetic, allocation limits, and entry-range validation;
- portable Clang builds of production parsing code for UBSan and LeakSanitizer on a supported non-Windows host;
- smaller test fixtures, reducing dependence on the multi-gigabyte external golden corpus;
- independent testing of the stable Observer ABI adapter against fake parser results and failure injection.

The refactoring does not replace end-to-end tests. ABI contract tests, real DLL loading, package smoke tests, and a
smaller compatibility corpus remain necessary because they cover integration behavior that parser-core tests cannot.

## Coverage

The analysis-only Coverage configuration uses `clang-cl` source instrumentation and `llvm-cov` to enforce 100% lines
and branches in first-party production modules. Functions and regions remain visible in the report but are not separate
release gates. Test framework, Catch2, vcpkg, generated, and Windows SDK code is excluded. Its reports are evidence
about the deterministic test suite, not release artifacts.

Canonical correctness tests and all distributed binaries continue to use MSVC `cl.exe`. The same deterministic tests
must pass under MSVC Debug before their clang-cl coverage result is accepted.

## Sanitizers and fuzzing

The primary ASan path links production core code into a test executable. A dedicated loader smoke test also exercises
the actual instrumented module DLL inside a controlled host process; real FAR/Observer is not a CI dependency.

Windows ASan does not detect memory leaks, and a debug-CRT leak check in the test executable cannot account for all
allocations made by separately linked shipping module DLLs. Leak testing is therefore a separate required layer rather
than an ASan option.

The `test-leaks` operation runs an optimized x64 Release `/MT` probe against the exact Release module DLLs and uses
UMDH from Windows Debugging Tools to compare process-wide heap snapshots. Before measurement it records the loaded
binary paths and hashes, audits their architecture, imports, and exports, and runs an automatic scenario preflight. The
probe then:

1. load every module and perform an unmeasured application warm-up;
2. let the first UMDH attachment enable process-local allocation stack collection, avoiding persistent/elevated GFlags
   registry state, then run another full workload window before accepting a measured baseline;
3. repeatedly exercise successful operations, malformed input, cancellation, read/write failure, and bounded
   large/sparse metadata workloads;
4. repeat the same scenario table in a separate mode that loads and unloads each real DLL on every round;
5. take snapshots after multiple measurement windows and retain diffs as test artifacts;
6. fail when allocation stacks or total live heap bytes show sustained growth across consecutive windows.

The gate must detect a leak slope rather than require a literal zero-byte snapshot difference: the Windows loader,
CRT, symbol engine, and third-party libraries can retain bounded one-time caches. Any allowance must be narrow,
stack-specific, documented, and stable across repeated windows. Debug-CRT checkpoints may provide faster feedback in
unit tests, but they are not accepted as proof that a complete plugin process is leak-free.

After the parser-core boundary exists, a Clang ASan plus LeakSanitizer job on a supported non-Windows host should check
the same core tests and fuzz regression corpus. It complements UMDH rather than replacing it: LeakSanitizer covers the
portable production logic, while UMDH covers the shipped Windows DLLs, static CRT instances, ABI adapter, and loader
lifecycle.

Leak freedom and bounded memory consumption are different requirements. A separate memory-budget stress test should
measure peak private bytes while processing synthetic large/sparse archives and verify that archive contents are not
buffered wholesale. The external real-world corpus remains useful as an opt-in local compatibility and stress layer,
but the required CI leak gate must use small, repository-owned fixtures and finish deterministically.

Fuzzers are standalone executables. They never fuzz through the FAR process. The initial target is the Ren'Py Pickle
parser; archive-index, path, and decompression fuzzers are added as parsing is separated from filesystem I/O. Pull
requests replay every checked-in seed before running each of the four format targets for 30 seconds. The weekly
Saturday 18:17 UTC schedule runs Pickle, Ren'Py, RPG Maker, and Zanzarah for 30 minutes per target (approximately two
hours of coverage-guided execution after the build). Checked-in seeds include minimized regression inputs and compact
representative structures derived from the opt-in external corpus; the external archives themselves are not committed.
Every crash is minimized and committed as a deterministic regression input after triage.

## Release audit and packaging

Before packaging, `dumpbin` verifies:

- the expected x86, x64, or ARM64 PE machine type;
- exactly the Observer exports `LoadSubModule` and `UnloadSubModule`;
- absence of `VCRUNTIME`, `MSVCP`, UCRT, zlib, xxHash, ASan, and other non-system DLL dependencies.

Packaging is rejected if unexpected DLLs, import libraries, or intermediate files enter the staging directory. PDBs
are published separately from module archives.

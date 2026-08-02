# Build system and engineering workflow

## Status

This document describes the implemented local build. The repository uses a Python/Jinja content-addressed DAG for
orchestration and keeps MSBuild as the native compile/link backend.

The IX-aligned build model and automatic CI sections below record implemented contracts. A feature described as
future or deferred is not part of the current gate.

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
build/main.py               command contract and graph composition
        |
        v
build/graphs/*              fine-grained content-addressed DAG
        |
        v
build/projects/*.vcxproj    compile/link graph
        |
        v
cl.exe / link.exe / lib.exe / rc.exe
```

The five-line root PowerShell script only enters the exact-pinned `uv` environment and forwards arguments. Python
builds and executes the outer DAG; short inherited Jinja templates render tool recipes. MSBuild still owns native
project evaluation, compilation, linking, and C++ header dependencies. Replacing that mature backend would duplicate
substantial toolchain behavior without a demonstrated critical-path benefit.

### DAG and content-addressed storage

The outer engine adapts the small model used by [pg83/ix](https://github.com/pg83/ix): a node declares `in_dir`,
`out_dir`, dependencies, data, one resource pool, and a Jinja recipe. Templates use inheritance and `StrictUndefined`;
PowerShell-specific quoting is centralized instead of repeated in every leaf.

Canonical MD5 covers the fully rendered recipe, descriptor/argv data, declared input bytes and paths, dependency UIDs,
toolchain/platform identity, and the executor schema. MD5 is a fast local content identity, not a cryptographic trust
boundary for remote artifacts. A CAS hit requires both the expected entry and its `touch` marker; failed or cancelled
work cannot publish the marker.

`filelock` coordinates node publication and clean operations between cooperating local processes. Mutable paths are
confined below the exact repository output root and existing reparse points are rejected. `psutil` launches and
observes processes; a Windows Job Object terminates the complete descendant tree on failure or cancellation. These are
explicit local-build guarantees, not a claim of hostile-process isolation.

## IX-aligned build model

The goal is not to import IX's package manager. It is to keep the small, proven part that this repository needs: short
inherited recipes, a dependency DAG, demand execution, content identities, and immutable successful outputs. The
native compiler backend remains MSBuild; replacing project evaluation, C++ dependency handling, compilation, and
linking is explicitly outside this stage.

### Recipe model

- One rendered recipe describes one cacheable node: readable name, direct dependencies, `in_dir`, `out_dir`, data,
  one logical pool, and either an argv command or a shell body.
- Jinja inheritance owns command construction and repeated tool policy. The intended hierarchy is a small JSON base,
  an argv or PowerShell base, an MSBuild base where relevant, and a short leaf containing only operation-specific data.
  `StrictUndefined` remains mandatory.
- Graph-building Python enumerates targets and real dependency edges and supplies semantic data. It must stop rebuilding
  the same argv, environment, path, and script boilerplate in every graph family.
- The rendered artifact remains a JSON/argv descriptor. PowerShell is one recipe backend, not the graph language. A
  future local WSL2 implementation can add a POSIX-shell base without changing the node, DAG, or CAS model.
- Typed `results` become part of the recipe contract. Each result has a stable logical id, kind, media type, and a path
  relative to the node output; graph code and CI must not infer results by scanning directories.

### Identity and storage

- Keep canonical MD5 and the zero-byte `touch` publication marker. MD5 identifies local content; any imported or
  downloaded artifact must be authenticated separately.
- The UID covers the rendered recipe, declared input paths and bytes, dependency UIDs, semantic configuration, and a
  normalized toolchain fingerprint.
- Absolute checkout/CAS/export paths, `GITHUB_*`, commit and PR ids, run timestamps, log destinations, `Jobs`, pool
  capacities, and scheduler order do not affect the UID.
- Change the physical successful-entry key to `out/cas/<uid>`. The readable node name remains in graph diagnostics,
  logs, and exported manifests instead of being duplicated in the CAS directory name.
- Keep mutable scratch, locks, leases, failed work, and incomplete-entry quarantine below `out/work`. There is no
  permanent IX-style trash directory: quarantine is recoverable during the run and stale work is removed by the
  existing safe cleanup contract.

### Execution

- Every runnable node consumes one shared `Jobs` slot. A recipe declares at most one additional logical pool, but a
  narrower pool is introduced only for a measured resource limit or a demonstrated tool serialization requirement.
  UMDH and BinSkim have no speculative `2` and `1` caps; by default they use the shared budget like other nodes.
- Preserve all real fine-grained edges and shards: individual translation units/analyzers, test shards, fuzz targets,
  leak scenarios and diffs, binary audits, and packages may overlap whenever their inputs are ready.
- The executor uses keep-going semantics. A failed node blocks only its descendants; independent ready work continues,
  all failures are collected, successfully produced diagnostics remain publishable, and the command finally exits
  nonzero.
- Continue using `filelock`, `psutil`, and the Windows Job Object rather than maintaining substitutes. Prefer a small,
  well-maintained open-source dependency whenever it removes repository code without weakening the contract.

### Public result boundary

`-ExportDir` on verification and packaging commands copies only declared typed results to stable paths such
as `reports/sarif/x64/...`, `reports/coverage/x64/...`, and `packages/x64/...`; it never exposes the internal CAS
layout. The root `manifest.json` describes the self-contained evidence bundle. Successful package exports have a
separate `packages/manifest.json`, whose paths are relative to that bundle, so CI can publish evidence after failure
without publishing release ZIPs from pull requests. Manifests are written after their files and record command status,
result ids, relative paths, producer UIDs, sizes, and SHA-256 digests. Diagnostics produced before a later gate failure
are exported; release packages are exported only after their complete package gates pass. The export destination is
not part of recipe identity.

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
.\build.ps1 verify-source -ExportDir <directory>
.\build.ps1 verify-arch -Arch x64 -ExportDir <directory>
.\build.ps1 verify -Arch x64
.\build.ps1 clean -CleanMode stale-work
```

`build.cmd` is a convenience shim for `cmd.exe`; both entry points execute the same Python driver through the root
PowerShell launcher.
Use `-FuzzTarget pickle|renpy|rpgmaker|zanzarah` for a focused local regression run; the default `all` runs every
format target.

Without `-ExportDir`, commands may print their internal target paths for local diagnostics. Stable consumers use the
typed export boundary; mutable intermediates and locks remain below `out/work`.

`verify` is the complete host-capable aggregate. It builds Debug and Release for every requested architecture, runs
deterministic tests only where the current host can execute them, and then runs source/compiler analysis, coverage,
the supported sanitizer/leak/fuzz gates, binary audit, package-content validation, and package runtime smoke. A
non-native runtime check is reported explicitly as deferred rather than falsely reported as executed. All gates are
designed to be runnable locally on the supported Windows host.
The verify fuzz work covers all four format targets; `-FuzzSeconds` controls each bounded run.

All graph families are merged into one executor. Ready nodes from builds, tests, analyzers, coverage, sanitizers,
fuzzing, leak checks, audit, and packaging may overlap whenever their real dependencies allow it. `-Jobs` sets the
shared global capacity. The implementation has no unmeasured UMDH or BinSkim limits encoded as
scheduler policy.

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
pinned in source control and packages are restored into architecture/flavor-specific CAS nodes; global vcpkg
integration is not required. Separate install roots keep manifest-mode vcpkg from pruning another architecture while
switching targets.

Normal public commands include the exact restore nodes they require. Independent architecture/flavor restores may run
concurrently; ARM64 ASan is omitted because that configuration is unsupported. There is no restore-skipping switch:
restore is an ordinary content-addressed graph node and a valid hit is already a no-op.

Developer tools are not library dependencies and are discovered by `doctor`:

- `uv` and the repository environment initialized once with `uv sync --project build --frozen`;
- Visual Studio Build Tools 2022 with MSVC x86/x64 and ARM64 tools plus a Windows SDK;
- PowerShell 7.4 or newer;
- vcpkg;
- LLVM tools (`clang-format` and `clang-tidy`);
- Cppcheck;
- PSScriptAnalyzer for PowerShell sources.

Normal `build.ps1` commands use `uv --frozen --no-sync`: they neither resolve nor download Python packages while a
build is running. `build/uv.lock` exact-pins Python 3.14.6 and the small runtime dependency set.

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

### Analysis evidence

Analysis work and the semantic gate are deliberately separate. Each analyzer may finish and preserve its report before
the deterministic merge/gate enforces findings. Cppcheck emits one SARIF file per architecture.
MSVC `/analyze` keeps one raw SARIF file per first-party project and assigns every run a stable
`msvc-analyze/<arch>/<project>/` identity before the architecture directory is uploaded. The clang-tidy logs produced
by that same compile-only graph are converted into a deduplicated first-party SARIF file with the stable identity
`clang-tidy/<arch>/`.

BinSkim emits one release-binary SARIF file per architecture. Reports remain separate by analyzer and architecture and
are stored as local CAS evidence. A CI job may repeat these commands, but it must not be the only way to execute or
inspect any mandatory gate.

The main GitHub workflow remains a thin client: it provisions an otherwise empty hosted runner, invokes public local
commands, and transports their declared results. It contains no private gate graph, report parser, CAS-path protocol,
or release logic.

## Automatic CI

CI contains no CI-only quality gate. Everything mandatory in GitHub must remain runnable from an ordinary local
console through the same public entry points:

```powershell
.\build.ps1 verify-source -ExportDir <directory>
.\build.ps1 verify-arch -Arch x86 -ExportDir <directory>
.\build.ps1 verify-arch -Arch x64 -ExportDir <directory>
.\build.ps1 verify-arch -Arch arm64 -ExportDir <directory>
.\build.ps1 verify -Arch all -ExportDir <directory>
```

`verify-source` runs architecture-independent formatting, PowerShell analysis, Python tests/100% coverage, and
repository contracts exactly once. `verify-arch` runs the complete applicable graph for one architecture. The existing
`verify -Arch all` remains the local aggregate and composes source plus every architecture into one executor for
maximum local overlap; the split entry points do not define different gates.

Only two automatic triggers are allowed:

```yaml
on:
  pull_request:
    branches: [master]
  push:
    branches: [master]
```

There is no `workflow_dispatch`, scheduled workflow, or ARM64-native runner. The four required jobs are:

| Job | Public command | Contract |
|---|---|---|
| `source` | `verify-source` | architecture-independent gates |
| `x86` | `verify-arch -Arch x86` | Debug/Release, runnable tests, analysis, ASan, audit, package validation |
| `x64` | `verify-arch -Arch x64` | x86-class gates plus coverage, UBSan, fuzzing, UMDH leaks, and package smoke |
| `arm64-cross` | `verify-arch -Arch arm64` | MSVC cross-build, analysis, binary audit, and package validation |

ARM64 runtime tests are explicitly deferred; a successful cross job must not report them as executed. All four jobs
are required for pull requests and pushes to `master`. GitHub matrix/job fail-fast is disabled, and the graph's own
keep-going behavior preserves independent evidence inside each job. Superseded pull-request runs are cancelled;
`master` runs are never cancelled by a newer push.

Pull requests use the same gates and thresholds with shorter explicit bounded-work parameters, for example a small
`-FuzzSeconds` value and minimal leak warm-up/iterations. Pushes to `master` use the full local defaults. There is no
hidden `pr`/`full` gate composition and no manual profile; changing a bounded duration never removes a target or
weakens the 100% coverage and zero-finding gates.

The workflow caches dependency transport data and completed content-addressed build nodes. CAS caches are isolated by
hosted-runner image and architecture job, restored only within GitHub's branch/ref cache scope, and never shared with
the source job. Node identities still cover recipes, declared inputs, dependency identities, configuration, runtime,
and toolchain content; fuzz and leak nodes include a run nonce. An absent or evicted cache therefore changes only
latency. The workflow never caches `out/work`, packages, or an installed vcpkg tree.

Each job uploads its `-ExportDir` with `if: always()` so reports from completed independent branches survive a later
failure. Pull-request evidence is retained for seven days; `master` evidence for thirty days. Release ZIPs and PDBs are
uploaded only from successful `master` jobs. Actions are pinned to full commit SHAs, permissions default to
`contents: read`, untrusted pull requests receive no secrets, and `pull_request_target` is forbidden.

The workflow first runs `doctor`, then exactly one public verification command. It always uploads the self-contained
evidence bundle and uploads the separate package bundle only from successful `master` architecture jobs. Branch
protection requires `source`, `x86`, `x64`, and `arm64-cross`.

The hosted x64 job selects UMDH from the serviced Windows 10 SDK 2004 line explicitly. This avoids the documented
allocation-stack capture defect in the UMDH shipped with Windows 11 SDKs without changing the locally runnable leak
gate or the SDK used to compile production binaries.

### Future analysis backlog

The following tools are deliberately recorded for later work so that they are not lost while the build and test
architecture is being stabilized:

- **Coverity Scan:** evaluate it only if it can be integrated into the locally runnable verification contract. It is
  not part of the approved CI workflow while submissions require a separate external/manual path.
- **Infer:** evaluate it locally under the future WSL2 parser-core workflow. Do not add a separate CI-only build path
  for the Windows DLL adapters merely to accommodate Infer.
- **Include What You Use:** introduce it after parser/header separation has stabilized. Pin an IWYU release compatible
  with the selected LLVM version, generate its compile commands from the canonical build graph, and review suggestions
  rather than applying fixes automatically. It checks direct/minimal include ownership, not runtime correctness.
- **CBMC:** use it for small, security-critical units with explicit bounds, especially the bounded byte reader and
  offset/size/allocation arithmetic. Write focused harnesses that prove properties such as no overflow and no
  out-of-range access; whole-project model checking is not a goal.

PC-lint Plus has been considered and explicitly rejected for this project. Do not add it to the required toolchain or
local verification matrix.

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
the actual instrumented module DLL inside a controlled host process; real FAR/Observer is not a test dependency.

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
but the required local leak gate uses small, repository-owned fixtures and finishes deterministically.

Fuzzers are standalone executables. They never fuzz through the FAR process. The initial target is the Ren'Py Pickle
parser; archive-index, path, and decompression fuzzers are added as parsing is separated from filesystem I/O. The local
gate replays every checked-in seed and then runs Pickle, Ren'Py, RPG Maker, and Zanzarah independently. Checked-in seeds
include minimized regression inputs and compact representative structures derived from the opt-in external corpus;
the external archives themselves are not committed. Every crash is minimized and committed as a deterministic
regression input after triage.

## Output and cleanup

`out/cas` contains immutable successful node results. `out/work` contains in-flight scratch space, failed-run evidence,
and `.locks`. Successful node scratch is removed immediately. Each active execution holds a run lease; `clean` takes a
coordination lock and refuses to race active runs or node publishers.

`clean -CleanMode stale-work` removes only inactive scratch. `clean -CleanMode all` removes CAS entries, completed run
data, and inactive locks while retaining the minimal coordination-lock skeleton. Both modes validate that every target
is the exact repository `out` layout and reject reparse points or unknown entries.

After the portable parser core is complete, revisit a separate local WSL2 workflow for Linux-only sanitizers and
test-quality experiments. It is not part of the current build graph; shipping modules remain Windows/MSVC artifacts.

## Release audit and packaging

Before packaging, `dumpbin` verifies:

- the expected x86, x64, or ARM64 PE machine type;
- exactly the Observer exports `LoadSubModule` and `UnloadSubModule`;
- absence of `VCRUNTIME`, `MSVCP`, UCRT, zlib, xxHash, ASan, and other non-system DLL dependencies.

Packaging is rejected if unexpected DLLs, import libraries, or intermediate files enter the staging directory. PDBs
are published separately from module archives.

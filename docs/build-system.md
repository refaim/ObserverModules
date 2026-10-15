# Build system

## What this is

A console-first, Windows-only build over MSBuild and pinned manifest-mode vcpkg, kept as small as it
can be while still compiling and testing the real project and producing the release archives. There
is no graph, cache, scheduler, report normaliser, template renderer or typed-result protocol:
`build/main.py` is the whole command contract, and `build/leaks.py` is the one standalone worker for
the UMDH leak probe. MSBuild owns project evaluation, dependencies, scheduling and incremental object
state under the stable `out/native` tree; vcpkg owns dependency resolution.

Philosophy: one operation runs one real tool and that tool's exit code is the verdict. Formatting,
static analysis, coverage and sanitizers are gated by the tool's own failure mode (`clang-format
--Werror`, `cppcheck --error-exitcode=1`, `coverage report --fail-under=100`, `halt_on_error`), not by
a first-party report parser. A missing prerequisite fails closed; a check is never weakened to make a
run pass. Whatever this document does not state is derivable from the code, and the code wins.

Release contract, non-negotiable: Windows-only native C++23 through MSVC `cl.exe`; x86, x64; MSVC
runtime and all third-party libraries linked statically (`/MT`, static vcpkg triplets); a release
archive carries no redistributable runtime and no non-system DLL; CMake is not part of this tree
(vcpkg ports may use it internally); no `/clr`. Each shipped module (`renpy`, `rpgmaker`, `zanzarah`,
`godot`) owns an independent semantic version in `src/modules/<module>/VERSION` and its own
`<module>/vX.Y.Z` release lineage (for example, `godot/vX.Y.Z`); there is no repository release
version.

## Prerequisites

A Windows 11 host with:

- [Visual Studio Build Tools 2026](https://aka.ms/vs/stable/vs_buildtools.exe) (VS18) carrying the
  legacy v143 14.44 toolset for x86 and x64 and its Spectre-mitigated libraries. The v143 piece is the
  `Microsoft.VisualStudio.Component.VC.14.44.17.14.x86.x64` compiler (the toolset family Visual
  Studio Build Tools 2022 introduced) plus its matching `.Spectre` component, for x86 and x64;
- [Visual Studio 2022](https://visualstudio.microsoft.com/vs/older-downloads/) with the
  `Microsoft.VisualStudio.Component.VC.Llvm.Clang` component, whose `VC/Tools/Llvm` tree carries the
  matched LLVM 19.1.5 instrumentation toolchain (the compiler, coverage/profile tools, headers and
  per-architecture runtimes) the coverage, UBSan and fuzz configurations use locally. The ASan
  configurations do not use it: they are built with the MSVC 14.44 toolset and its dynamic ASan
  runtime;
- the [Windows 11 SDK 26100](https://learn.microsoft.com/en-us/windows/apps/windows-sdk/downloads),
  and the Windows 10 SDK 2004 debugging tools that provide the pinned UMDH and GFlags the leak probe
  needs. The 26100 SDK is the settled build input the shipped binaries and the restored dependencies
  are compiled against; no SDK 28000 target is used;
- PowerShell 7.4 or newer, the shell `build.ps1` targets;
- `uv` 0.13.0 to run the pinned
  [Python 3.14.7](https://www.python.org/downloads/release/python-3147/) environment under
  `build/.venv` (coverage 7.16.2, filelock 4.1.1, psutil 7.2.2, pywin32 312). `build.ps1` invokes
  `uv run --frozen --no-sync --no-python-downloads`, so the environment must already exist: it never
  creates the venv, syncs the dependencies or downloads an interpreter;
- `vcpkg` reachable through `VCPKG_ROOT`, used in manifest mode over the pinned dependency set, with
  the executable pinned to the
  [2026-09-26 vcpkg-tool release](https://github.com/microsoft/vcpkg-tool/releases/tag/2026-09-26);
- [clang-tidy and clang-format from LLVM 23.1.3](https://github.com/llvm/llvm-project/releases/tag/llvmorg-23.1.3)
  for the Release analysis and the format check, resolved from the standard `PATH` or selected by the
  `UNPACKRAT_CLANG_TIDY` and `UNPACKRAT_CLANG_FORMAT` executable paths. The driver verifies each
  tool's version and never installs it; there is no private `%LOCALAPPDATA%` tool cache.

The native build and the vcpkg overlay triplets share one v143 14.44 compiler family: the driver
forwards the full installed `VCToolsVersion` instead of hard-coding a toolset folder, the triplets
pin v143 at 14.44, and `VCPKG_VISUAL_STUDIO_PATH` names the same selected Visual Studio so the
compiler and the STL agree with the dependency builds.

The release input is preserved as-is: the VS2026 MSBuild with the v143 14.44 toolset and the 26100 SDK
compile the shipped binaries and the restored dependencies. The `WINVER 0x600` (Windows Vista) target
is a build-time choice only; an actual Windows Vista runtime has not been proved and stays an open
compatibility check rather than an accepted deviation. Every local tool must already be installed:
the driver never downloads or installs a compiler, SDK or analyzer, and a missing prerequisite fails
closed with a plain message. Moving the native build onto the newer v145 toolset together with
VC-LTL5, which would drop the legacy MSVC 14.44 runtime requirement, is deferred and is not a current
build input.

The coverage, UBSan and fuzz variants compile, profile and link against one matched LLVM 19.1.5
family: the `clang-cl` compiler, the `llvm-cov`/`llvm-profdata` tools, the headers and the
per-architecture `compiler-rt` (profile, fuzzer, static ASan and UBSan) libraries. The driver selects
that family from the explicit, absolute `UNPACKRAT_LLVM_INSTRUMENTATION` root CI publishes, or,
locally, from the LLVM 19.1.5 bundled with the Visual Studio 2022 installation; it validates every
tool version, the headers and every required runtime library before it forwards the toolchain to
MSBuild and never borrows a global LLVM from `PATH`. The separately pinned clang-tidy and
clang-format (LLVM 23.1.3) analyze and format Release code. The Release analysis marks the restored
vcpkg include paths as system headers through
`/imsvc` while the ordinary `/I` form of the same path stays a first-party include, and repository
`src` headers are always first-party. That is a real AST-matcher scope decision, not a diagnostics
filter: LLVM still parses and type-checks the external declarations C++ semantics need, only matcher
checks on vcpkg headers are skipped, and our own headers are always checked. Our own headers use
standard include guards (all 48), and the source checks keep a scope proof that feeds one foreign path
both as an ordinary include and through `/imsvc`, with paths containing spaces and a genuine negative
control. The interprocedural analyzer may still follow external call bodies while checking our call sites.

Cppcheck 2.22.0 (the
[official MSI](https://github.com/cppcheck-opensource/cppcheck/releases/tag/2.22.0), verified by
SHA-256), PSScriptAnalyzer 1.25.0 and BinSkim 4.4.9.11 round out the source and native analysis
tools.

Only the tools the stage you run actually needs must be present: `ci-source` needs clang-format,
Cppcheck and PSScriptAnalyzer; the `native-debug`, `native-release` and `msvc-analysis` stages need
the legacy MSVC 14.44 toolset (`native-release` also needs its Spectre libraries, dumpbin and
BinSkim); compile-only `msvc-analysis` retains `/Qspectre` without selecting Spectre libraries.
The `clang-tidy` stage needs the standalone
LLVM 23.1.3 clang-tidy and its parallel runner on top of MSVC; the
coverage, UBSan and fuzz stages need the LLVM 19.1.5 instrumentation family; and the x64 `leaks`
stage needs UMDH over an already-built x64 Release tree. A missing tool fails the run with a plain
message rather than being skipped.

## Map

- `build.ps1` / `build.cmd` — entry point: pinned `uv` environment, rejoins array-valued arguments
  with commas so an unquoted `x86,x64` survives PowerShell parsing, forwards argv.
  `UNPACKRAT_BUILD_PRINT_ARGV=1` prints argv and exits 97.
- `build/main.py` — the public surface and the command contract: `build`, `test`, `package`,
  `coverage_complete`, `run` and `main`, exposed as the CLI subcommands `build`, `test`,
  `ci-source`, `ci-arch` and `package`.
- `build/leaks.py` — the standalone UMDH worker: `setup` stages the built x64 Release binaries and
  `measure` captures one scenario under UMDH, diffs consecutive windows and fails on repeated growth.
- `build/projects/*.vcxproj` + `build/*.props` are MSBuild's half; `.github/workflows/main.yml` is a
  thin client that provisions the tools and calls the public entry points.
- `build/vcpkg/ports/` contains the local zlib-ng compatibility provider and pinned bcdec, drlibs
  and Godot-fork QOA static-library overlays. vcpkg verifies downloaded header hashes before building;
  the decoder implementations no longer expand inside first-party adapter translation units.

## Invariants

- **MSBuild owns incremental state.** Each build asks MSBuild for one project with an explicit
  `ArtifactsRoot` and lets it decide what to rebuild. Python owns no object cache and no dependency
  graph.
- **Explicit output directories.** The native tree is `out/native`; every other operation writes
  under an explicit directory the caller owns.
- **100% lines and branches, in both gates**: first-party C++ under the Coverage configuration, and
  the build system's own python (`build/pyproject.toml`, `fail_under = 100`, `branch = true`, over
  `main.py` and `leaks.py`). The python gate excludes no lines: the `__main__` entry points run for
  real under `tests/test_round2_findings.py`, and `tests/test_coverage_config.py` asserts the gate
  declares no exclusions and the sources carry no coverage pragma.
- **Fail closed.** A nonzero tool exit, a missing prerequisite, a sanitizer finding or an incomplete
  coverage total stops the run; nothing is caught merely to keep going.
- **A commit changing any input of a module's content identity must bump that module's VERSION in the
  same commit**, so the version reported to the Observer host and recorded in the Windows VERSIONINFO
  resource always come from the same bytes as the shipped code.

## Packages and releases

`package` reads `src/modules/<module>/VERSION`, the module's `observer_user.ini`, its two bilingual
`ChangeLog.<lang>.txt` files and the `out/native/bin/<arch>/Release` binaries, and writes the release
archives with the standard library `zipfile`: `<module>-<version>-bin-x86.zip`,
`<module>-<version>-bin-x64.zip` and `<module>-<version>-pdb.zip`. Each module archive carries the
module `.so`, the registration ini, both change logs and a generated `LICENSES.txt` (the project's own
MIT license, the Observer API header's LGPLv3 notice with the full GPLv3 text, and only that module's
own third-party notices). A module release publishes exactly two per-architecture module archives and
one `<module>-<version>-pdb.zip`; the published bundle carries one `<module>.pdb` for each of the two
supported architectures (`x86` and `x64`). A `zipfile.BadZipFile` or any other error propagates; there
is no runtime re-validation or self-audit framework.

The release body is the module's latest English and Russian change log, and the release is published
with a single `gh release create --verify-tag --notes-file` over the three selected archives. Python
holds no GitHub credentials and does no orchestration.

## Running things

```powershell
.\build.ps1 build -Arch x86,x64 -Config Release
.\build.ps1 test -Arch x86,x64 -Config Debug
.\build.ps1 ci-source                 # clang-format, Cppcheck, PSScriptAnalyzer, contracts, python gate
.\build.ps1 ci-arch -Arch x64         # tests, coverage, sanitizers, fuzz, leaks, audit
.\build.ps1 ci-arch -Arch x64 -Stage coverage   # one slice only
.\build.ps1 package                   # the three release archives per module
```

- `-Arch`/`-Config`/`-Module` take comma lists or `all`. Exactly three architecture rejections
  apply: `all` must be selected on its own, an unknown value is refused, and only x86 and x64 exist.
- `ci-source` runs `clang-format --dry-run --Werror`, `cppcheck --error-exitcode=1`,
  PSScriptAnalyzer over the build scripts, the `*.Tests.ps1` contracts, and the python gate
  (`coverage run` then `coverage report --fail-under=100`).
- `ci-arch` builds and runs the Catch2 Debug and Release suites, gates 100% LLVM line and branch
  coverage through `coverage_complete`, runs the ASan (x86/x64) and UBSan (x64) suites with
  `halt_on_error`, runs the bounded x86 replay and x64 timed libFuzzer gates, runs the x64 UMDH leak
  probe, and audits the shipped DLLs' machine, imports and exports with dumpbin.
  Only x86 ASan uses four checked Catch2 shards in fresh processes, preserving every test and the
  same sanitizer options while avoiding cumulative 32-bit address-space pressure.

`ci-arch` also takes a bounded `-Stage` selection, `all` (the default), `native-debug`,
`native-release`, `msvc-analysis`, `clang-tidy`, `coverage`, `asan`, `ubsan`, `fuzz` or `leaks`.
`all` runs every stage in the fixed order `native`, `coverage`, `asan`, `ubsan`, `fuzz`, `leaks`
and prepares the Release trees and the x64 leak probe once. The legacy `native` stage runs the
Debug and Release suites, analysis and binary audits together; CI uses the four real
`native-debug`, `native-release`, `msvc-analysis` and `clang-tidy` slices independently. A
single stage runs only itself: `native-debug` builds and runs the full Debug suite plus the fatal
cleanup probe; `native-release` builds the Release output, runs the Release suite and the
dumpbin/BinSkim audits, and for x64 builds the leak probe; `msvc-analysis` runs the MSVC static
analyzer and `clang-tidy` runs the pinned clang-tidy, each independently restoring the vcpkg headers
and checking all four modules, the tests and the x64 leak probe; and `leaks` is
only the UMDH setup and the mode/scenario measurements over an already-built x64 Release tree, so it
never restores vcpkg, runs MSBuild, BinSkim or LLVM. Standalone `msvc-analysis` keeps one MSBuild
node and visits projects in order; the standard MSBuild MultiToolTask runs up to `-Jobs` compiler
processes within each project, with the process limit enforced across builds. Compiler `/MP` and
CL server mode remain disabled. Every translation unit retains its own SARIF log and the strict
analysis ruleset. Compile-only analysis removes the inherited `/analyze:quiet` option, so the
existing `/WX` fails on findings without requiring the normal build's post-link ingestion.
The `clang-tidy` stage asks MSBuild for fresh
compile-command metadata per project, writes a standard compilation database from every returned
translation unit and runs LLVM's pinned `run-clang-tidy` with `-j` set to the selected `-Jobs` value.
CI passes `-Jobs 4`; local runs use the caller's `-Jobs` value. A missing or empty metadata result
fails before the runner starts. CI uses the pinned local ctcache client for this stage: only clean,
successful analyzer calls may be reused. In cache mode, the compilation database adds
`/clang:-frewrite-includes`, retaining source and included-header directives in the cache input.
Editing one translation unit or its included header invalidates that unit while independent units
remain reusable across commits; configuration, flags and NOLINT changes also invalidate affected
results. Findings still reach the real analyzer. Local runs without an explicit cache path keep the
standard runner unchanged. The `native-debug`, `native-release` and `msvc-analysis` stages never provision or resolve
the standalone LLVM 23.1.3 tools, which only the `clang-tidy` and `source` stages need. The `ubsan`
and `leaks` stages are the x64 specialists and refuse any other selection
up front. The flag exists so CI can run each slice in an independent job; the local default is
unchanged.

## CI

The required check names are the real per-stage jobs, not a gate that re-scores other jobs. Branch
protection requires `source-checks`, then `native-debug-x86`, `native-debug-x64`,
`native-release-x86`, `native-release-x64`, `msvc-analysis-x86`, `msvc-analysis-x64`,
`clang-tidy-x86`, `clang-tidy-x64`, `coverage-x86`, `coverage-x64`, `asan-x86`, `asan-x64`,
`ubsan-x64`, `fuzz-x86`, `fuzz-x64` and `leaks-x64` directly, so no slice waits on another and a
single stage fails on its own. Branch protection no longer lists the single monolithic `native`
stage: its old `native-x86` and `native-x64` checks, which bundled the Debug suite, the Release
suite, the analyzers and the binary audits into one job, are replaced by the eight native/analysis
checks above, and the `-Stage` selection still exposes each of the four `native-debug`,
`native-release`, `msvc-analysis` and `clang-tidy` slices on its own. Every
stage/architecture slice calls one maintained reusable workflow, `.github/workflows/ci-check.yml`,
with its `arch` and `stage`, and that workflow provisions only what the stage needs: the source tools
only for `source`, the standalone LLVM 23.1.3 analyzer/formatter only for `clang-tidy` and `source`,
BinSkim only for `native-release`, UMDH only for `leaks`, the LLVM 19.1.5 instrumentation family
only for `coverage`, `ubsan` and `fuzz`, the pinned Python/uv environment for every stage, and the
legacy MSVC 14.44 toolset plus vcpkg for every MSVC-driven stage (`source`, `native-debug`,
`native-release`, `msvc-analysis`, `clang-tidy`, `coverage`, `asan`, `ubsan`, `fuzz`), with Spectre
libraries only for `native-release`. The `source-checks` row runs `ci-source`;
the rest run `ci-arch -Stage …` for one
architecture. `leaks-x64` depends only on `native-release-x64` and downloads that job's `release-x64`
artifact from the same run — its x64 Release directory already carries the module `.so`/`.pdb` and
`leak-probe.exe`/`.pdb` — and never touches another run's binaries or a cache. A failed, skipped or
cancelled slice therefore fails closed through GitHub's own `needs`, not through a shell decision
table.

The required `instrumentation-tools` preparation check joins these seventeen stage checks. Its tool
exit code determines whether the pinned LLVM toolchain is ready for the five jobs that consume it.

All seventeen checks run on the GitHub-hosted `windows-2025-vs2026` image, whose Visual Studio 2026
(VS18) install carries the same v143 14.44 toolset and standalone LLVM tooling the prerequisites
name. The matched LLVM 19.1.5 instrumentation family is built once by one real `instrumentation-tools`
provisioning job (`./.github/workflows/ci-instrumentation.yml`), which downloads the two official
Visual Studio payloads, hash- and size-verifies them, extracts the compiler, coverage/profile tools,
headers and per-architecture runtimes, verifies the 19.1.5 versions and runtime libraries for both
architectures, caches the verified tree and publishes it as a same-run artifact before the
instrumentation gates run. That job is a provisioning step, not a verdict that re-scores other jobs:
the three LLVM 19.1.5 instrumentation stages (`coverage`, `ubsan`, `fuzz`) depend on it and select
the artifact through `UNPACKRAT_LLVM_INSTRUMENTATION`, while the two ASan stages are the MSVC 14.44
dynamic AddressSanitizer build and never wait on it, and the native, analysis, source and leak stages
never wait on it either. Provisioning for the standalone LLVM 23.1.3 tool tree uses the split
`actions/cache/restore`
and `actions/cache/save` steps so the tool tree is saved directly once its version is verified and
before the `clang-tidy` build, rather than waiting behind a build gate that may fail; a restored tool
cache is re-checked for completeness, for the exact pinned version and for the include-scope proof
before it is trusted.

Triggers: `pull_request` on `master`, `push` on `master` and the module tag prefixes; no
`workflow_dispatch` and no schedule. Every mandatory check stays runnable from a local console through
the same public entry points. The `native-release` slices upload their verified Release binaries as
Actions artifacts; `packaging` (needing all seventeen required checks) consumes them without
rebuilding and writes the module archives; the tag-only `release` job selects exactly the three
archives of the tagged module and publishes them with `gh release create --verify-tag --notes-file`.
Logs and reports are uploaded as Actions artifacts, never as public release assets.

The source gate writes Cppcheck SARIF and each MSVC analysis gate writes per-translation-unit
PREfast SARIF. CI combines the latter with the pinned SARIF Multitool. The clang-tidy gate keeps
its complete diagnostic log; a failed runner still lets the other projects run, and only a full
project pass receives the completion marker required for Code Scanning upload. CI converts the log
to SARIF with the pinned report-converter, combines its reports with SARIF Multitool, and publishes
only real check findings. All three analyzers publish
to [Code Scanning](https://github.com/refaim/unpackrat/security/code-scanning) under distinct source
or architecture categories. The tools' exit codes remain the required check verdicts; the logs and
SARIF files also stay available as Actions artifacts when a gate fails.

## Coverage reports and Codecov

Every first-party gate keeps its tool's own failure mode; the reports exist so a failing run stays
reviewable and so a pull request shows the same evidence.

- The native Coverage configuration still exports the authoritative JSON summary and gates it with
  `coverage_complete` (exact 100% first-party lines and branches). `ci-arch` additionally writes the
  full LCOV export, `out/native/coverage/<arch>/coverage.lcov`, over the same test and module objects
  and the same production filter. `--summary-only` appears only on the JSON export. Both files are
  written before the gate, so an incomplete run still leaves the uncovered lines and branches visible.
- `ci-source` writes the coverage.py Cobertura XML, `out/native/coverage/python/coverage.xml`, before
  the report gate. The XML keeps branch metrics and resolves `<source>` plus `<filename>` back to
  `build/main.py` and `build/leaks.py`; `coverage xml` inherits the configured `fail_under = 100`, so
  it emits the report and then fails, and the gate is never weakened with `--fail-under=0`.

The reusable `ci-check.yml` uploads the coverage reports to Codecov through the pinned official
`codecov/codecov-action` with `disable_search: true` and `fail_ci_if_error: true`: the `source` stage
uploads the python XML under the `python` flag, and each `coverage` stage uploads its own LCOV under
the `native-<arch>` flag. The distinct flags expose a separate per-architecture report; Codecov's
project default combines those flags, so the flags themselves do not enforce a per-architecture
check. Each architecture is enforced by the local strict 100% line-and-branch gates.
Uploads use OIDC (`id-token: write`, propagated through the caller) for same-repo runs and fall back
to Codecov's tokenless public-fork path for a cross-repo pull request; no secret is exposed and there
is no `pull_request_target`. The upload runs even after a gate failure, so the report is published
while the job keeps its original failure. There is no custom comment bot: the Codecov App posts the
comment and the checks.

The vcpkg binary cache key distinguishes the runner image, the architecture, the standard versus ASan
toolchain and the dependency inputs (the manifest plus the overlay ports and triplets), so two
concurrent jobs can never save a mixed or incomplete cache under one key. A tool-version component
joins that key when a provisioning change needs it. CI also caches the pinned standalone LLVM and
clang report tools, plus successful per-translation-unit clang-tidy results through ctcache. No
`out/native` object tree or incremental build output is carried across commits.

`codecov.yml` shows project and patch coverage with a `100%` target and a `0%` threshold, lists the
flags and files in the pull-request comment (`layout: "diff, flags, files"`), and waits for all three
uploads (`after_n_builds: 3`) with no carry-forward flag state. The comment is allowed with only the
head report (`require_head: true`, `require_base: false`, `require_changes: false`), so the first
onboarding pull request does not pretend a baseline exists. The Codecov App must be installed on the
repository, and Codecov's own normalizer resolves the standard XML `<source>` and the LCOV `SF:`
paths, so the build driver carries no first-party report converter.

## Open gaps

Each is a missing gate or a false promise, none an accepted deviation. TDD applies: the failing test
that states the observable requirement comes first.

**№1 — the memory-budget stress gate does not exist.** The UMDH gate compares allocation growth across
windows, which a parser buffering a whole entry and releasing it every round passes. Needed: a gate
failing when peak private bytes scale with archive or entry size instead of the streaming buffer.

**№6 — the instrumentation and standalone tools sit on two deliberate LLVM pins.** The Release
analysis and the format check run on the standalone LLVM 23.1.3 clang-tidy/clang-format, while the
coverage, sanitizer and fuzz variants stay on the matched LLVM 19.1.5 instrumentation family CI
publishes. The split is deliberate, not a claim that every tool tracks the newest LLVM: no single
release that ships a complete static ASan/UBSan/fuzz `compiler-rt` for both x86 and x64 has been
validated as a full replacement yet, so `clang-cl` and `llvm-cov` are not moved onto the standalone
LLVM 23.1.3 archive. Migrate the instrumentation and the standalone tools onto one pinned LLVM
release once that runtime compatibility is verified.

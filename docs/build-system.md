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
(vcpkg ports may use it internally); no `/clr`. Each shipped module (`renpy`, `rpgmaker`, `zanzarah`)
owns an independent semantic version in `src/modules/<module>/VERSION` and its own `renpy/vX.Y.Z`
release lineage; there is no repository release version.

## Prerequisites

A Windows 11 host with:

- Visual Studio Build Tools 2022 with the v143 toolset for x86 and x64, the Spectre-mitigated
  libraries, and the LLVM/Clang toolset the coverage, sanitizer and fuzz configurations use;
- the Windows 11 SDK, and the Windows 10 SDK 2004 debugging tools that provide the pinned UMDH and
  GFlags the leak probe needs;
- PowerShell 7.4 or newer, the shell `build.ps1` targets;
- `uv`, which `build.ps1` uses to create the pinned Python environment;
- `vcpkg` reachable through `VCPKG_ROOT`, used in manifest mode over the pinned dependency set.

Only the tools the operation you run actually needs must be present: `ci-source` needs clang-format,
Cppcheck and PSScriptAnalyzer, `ci-arch` needs the full MSVC/LLVM toolchain and dumpbin, and the x64
leak step needs UMDH. A missing tool fails the run with a plain message rather than being skipped.

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

`ci-arch` also takes a bounded `-Stage` selection, `all` (the default), `native`, `coverage`,
`asan`, `ubsan`, `fuzz` or `leaks`. `all` runs every stage in the fixed order native, coverage,
asan, ubsan, fuzz, leaks and prepares the Release tree and the x64 leak probe once. A single stage
runs only itself: `native` is the Debug/Release suites, the x64 leak-probe build and the dumpbin
and BinSkim audits; `leaks` is only the UMDH setup and the mode/scenario measurements over an
already-built x64 Release tree, so it never restores vcpkg, runs MSBuild, BinSkim or LLVM. The
`ubsan` and `leaks` stages are the x64 specialists and refuse any other selection up front. The
flag exists so CI can run each slice in an independent job; the local default is unchanged.

## CI

The required check names are the real per-stage jobs, not a gate that re-scores other jobs. Branch
protection requires `source-checks`, `native-x86`, `native-x64`, `coverage-x86`, `coverage-x64`,
`asan-x86`, `asan-x64`, `ubsan-x64`, `fuzz-x86`, `fuzz-x64` and `leaks-x64` directly, so the slow
native build of one architecture never waits on another and a single stage fails on its own. Every
stage/architecture slice calls one maintained reusable workflow, `.github/workflows/ci-check.yml`,
with its `arch` and `stage`, and that workflow provisions only what the stage needs: the source tools
only for `source-checks`, BinSkim only for `native`, UMDH only for `leaks`, the pinned Python/uv
environment for every stage, and the vcpkg/MSBuild side only for the build and source stages. The
`source-checks` row runs `ci-source`; the rest run `ci-arch -Stage …` for one architecture
(`native`/`coverage`/`asan`/`ubsan`/`fuzz`/`leaks`). `leaks-x64` depends only on `native-x64` and
downloads that job's `release-x64` artifact from the same run — its x64 Release directory already
carries the module `.so`/`.pdb` and `leak-probe.exe`/`.pdb` — and never touches another run's
binaries or a cache. A failed, skipped or cancelled slice therefore fails closed through GitHub's own
`needs`, not through a shell decision table.

Triggers: `pull_request` on `master`, `push` on `master` and the module tag prefixes; no
`workflow_dispatch` and no schedule. Every mandatory check stays runnable from a local console through
the same public entry points. The `native` slices upload their verified Release binaries as Actions
artifacts; `packaging` (needing all eleven required checks) consumes them without rebuilding and
writes the module archives; the tag-only `release` job selects exactly the three archives of the
tagged module and publishes them with `gh release create --verify-tag --notes-file`. Logs and reports
are uploaded as Actions artifacts, never as public release assets.

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
toolchain and the dependency inputs, so two concurrent jobs can never save a mixed or incomplete
cache under one key. Only the binary dependency cache exists; no content-addressed or incremental
build output is carried across commits.

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

**№6 — analyzer versions are not pinned consistently.** The workflow pins Cppcheck and PSScriptAnalyzer
by version and the x64 UMDH MSI by SHA-256, but LLVM (`clang-cl`, `clang-tidy`, `clang-format`,
`llvm-cov`) comes from the hosted image's VS install. Pin the versions and provision LLVM explicitly.

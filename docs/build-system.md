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

## CI

Three verification jobs — `source` (`ci-source`) and `x86`/`x64` (`ci-arch -Arch …`) — plus a
dependent `packaging` job and a `release` job on `renpy/vX.Y.Z`, `rpgmaker/vX.Y.Z` or `zanzarah/vX.Y.Z`
tags. Triggers: `pull_request` on `master`, `push` on `master` and the module tag prefixes; no
`workflow_dispatch` and no schedule. Every mandatory check stays runnable from a local console through
the same public entry points. The verification rows upload the verified Release binaries as Actions
artifacts; `packaging` consumes them without rebuilding and writes the module archives; the tag-only
`release` job selects exactly the three archives of the tagged module and publishes them with
`gh release create --verify-tag --notes-file`. Logs and reports are uploaded as Actions artifacts,
never as public release assets.

## Open gaps

Each is a missing gate or a false promise, none an accepted deviation. TDD applies: the failing test
that states the observable requirement comes first.

**№1 — the memory-budget stress gate does not exist.** The UMDH gate compares allocation growth across
windows, which a parser buffering a whole entry and releasing it every round passes. Needed: a gate
failing when peak private bytes scale with archive or entry size instead of the streaming buffer.

**№6 — analyzer versions are not pinned consistently.** The workflow pins Cppcheck and PSScriptAnalyzer
by version and the x64 UMDH MSI by SHA-256, but LLVM (`clang-cl`, `clang-tidy`, `clang-format`,
`llvm-cov`) comes from the hosted image's VS install. Pin the versions and provision LLVM explicitly.

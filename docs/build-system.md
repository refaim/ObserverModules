# Build system

## What this is

A console-first, Windows-only build over MSBuild and pinned manifest-mode vcpkg. There is no graph, cache, scheduler,
Jinja renderer or typed-result protocol: `build/main.py` is the argument contract, `build/native.py` drives vcpkg and
MSBuild directly, and the remaining families (`build/source_checks.py`, `build/diagnostics.py`,
`build/packaging_ops.py`) are plain functions that shell out to the retained `core` validators. MSBuild owns project
evaluation, dependencies, scheduling and incremental object state under the stable `out/native` tree; vcpkg owns
dependency resolution.

Philosophy: one operation runs one real tool through one explicit output directory, and the tool's exit code (or a
`core` validator's gate) is the verdict. A missing prerequisite fails closed; a check is never weakened to make a run
pass. Whatever this document does not state is derivable from the code, and the code wins.

Release contract, non-negotiable: Windows-only native C++23 through MSVC `cl.exe`; x86, x64; MSVC runtime and all
third-party libraries linked statically (`/MT`, static vcpkg triplets); a release archive carries no redistributable
runtime and no non-system DLL; CMake is not part of this tree (vcpkg ports may use it internally); no `/clr`. Each
shipped module (`renpy`, `rpgmaker`, `zanzarah`) owns an independent semantic version in `src/modules/<module>/VERSION`
and its own `renpy/vX.Y.Z` release lineage; there is no repository release version.

## Prerequisites

A Windows 11 host with:

- Visual Studio Build Tools 2022 with the v143 toolset for x86 and x64, and the matching
  Spectre-mitigated libraries;
- the Windows 11 SDK, for the tooling the binary audit and leak operations shell out to;
- PowerShell 7.4 or newer, the shell `build.ps1` targets;
- `uv`, which `build.ps1` uses to create the pinned Python environment;
- `vcpkg` reachable through `PATH` or a `VCPKG_ROOT` environment variable, used in manifest mode over
  the pinned dependency set.

`.\build.ps1 doctor` probes this toolchain and reports which further verification tools it expects
(clang-format, Cppcheck, PSScriptAnalyzer, BinSkim, LLVM and UMDH); install only what the operations
you run need.

## Map

- `build.ps1` / `build.cmd` — entry point: pinned `uv` environment, rejoins array-valued arguments with commas so an
  unquoted `x86,x64` survives PowerShell parsing, forwards argv. `UNPACKRAT_BUILD_PRINT_ARGV=1` prints argv, exits 97.
- `build/main.py` — the argument and command contract and the plain operation dispatch: subcommands, options, three
  architecture rejections, the desktop-priority class, and the keep-going verification runner.
- `build/native.py` — vcpkg restore and MSBuild build/test against the stable `out/native` tree; MSBuild is the only
  scheduler.
- `build/source_checks.py` — clang-format, Cppcheck, PSScriptAnalyzer, the `*.Tests.ps1` contracts, MSVC `/analyze` and
  clang-tidy, normalized and gated through `core.sarif`.
- `build/diagnostics.py` — LLVM coverage, ASan/UBSan, libFuzzer and UMDH leaks, gated through `core.cpp_coverage`,
  `core.sanitizer` and `core.leak`.
- `build/packaging_ops.py` — dumpbin/BinSkim/PE-version audit, deterministic staging and ZIP creation, smoke tests, and
  the release manifest, over the stable `out/native/bin/<arch>/Release` binaries.
- `build/core/` — the retained validators and tool locators: `cpp_coverage`, `sanitizer`, `sarif`, `binary_audit`,
  `package`, `module_version`, `release`, `changelog`, `leak`, `host`, `toolchain`, `quality_tools`, `source_tools`, `doctor`,
  `python_coverage`, the direct `clean` and `result_export` helpers, and the small `windows_process` job-owned runner
  (`windows_job` holds the kill-on-close limit).
- `build/projects/*.vcxproj` + `build/*.props` are MSBuild's half; `.github/workflows/main.yml` is a thin client.

## Invariants

- **MSBuild owns incremental state.** Each operation asks MSBuild for one project or one `unpackrat.proj`, with an
  explicit `ArtifactsRoot`, and lets it decide what to rebuild. Python owns no object cache and no dependency graph.
- **Explicit output directories.** The native tree is `out/native`; every other operation writes under an explicit
  directory the caller owns (`out/reports/<command>`, or the `-ExportDir` destination). A `core` validator that still
  confines itself to an output root is reached with a scoped `UNPACKRAT_OUT_DIR`/`UNPACKRAT_BUILD_DIR`.
- **100% lines and branches, in both gates**: first-party C++ under the Coverage configuration, and the build system's
  own python (`build/pyproject.toml`, `fail_under = 100`, `branch = true`). The only python escape hatch is a literal
  trailing `# pragma: no cover`; `exclude_also` is empty and `tests/test_coverage_config.py` drives coverage.py's parser
  to require every excluded line to carry it, a `__main__` guard body being the sole permitted widening.
- **Keep-going.** The verification runner executes independent operations, collects every failure and blocks only a
  dependent operation (packaging follows a failed Release build, test or audit), then exits nonzero.
- **Publishing is atomic and fresh.** `core/result_export.py` copies the current reports, failure logs and flat
  `packages/` into a new destination; it refuses an existing destination, a reparse hop, and any missing claimed file,
  and never publishes stale previous success or package evidence on a failure.
- **Local cleanup only.** `core/clean.py` removes exactly `out/native`, `out/reports` and `out/packages`, refusing a
  reparse point anywhere in an owned tree, and leaves `out/sol-team` and any unrelated directory untouched.
- **Desktop priority and descendant control.** `-Priority below-normal` (the default) sets the process priority class
  and the kill-on-close job limit, so every child compiler, linker and MSBuild node inherits it and dies with the job
  even on cancellation; a hosted runner has no desktop to protect and asks for `-Priority normal`.
- **A commit changing any input of a module's content identity must bump that module's VERSION in the same commit**,
  enforced at release time by `core/release.py`, not by review. `content_identity` raises on an input named `VERSION`
  rather than skipping it: content identity answers whether shipped bytes changed, which a version literal cannot.

## Deliberate decisions that look like bugs

- `Fuzz|Win32` compiles with `/clang:-mllvm /clang:-asan-stack-dynamic-alloca=0` (`unpackrat-fuzz.props:24`). Never
  substitute `-asan-stack=0`: it clears the same spurious i386 frame report but gives up real overflow detection.
- Coverage loads the three shipped `.so` files as `llvm-cov --object` with `tests.exe` as the profile executable, so the
  gate measures the DLLs FAR Manager actually loads, not the test binary.
- Coverage is `/MTd`, not `/MT` (`unpackrat-project.props:59-60`). Leak checking is an operation over `Release|x64`, not a
  seventh configuration.
- Only the timed x64 fuzz run consumes a wall-clock budget; x86 fuzzing is a replay-only gate over the same targets and
  is the only place 32-bit `size_t` overflow thresholds execute at all.
- Each leak scenario runs a preflight, a capture-and-judge measurement and a gate, because window diffs can only run in
  the session that captured them while a failing scenario must still export evidence. The gate fails on a repeated
  allocation stack above `-LeakToleranceBytes` across consecutive windows or on sustained total growth beyond twice it
  (`core/leak.py:301-304`), never on a literal zero-byte difference.
- Archive bytes, metadata, paths, counts and offsets are untrusted input: the format parsers live in a platform-neutral
  core with no Observer or Win32 dependency, impose explicit resource and iteration bounds, and validate before use.

## Running things

```powershell
.\build.ps1 build -Arch x86,x64 -Config Release
.\build.ps1 verify-arch -Arch x64 -ExportDir C:\evidence
.\build.ps1 verify-source          # python tests + 100% gate, clang-format, Cppcheck, PSScriptAnalyzer, contracts
.\build.ps1 fuzz -Arch x86,x64 -FuzzTarget all -FuzzSeconds 60
.\build.ps1 clean -CleanMode reports
```

- Subcommands and every option live in `main.py`; `-Arch`/`-Config`/`-Module` take comma lists or `all`. Exactly three
  architecture rejections: `test-leaks` demands `x64` alone, `fuzz` only x86/x64, `verify-arch` exactly one.
- `-ExportDir` is accepted by `package`, `verify-source`, `verify-arch` and `verify`, and exports on success and failure
  alike once the operations have run. The destination must be new.
- `verify_route` (`core/host.py`) intersects the request with host capability, then routes coverage x64, UBSan x64, ASan
  x86+x64, fuzz x86+x64; x64 specialists, leaks included, follow UBSan. Unroutable gates are reported deferred and the
  remaining independent gates still run.
- `verify-source` is flat: the source-format/PSSA/contract checks plus the python gate. `verify-arch` adds compiler
  analysis, the Debug and Release DLL suites, the routed diagnostics, the binary audit and packaging; `verify` combines
  both across the requested architectures.
- The python gate runs, from `build/`: `coverage run --rcfile pyproject.toml -m unittest discover -s tests
  -p test_*.py`, then `coverage report --fail-under=100` (`core/python_coverage.py`).
- Release gate over the manifests `verify-arch` produced: `python -m core.release gate --module <id> --tag <tag>
  --repository <root> --assets <dir> --output <dir> [--previous <packages.json>] <packages.json>...`, or the plain
  all-module form without `--module`. It fails on changed content without a bump, a bump without changed content and a
  backwards version; with no previous manifest it bootstraps explicitly.
- The tag's version must also match both the `src/modules/<module>/ChangeLog.ru.txt` and `ChangeLog.en.txt` top entries,
  and a module release's published notes carry both language sections (`core.changelog.release_notes`) rather than a
  generated commit list. The selective module archive carries the module, the registration ini, the two change logs and
  a generated `LICENSES.txt` at its root: the project's own MIT license, the Observer API header's LGPLv3 notice with the
  accompanying full GPLv3 text, and only that module's dependency notices; documentation-only format references follow
  in their own labeled section.
- A module release publishes exactly two per-architecture module archives and one
  `<module>-<version>-pdb.zip`. Every architecture's manifest names the same intermediate symbol bundle, so the
  published bundle carries one `<module>.pdb` for each of the two supported architectures (`x86` and `x64`); the gate
  assembles the disjoint architectures before the normal merge, version-lineage and asset gates run. There is no
  repository-wide symbol bundle.

## CI

Three verification jobs — `source` (`verify-source`) and `x86`/`x64` (`verify-arch -Arch …`) — plus a `release`
job on `renpy/vX.Y.Z`, `rpgmaker/vX.Y.Z` or `zanzarah/vX.Y.Z` tags. Triggers: `pull_request` on `master`, `push` on
`master` and the module tag prefixes; no `workflow_dispatch` and no schedule. Every mandatory check stays runnable
from a local console through the
same public entry points. The `source` row installs only the source-check tools, the architecture rows provision BinSkim
by operation, and the x64 row pins UMDH from the Windows 10 SDK 2004 line (a documented capture defect in Windows 11
SDKs); only the native vcpkg binary cache remains. Python owns the version, content and asset validation policy
(`src/modules/<module>/VERSION` and `core.release`); the workflow owns only the GitHub-specific orchestration over those
public entry points — uploading the verification evidence, probing the module's existing tag/asset lineage, running the
release gate and publishing the verified assets. Pull requests use the same gates with shorter bounded-work parameters.

## Open gaps

Each is a missing gate or a false promise, none an accepted deviation. Closed items live in git history; TDD applies:
the failing test that states the observable requirement comes first.

**№1 — the memory-budget stress gate does not exist.** Nothing in `build/` or `src/` measures peak private bytes or
working set; the only memory-shaped control is libFuzzer's own `-rss_limit_mb=1024`. The UMDH gate compares allocation
growth across windows (`core/leak.py:301-304`), a different property — a parser buffering a whole entry and releasing it
every round passes it. Needed: a gate failing when peak private bytes scale with archive or entry size instead of the
streaming buffer, finishing deterministically against repository-owned fixtures.

**№6 — analyzer versions are neither pinned nor provisioned consistently.** `doctor` enforces only the python version
(`core/doctor.py:32-38`) and formats the remaining probe strings without comparing them. The workflow pins Cppcheck,
PSScriptAnalyzer, BinSkim and the x64 UMDH MSI by version or SHA-256, but never LLVM: `clang-cl`, `clang-tidy`,
`clang-format`, `llvm-cov` come from the hosted image's VS install (`core/toolchain.py`), and the runtime locator only
checks presence. Pin the versions `doctor` accepts and provision LLVM explicitly.

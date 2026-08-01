# Autonomous build-system work log

This file records decisions, assumptions, unresolved questions, and verification evidence from the autonomous work
session started on 2026-08-01. It is intentionally a review log rather than permanent user documentation; discuss and
either accept, revise, or remove its entries after review.

## Fixed requirements

- The shipped modules are native MSVC binaries for x86, x64, and ARM64.
- Release code and dependencies use the static runtime (`/MT`) and ship without redistributable or third-party DLLs.
- The repository build graph uses MSBuild directly; vcpkg ports may use CMake internally.
- The public build entry point works from a clean Windows console without an IDE.
- Production code must reach 100% source line and branch coverage.
- Every meaningful parser surface must have deterministic regression tests and coverage-guided fuzzing.
- Real module DLLs require process-wide leak testing; bounded peak memory is tested separately from leak freedom.
- No local software may be installed or updated during this session.

## Decisions made during autonomous work

- Owner decision: keep MSBuild as the native compile/link backend. A Python DAG may orchestrate existing build and
  verification leaves, but replacing MSBuild is currently too costly and risky relative to the expected benefit.
  Revisit only with build-only evidence of a material native critical-path problem.
- The project registry baseline now follows the installed Scoop vcpkg revision
  `9e593bb18ea69cc5095e012465dcd675a822ed0d` (2026-07-29). Of the direct dependencies, only Catch2 changed at this
  baseline, from 3.15.2#1 to 3.15.3; zlib 1.3.2, nlohmann/json 3.12.0, and xxHash 0.8.3 remain current.
- Direct use of the zlib C API is isolated in `src/core/compression/zlib_codec.cpp`; all format and test code uses the
  repository C++ boundary. The unused zstr adapter was removed after its exception type proved incompatible with the
  current Windows sanitizer runtime. Keeping zlib 1.3.2 behind the boundary makes a future backend swap local.
- Parser allocation limits are now explicit: an encoded archive path is capped at 4,092 bytes, derived from the public
  1,024 UTF-16-code-unit Observer ABI buffer, and Zanzarah metadata is capped at 100,000 entries. The external corpus
  currently peaks at 2,205 entries, so the entry limit leaves substantial compatibility headroom. Revisit the 100,000
  value if a legitimate corpus example exceeds it; do not remove the bound.

- After the mandatory build, coverage, fuzzing, and leak gates are complete, parser-core extraction and IWYU
  integration are authorized as stretch work during the same autonomous session.
- Parser-core extraction must preserve the public Observer ABI and self-contained per-module release layout; it is a
  source boundary statically linked into the existing DLLs, not a new runtime binary.
- Owner decision: after the current build migration and portable parser-core extraction are complete, establish a
  first-class WSL2/Linux developer workflow. It should run Mull and the useful Linux sanitizer/tooling surface against
  the portable core while the release artifacts continue to be built and verified with MSVC on Windows.
- Missing IWYU is not permission to install software. In that case the repository/CI integration may be prepared, but
  the unavailable local execution must be reported explicitly.
- Pickle memo entries currently use deep-copy value semantics because the parser's `unique_ptr` value model cannot
  represent Python object identity or cycles. This is correct for the acyclic Ren'Py indexes in scope, but the parser
  must not be described as a general cyclic Pickle implementation.
- Fixed-size Observer ABI metadata is always NUL-terminated and safely truncated with `wcsncpy_s(..., _TRUNCATE)`.
  Overlong descriptive metadata no longer rejects an otherwise valid archive.
- Two ineffective defensive catches were removed: the `GetItem` runtime-error catch could not be reached after the
  explicit out-of-range path, and zstr did not report a beyond-EOF seek through the attempted `ios_base::failure`
  catch. Actual parser/read failures remain mapped at the ABI boundary.

## Doubts and items for morning review

- Current BinSkim output is clean at error level but emits BA2027 for all three modules because their PDBs do not embed
  SourceLink metadata. Adding SourceLink would improve post-release debugging, but doing it correctly requires
  commit-specific URL mapping and a decision about publishing source-linked PDBs; it is not being silently suppressed.
- `vcpkg.json` declares `LGPL-3.0-or-later`, while the repository root license and README identify the overall project
  as GPLv3. This predates the build-system work. The manifest value may be inaccurate, but changing licensing metadata
  needs an explicit owner decision rather than an autonomous guess.
- One ABI coverage test deliberately uses a host progress callback that throws `std::runtime_error` so the DLL's
  best-effort exception mapping is exercised. Throwing a C++ exception through a DLL callback is not a supported host
  contract, especially with independently linked `/MT` CRT instances. Decide whether to document callbacks as
  non-throwing and keep this defensive catch, or introduce an internal failure seam so the branch is tested without a
  cross-boundary exception.
- The large `M:\observer\_test` corpus was inspected read-only for representative format variants, but the required
  hermetic suite does not execute its multi-gigabyte contents. Generated fixtures cover the supported RPA 2.0, RPA 3.0,
  RGSS3A, and Zanzarah PAK variants; the real corpus remains an opt-in compatibility/stress run.
- Native ARM64 CI uses the hosted `windows-11-arm` label. The current environment discovery intentionally invokes the
  amd64 MSBuild host and cross compiler, which should run under Windows ARM64 x64 emulation; the first hosted run is the
  final proof of that runner/toolchain assumption.
- There is no separate `RelWithDebInfo` configuration. `Release` is the single shippable configuration and combines
  `/O2`, `/GL`/`/LTCG`, `/MT`, compiler PDB information, and an explicitly full linker PDB. Symbols are archived
  separately per architecture and do not add a runtime or distribution dependency to module DLLs.
- The repository now has a proposed critical-software-inspired policy in `docs/critical-software-methodology.md`.
  It makes TDD, 100% first-party line/branch coverage, clean dependency direction, strict C/C++ ABI containment,
  RAII-only ownership, bounded untrusted-input processing, and evidence from exact release DLLs mandatory. It does not
  claim formal certification.
- Owner decision: decision tables are executable data-driven tests rather than manually maintained review documents;
  critical compound conditions require targeted MC/DC reasoning in addition to the global 100% branch gate.
- Owner decision: mutation testing is mandatory. A surviving non-equivalent reached mutant is a test defect; there is
  no accepted sub-100 mutation score. The engine remains an implementation decision because Mull requires LLVM and
  has no supported native-Windows workflow; mutating the future portable parser core in Linux CI is the leading design.
- Owner decision: certification and coding-standard compliance are out of scope. Core Guidelines, CERT, and JPL ideas
  are engineering inputs only; no MISRA or formal safety-standard profile will be maintained.
- Owner decision: do not impose an arbitrary maximum archive-file size. The read-only golden corpus includes RPA up to
  4,468,455,116 bytes, RGSS3A up to 901,714,312 bytes, and Zanzarah PAK up to 774,465,076 bytes. Defensive limits apply
  instead to declared fields versus actual remaining input, ABI-representable paths, entry/allocation arithmetic, and
  configurable expanded metadata/index budgets. Concrete metadata-budget defaults remain to be derived and tested.

## Verification ledger

Commands and their final results will be recorded here after the implementation stabilizes.

- LLVM production coverage: 969/969 lines, 296/296 branches, 480/480 regions, and 79/79 functions (100% each).
- Deterministic suite at that point: 24 test cases and 539 assertions; x86/x64 MSVC Debug, x64 MSVC ASan, and the x64
  clang-cl coverage gate passed.

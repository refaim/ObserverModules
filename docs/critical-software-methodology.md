# High-assurance software methodology

Status: proposed repository policy, accepted principles with several owner decisions still open.

ObserverModules parses untrusted, sometimes very large archives inside another application's process. A malformed
input, memory leak, ABI violation, or unbounded operation can therefore corrupt or exhaust the host. The project adopts
a **critical-software-inspired** engineering method to reduce that risk. This is not a claim of formal MISRA,
DO-178C, IEC 61508, or other safety certification: the project does not currently have an independent verification
organization, a certified toolchain, or the complete requirements-to-binary evidence such a claim would require.

## Non-negotiable policy

1. **Requirements and invariants come before implementation.** Each change identifies its observable behavior,
   failure behavior, input limits, ownership, and ABI impact. Safety-relevant assumptions must be executable as tests
   or explicit assertions where practical.
2. **Strict TDD is the default change protocol.** First produce a focused test that fails for the expected reason,
   then make the smallest production change, then refactor with the suite green. Every defect starts with a regression
   test. A test that executes a branch without checking behavior is not sufficient.
3. **All first-party production code has 100% line and branch coverage.** Coverage is a necessary completeness signal,
   not proof of correctness. Dangerous compound decisions additionally require executable data-driven decision tables
   and targeted MC/DC reasoning so each independent condition is shown to affect the result.
4. **Architecture boundaries are enforced.** Observer/FAR and Win32 integration are outer adapters. Archive operations
   and format parsers are inner policy. Dependencies point inward; parser code must be independently testable without
   loading FAR, Observer, or Win32 UI infrastructure.
5. **The C/C++ boundary is explicit and hostile by default.** It exposes only stable C-compatible layouts, functions,
   result codes, and documented ownership. No C++ exception, STL type, RTTI identity, allocator responsibility, or
   implicit lifetime crosses the ABI.
6. **C++ resources use deterministic ownership.** Prefer values and standard containers. Every acquired resource is
   immediately owned by an RAII handle. Application code contains no naked owning allocation or release. Raw pointers
   are non-owning; `std::unique_ptr` is the default polymorphic owner, while `std::shared_ptr` is exceptional and must
   express a genuinely shared lifetime.
7. **All work caused by input is bounded.** A parser defines and checks maximum sizes, counts, nesting depth, allocation
   budget, and progress conditions before doing expensive work. Integer calculations are checked before narrowing,
   seeking, allocating, or indexing. Input-driven recursion is replaced by iteration or given a strict depth bound.
   There is no arbitrary whole-archive size ceiling: multi-gigabyte archives are legitimate. Declared structural
   fields must fit the actual input, paths must fit the public ABI, and expanded metadata/index data receives a
   separately configurable budget so a compact decompression bomb cannot exhaust the host process.
8. **Failures are deterministic and fail closed.** Partial output is not reported as success. Cancellation, callback
   failure, malformed input, exhaustion, and I/O errors have tested outcomes. Outermost ABI functions validate inputs,
   initialize outputs, catch only at the boundary, and translate failures to the documented result contract.
9. **Evidence is produced by the exact deliverable.** Unit and parser tests may use seams, but ABI integration,
   import/export audit, packaging smoke tests, and leak tests exercise the MSVC Release DLLs that are shipped.
10. **A green gate is never manufactured.** Threshold reductions, first-party exclusions, broad suppressions, swallowed
    sanitizer failures, or catch-all fuzz targets are prohibited. Any necessary deviation is narrow, justified,
    time-bounded where appropriate, and owner-reviewed.

## C ABI contract

Every exported function and callback must satisfy all of the following:

- use `extern "C"`, an explicit calling convention, fixed-width or ABI-defined types, and fixed-layout structures;
- version extensible structures with `StructSize` or an equivalent explicit size contract;
- validate every pointer, buffer length, structure size, enum/range value, and callback before dereference or call;
- initialize output structures and handles before any operation that can fail;
- document who owns every buffer and handle, how long borrowed data remains valid, and who releases a resource;
- never allocate in one CRT and require another module to deallocate it;
- prohibit exceptions escaping either exported functions or host callbacks; translate internal failures once at the
  outer boundary and keep that mapping covered by ABI tests;
- preserve the existing symbol names, calling convention, layouts, and result semantics unless an explicitly reviewed
  ABI version change is made.

Compatibility is checked at both source and binary levels: compile-time layout assertions, real-DLL contract tests,
exact export allowlists, import audits, and package smoke tests.

## C++ ownership and resource rules

- Prefer values, `std::vector`, `std::string`, and scoped resource wrappers.
- Use `std::span`/`std::string_view` for checked borrowed ranges and references for required non-null objects.
- Use `std::unique_ptr` only when value semantics do not fit, normally for polymorphism or optional ownership.
- Use `std::shared_ptr` only when no single owner can be identified; record the lifetime reason in the design review.
- Wrap `FILE*`, Win32 `HANDLE`/`HMODULE`, archive streams, zlib state, and temporary-file cleanup in move-only RAII
  types with non-throwing destructors.
- Do not call owning `new`, `delete`, `malloc`, `calloc`, `realloc`, or `free` in application C++. Placement new inside
  a reviewed low-level resource abstraction is a possible deviation, not a general exception.
- Destructors and cleanup paths must not throw. Move operations should be `noexcept` when their members permit it.
- Avoid mutable globals. If shared state is unavoidable, define its lifetime, synchronization, and reset behavior for
  repeated module load/unload cycles.

These rules follow the C++ Core Guidelines resource-management model: automatic resource handles and RAII, raw
pointers as non-owning views, no naked `new`/`delete`, and `unique_ptr` preferred over shared ownership.

## Parser safety case

Each supported format gets a small safety case in tests and, as the parser core is extracted, in its module-level
documentation. At minimum it answers:

- What identifies the format, and how are truncated or contradictory headers rejected?
- What are the maximum accepted archive size, entry count, path length, nesting depth, metadata/index size, and
  decompressed size? Which limits come from the format and which are defensive project limits?
- Which additions, multiplications, casts, seeks, and range calculations can overflow or leave the input bounds?
- Can every loop demonstrate progress and a finite upper bound? Can decompression or parsing amplify tiny input into
  excessive CPU, memory, disk, or output?
- How are path traversal, absolute paths, device names, alternate separators, duplicate names, and Unicode conversion
  handled before extraction?
- What happens on cancellation, callback failure, short read/write, close/flush failure, partial output, and host
  unload/reload?

Required tests include valid minimal and representative archives, every error category, zero/one/maximum boundaries,
one-past-limit cases, truncation at meaningful byte positions, arithmetic edges, callback failures, cancellation, and
resource cleanup after every failure path. Multi-gigabyte external corpora remain optional compatibility/stress input;
small generated repository fixtures are the deterministic local contract.

## Verification ladder

Every layer finds a different defect class; passing one does not substitute for another.

1. **Fast deterministic tests:** parser/unit tests, common archive-operation tests, and ABI contract tests.
2. **Structural coverage:** 100% LLVM line and branch coverage over first-party production code, plus review of tests
   that reach each branch.
3. **Exact-toolchain tests:** MSVC Debug and shippable MSVC Release on runnable x86 and x64 targets; ARM64 is
   cross-built, analyzed, audited, and packaged, with unavailable runtime checks reported explicitly as deferred.
4. **Static analysis:** compiler warnings-as-errors, MSVC `/analyze`, clang-tidy, Cppcheck, and PowerShell analysis.
   Diagnostics are fixed or narrowly justified, never globally muted. Additional services such as CodeQL may repeat or
   extend this evidence but cannot replace a local gate.
5. **Dynamic analysis:** MSVC AddressSanitizer where supported; clang-cl AddressSanitizer and
   UndefinedBehaviorSanitizer as independent diagnostic builds; UMDH across repeated real-DLL operations and repeated
   load/unload cycles. Peak private bytes and resource budgets are checked separately from leak growth.
6. **Coverage-guided fuzzing:** every parser and its meaningful decoding surfaces receive libFuzzer targets, curated
   seed corpora, bounded input/resources, persisted crash artifacts, and regression tests for every confirmed defect.
7. **Binary and package assurance:** exact exports, forbidden imports, `/MT` runtime audit, BinSkim, PDB archive audit,
   archive-content allowlists, and smoke tests of the packaged DLL bytes.
8. **Release evidence:** clean-checkout build, pinned dependencies, full gate results, hashes/artifacts, and a reviewed
   decision/deviation log.

Coverage and fuzzing must not catch allocation exhaustion or unexpected exceptions merely to keep running. A crash,
sanitizer report, timeout, leak, or resource-budget violation is a finding and becomes a minimized regression test.

## Change protocol and traceability

For each non-trivial change, preserve this chain in the issue/commit, test names, and reports:

`requirement or hazard -> failing test -> implementation -> coverage -> analysis -> dynamic/fuzz evidence -> binary`

The repository does not need bureaucratic documents for trivial refactors, but a reviewer must be able to answer why
the change exists, which failure it prevents, which test demonstrates it, which binary contains it, and whether it
changes an ABI, parser limit, dependency, or accepted risk.

Security- and reliability-relevant deviations are recorded beside the affected code/configuration and in the owning
issue or commit. Analyzer suppressions include the rule, exact scope, rationale, and a test or other evidence that
covers the residual risk.

## Standards adapted, not claimed

- **C++ Core Guidelines:** normative baseline for ownership, RAII, interfaces, bounds-aware views, and simplicity.
- **SEI CERT C++:** secure-coding review source for declarations, integers, containers, strings, memory, I/O, error
  handling, object lifetime, concurrency, and miscellaneous security hazards.
- **JPL Power of Ten:** adopt reviewable control flow, bounded loops, no unbounded recursion, small cohesive functions,
  assertions/contracts, minimal preprocessor use, and warnings/static analysis. Its strict C-oriented prohibition on
  dynamic allocation is adapted to bounded RAII allocation because archive metadata is inherently variable-sized.
- **MISRA and formal safety standards:** they can inspire individual engineering practices, but compliance and
  certification are explicitly out of scope. The repository will not maintain a MISRA profile or claim DO-178C,
  IEC 61508, ISO 26262, or similar status.

## Decisions to settle with the owner

1. **Internal error model:** keep typed C++ exceptions inside the core and translate them only at the ABI, or migrate
   fallible parser/application operations toward an explicit result type such as `std::expected`? Either choice must
   preserve RAII and prohibit exceptions crossing the C boundary.
2. **Resource budgets:** choose concrete archive, entry-count, path, index, decompressed-output, nesting, CPU/time, and
   memory ceilings per format, including whether callers may configure them.
3. **Shared ownership:** forbid `std::shared_ptr` entirely in first-party code unless an ADR is approved, or permit it
   with a local lifetime rationale?
4. **Release provenance:** whether reproducible-build comparison, SBOM, signing, and SLSA-style provenance become
   mandatory release gates.

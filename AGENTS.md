# Repository agent instructions

## Build

This project has a console-first Windows build based directly on MSBuild and pinned manifest-mode vcpkg dependencies.
Repository CMake is not part of the build graph; CMake use inside a vcpkg port is acceptable.

Use the root entry point from an ordinary PowerShell or `cmd.exe` console:

```powershell
.\build.ps1 doctor
.\build.ps1 build -Arch all -Config Release
.\build.ps1 test -Arch x86,x64 -Config Debug
.\build.ps1 verify -Arch x64
```

Run the relevant build, tests, and checks after changing code. Do not install or update developer tools automatically;
report missing prerequisites to the user. Release modules must remain MSVC-built, `/MT`, self-contained binaries for
x86, x64, and ARM64 with no third-party runtime DLLs.

All production changes follow strict TDD: state the observable requirement or invariant, add a focused test that fails
for the expected reason, implement the smallest correct change, and refactor only while the suite remains green. Every
bug fix starts with a regression test. The required coverage gate is 100% first-party source lines and branches; do not
weaken thresholds, exclude production files, write coverage-only tests with no behavioral assertion, or add broad
suppressions to make a check pass.

## Development guidelines

This project uses C++23 and follows the high-assurance engineering policy in
`docs/critical-software-methodology.md`. In particular:

- Preserve clean dependency direction: platform/Observer adapters depend on application and parser code, never the
  reverse. Format parsing belongs in a platform-neutral core and must not acquire Observer or Win32 dependencies.
- Keep the C ABI boundary strict. Export only C-compatible, fixed-layout data and explicit sizes. Never let STL types,
  C++ exceptions, allocator ownership, or implicit lifetime assumptions cross the boundary. Validate all inbound
  pointers and structure sizes, initialize outputs defensively, and translate every internal failure to the documented
  ABI result at the outermost boundary.
- Use value semantics and RAII for every resource, including memory, files, module handles, and temporary output.
  Application C++ must not contain owning `new`, `delete`, `malloc`, `calloc`, `realloc`, or `free`. A raw pointer or
  reference is non-owning; prefer references, `std::span`, and `std::string_view` where they express the contract.
  Prefer `std::unique_ptr` for polymorphic ownership. `std::shared_ptr` requires a documented, genuinely shared
  lifetime; use `std::weak_ptr` to break cycles.
- Treat archive bytes, metadata, paths, counts, offsets, sizes, and callback behavior as untrusted input. Validate
  before use, use checked arithmetic before narrowing/allocation/seeking, impose explicit resource and iteration
  bounds, guarantee loop progress, and avoid input-driven recursion unless a strict depth limit is enforced.
- Express security-relevant condition combinations as executable, data-driven decision-table tests.
- Keep functions cohesive, control flow reviewable, ownership explicit, and preprocessor use minimal. Avoid magic
  numbers, hidden global state, duplicated policy, and speculative abstraction. KISS and DRY remain subordinate to
  clear boundaries and independently testable behavior.
- A check suppression or deviation must be narrow, explained beside the code or in the decision log, and reviewed by
  the owner. Never catch `std::bad_alloc`, `std::length_error`, access violations, or sanitizer findings merely to make
  fuzzing or tests pass.

## Architecture overview

This project implements Observer plugin modules for FAR Manager that handle exotic archive formats. Each module
implements the Observer API for one format family.

### Core components

- **API layer** (`src/api.h`, `src/dll.cpp`): Observer entry points such as `OpenStorage`, `CloseStorage`, `GetItem`,
  and `ExtractItem`.
- **Archive wrapper** (`src/archive.h`, `src/archive.cpp`): common archive lifecycle and extraction behavior.
- **Extractor interface** (`src/modules/extractor.h`): the internal contract implemented by each format module.

### Module structure

Supported modules live under `src/modules/`:

- `renpy/`: Ren'Py RPA archives and their Pickle index parser;
- `rpgmaker/`: RPG Maker VX Ace RGSS3A archives;
- `zanzarah/`: Zanzarah PAK archives.

Each contains format-specific implementation, a `.def` export definition, and `observer_user.ini` registration data.

### Data flow

1. FAR Manager/Observer loads the module through `LoadSubModule()`.
2. `OpenStorage()` creates an archive wrapper with the format extractor.
3. `PrepareFiles()` validates and indexes archive contents.
4. `GetItem()` exposes entry metadata.
5. `ExtractItem()` streams an entry to the requested destination with progress/cancellation reporting.

### Tests

Catch2 tests live in `src/tests/`. Unit tests exercise parser logic directly, while integration and ABI contract tests
load the actual module binaries without requiring FAR Manager. Small repository-owned fixtures are mandatory. The
external golden corpus is an optional compatibility/stress layer selected with `-Corpus`.

See `docs/build-system.md` for the current command contract and build architecture.

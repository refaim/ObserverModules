# GARbro Observer Module — Implementation Plan

## Architecture

```
dll.cpp              (C, Observer API exports)
  → garbro_archive.h/cpp  (pure C++, orchestration)
    → bridge.h/cpp   (C++/CLI behind pimpl, talks to GARbro)
      → GARbro.Core.dll  (ILRepack-merged: GameRes + ArcFormats + all deps)
```

Only `bridge.cpp` compiles with `/clr`. All other files are pure native C++.

### Distribution Layout

```
modules/
├── garbro.so              ← C++/CLI mixed-mode DLL (platform-specific: x86 or x64)
├── observer_user.ini      ← generated filter list (all GARbro extensions)
└── garbro/
    ├── GARbro.Core.dll    ← ILRepack-merged assembly (Any CPU, ~10-15 MB)
    └── Formats.dat        ← encryption schemes database
```

### Repository Layout

```
ObserverModules/
├── extern/
│   └── GARbro/                    ← git submodule
├── tools/
│   └── gen_ini/                   ← C# console tool: generates observer_user.ini
│       ├── gen_ini.csproj
│       └── Program.cs
├── src/
│   ├── api.h
│   ├── modules/
│   │   └── garbro/
│   │       ├── bridge.h           ← pure C++ interface (pimpl)
│   │       ├── bridge.cpp         ← C++/CLI implementation (/clr)
│   │       ├── garbro_archive.h   ← pure C++ archive wrapper
│   │       ├── garbro_archive.cpp
│   │       ├── dll.cpp            ← Observer API exports (pure C)
│   │       ├── garbro.def         ← DLL exports
│   │       └── observer_user.ini  ← template (overwritten by gen_ini)
│   └── tests/
│       ├── garbro.cpp             ← integration tests
│       └── framework/
└── CMakeLists.txt
```

### CI Pipeline

```
Step 1: git submodule update --init (GARbro)
Step 2: nuget restore extern/GARbro
Step 3: msbuild extern/GARbro/GARbro.sln /p:Configuration=Release /p:Platform="Any CPU"
Step 4: ilrepack /out:GARbro.Core.dll GameRes.dll ArcFormats.dll ArcExtra.dll ArcLegacy.dll <deps...>
Step 5: dotnet run --project tools/gen_ini (generates observer_user.ini)
Step 6: cmake --preset x64-release && cmake --build build/x64-release
Step 7: cmake --preset x86-release && cmake --build build/x86-release
Step 8: ctest (run all tests)
Step 9: cpack (package x86 + x64 ZIPs)
```

---

## Phase 0: Preparation

### 0.1 — Interface Design (BLOCKING — all other phases depend on this)

- [ ] **0.1.1** [PROG-A] Define `src/modules/garbro/bridge.h` — pure C++ interface with pimpl:
  - `garbro::file_info` struct (path, size, packed_size)
  - `garbro::archive` class (try_open, format, list_files, extract, file_count)
  - `garbro::init(module_dir_path)` / `garbro::shutdown()` free functions
  - init loads `GARbro.Core.dll` and `Formats.dat` from `garbro/` subfolder relative to module_dir_path
  - No managed types leak outside
- [ ] **0.1.2** [REVIEW-A] Review `bridge.h` — check: no CLI types, no leaking .NET, pimpl correct, const-correctness, noexcept where appropriate
- [ ] **0.1.3** [PROG-A] Fix review findings for `bridge.h`

### 0.2 — Build System Skeleton (can start after 0.1.1)

- [ ] **0.2.1** [PROG-B] Add GARbro as git submodule at `extern/GARbro`
- [ ] **0.2.2** [PROG-B] Add `garbro` target to `CMakeLists.txt`:
  - Shared library, output `garbro.so`
  - `bridge.cpp` compiled with `/clr` and `/EHa` flags (per-file property)
  - All files compiled with `/MD` (dynamic CRT, required by `/clr`)
  - Other files compiled as native C++
  - Reference `GARbro.Core.dll` via `#using`
  - Add `garbro.def` with `LoadSubModule` / `UnloadSubModule` exports
- [ ] **0.2.3** [PROG-B] Create `src/modules/garbro/garbro.def`
- [ ] **0.2.4** [PROG-B] Verify the skeleton compiles (empty stubs)
- [ ] **0.2.5** [REVIEW-B] Review CMake changes — check: /clr only on bridge.cpp, /MD not conflicting with other modules, correct assembly references, both x86 and x64 presets work
- [ ] **0.2.6** [PROG-B] Fix review findings for build system

### 0.3 — GARbro Build + ILRepack (parallel with 0.2)

- [ ] **0.3.1** [PROG-B] Create build script `scripts/build_garbro.bat`:
  - `nuget restore extern/GARbro`
  - `msbuild extern/GARbro/GARbro.sln /p:Configuration=Release /p:Platform="Any CPU"`
  - `ilrepack /out:GARbro.Core.dll GameRes.dll ArcFormats.dll ArcExtra.dll ArcLegacy.dll <all NuGet deps>`
  - Copy `GARbro.Core.dll` + `Formats.dat` to build output
- [ ] **0.3.2** [PROG-B] Verify ILRepack produces working `GARbro.Core.dll`:
  - Load in test harness
  - FormatCatalog.Instance initializes
  - ArcFormats discovered
- [ ] **0.3.3** [REVIEW-B] Review build script — check: all deps included in ILRepack, Formats.dat copied, idempotent
- [ ] **0.3.4** [PROG-B] Fix review findings

### 0.4 — Extension List Generator (parallel with 0.2, 0.3)

- [ ] **0.4.1** [PROG-C] Create `tools/gen_ini/gen_ini.csproj` — .NET console app referencing `GARbro.Core.dll`
- [ ] **0.4.2** [PROG-C] Implement `tools/gen_ini/Program.cs`:
  - Load `FormatCatalog.Instance`
  - Load `Formats.dat` scheme
  - Enumerate `catalog.ArcFormats.SelectMany(f => f.Extensions)`
  - Deduplicate, sort, format as `*.ext`
  - Output `observer_user.ini` with `[Modules]` and `[Filters]` sections
- [ ] **0.4.3** [PROG-C] Write tests for gen_ini:
  - Output is valid INI format
  - Contains `[Modules]` section with `GARbro=modules\garbro.so`
  - Contains `[Filters]` section with comma-separated extensions
  - No empty extensions in output
  - No duplicate extensions
- [ ] **0.4.4** [PROG-C] Verify gen_ini runs after ILRepack and produces correct output
- [ ] **0.4.5** [REVIEW-C] Review gen_ini — check: handles empty extensions, deduplication, INI escaping
- [ ] **0.4.6** [PROG-C] Fix review findings

---

## Phase 1: Bridge Layer (C++/CLI ↔ GARbro)

All items in Phase 1 can run **in parallel** with Phase 2 (archive layer) once `bridge.h` is finalized.

### 1.1 — Init/Shutdown

- [ ] **1.1.1** [PROG-A] Write tests for `garbro::init()` / `garbro::shutdown()`:
  - init loads FormatCatalog from `GARbro.Core.dll`, loads `Formats.dat` scheme
  - double-init is safe (idempotent)
  - shutdown after init doesn't crash
  - shutdown without init doesn't crash
- [ ] **1.1.2** [PROG-A] Implement `garbro::init()` and `garbro::shutdown()` in `bridge.cpp`:
  - Use `GetModuleFileName()` to find own DLL path
  - Resolve `garbro/GARbro.Core.dll` and `garbro/Formats.dat` relative to it
  - Load assembly, initialize FormatCatalog, deserialize scheme
- [ ] **1.1.3** [PROG-A] Verify tests pass, check coverage — must be 100% lines+branches
- [ ] **1.1.4** [REVIEW-A] Review init/shutdown — check: thread safety, resource leaks, exception handling across managed/native boundary, path resolution correct
- [ ] **1.1.5** [PROG-A] Fix review findings

### 1.2 — Format Detection (try_open)

- [ ] **1.2.1** [PROG-A] Write tests for `garbro::archive::try_open()`:
  - Valid archive → returns non-null, format() returns correct tag
  - Invalid file → returns nullptr
  - Non-existent path → returns nullptr (no exception)
  - Empty file → returns nullptr
  - Test with at least 3 different archive formats from GARbro test data
- [ ] **1.2.2** [PROG-A] Implement `try_open()` in `bridge.cpp`:
  - Create `ArcView` from path
  - Call `ArcFile::TryOpen()`
  - Store `ArcFile^` in pimpl via `gcroot<>`
  - Return format tag via `format()`
- [ ] **1.2.3** [PROG-A] Verify tests pass, 100% coverage
- [ ] **1.2.4** [REVIEW-A] Review try_open — check: ArcView lifecycle, GCHandle pinning, wstring conversion correctness, exception translation
- [ ] **1.2.5** [PROG-A] Fix review findings

### 1.3 — File Listing (list_files / file_count)

- [ ] **1.3.1** [PROG-A] Write tests for `list_files()` and `file_count()`:
  - Known archive → correct file count
  - Known archive → correct file names, sizes, packed_sizes
  - Archive with subdirectories → paths preserved with backslashes
  - PackedEntry → packed_size != size
  - Empty archive (0 files) → empty list
- [ ] **1.3.2** [PROG-A] Implement `list_files()` and `file_count()`:
  - Iterate `ArcFile::Dir`
  - Convert `Entry` / `PackedEntry` → `garbro::file_info`
  - Handle path encoding (Shift-JIS / UTF-8)
- [ ] **1.3.3** [PROG-A] Verify tests pass, 100% coverage
- [ ] **1.3.4** [REVIEW-A] Review list_files — check: encoding conversion, PackedEntry detection, memory allocation
- [ ] **1.3.5** [PROG-A] Fix review findings

### 1.4 — File Extraction (extract)

- [ ] **1.4.1** [PROG-A] Write tests for `extract()`:
  - Extract known file → output matches expected bytes (hash check)
  - Extract compressed file → decompressed correctly
  - Progress callback receives bytes
  - Progress callback returning false → extraction aborts
  - Extract to non-writable path → throws write_error
  - Index out of range → throws
- [ ] **1.4.2** [PROG-A] Implement `extract()`:
  - Call `ArcFile::OpenEntry()` to get Stream
  - Read stream in 128KB chunks
  - Write to dest path
  - Call progress callback per chunk
- [ ] **1.4.3** [PROG-A] Verify tests pass, 100% coverage
- [ ] **1.4.4** [REVIEW-A] Review extract — check: stream disposal, large file handling, progress granularity, exception safety
- [ ] **1.4.5** [PROG-A] Fix review findings

### 1.5 — Destructor / Resource Cleanup

- [ ] **1.5.1** [PROG-A] Write tests:
  - Destroy archive → no leaks (ArcView disposed)
  - Destroy after partial extraction → clean shutdown
  - Move semantics work correctly
- [ ] **1.5.2** [PROG-A] Implement destructor — dispose GCHandle, release ArcFile
- [ ] **1.5.3** [PROG-A] Verify tests pass, 100% coverage
- [ ] **1.5.4** [REVIEW-A] Review destructor — check: prevent double-free, prevent access after dispose
- [ ] **1.5.5** [PROG-A] Fix review findings

---

## Phase 2: Archive Layer (pure C++)

Can run **in parallel** with Phase 1 once `bridge.h` is finalized.
Uses mock/stub of bridge for unit testing.

### 2.1 — Mock Bridge

- [ ] **2.1.1** [PROG-B] Create `mock_bridge.h/cpp` — test double for `garbro::archive`:
  - Configurable: set file list, set extract behavior, set format string
  - Tracks calls: open count, extract calls, last progress callback
- [ ] **2.1.2** [REVIEW-B] Review mock — check: covers all bridge.h methods, configurable error injection
- [ ] **2.1.3** [PROG-B] Fix review findings

### 2.2 — Archive Wrapper

- [ ] **2.2.1** [PROG-B] Write tests for `garbro_archive` (new class, does NOT reuse extractor.h):
  - `open()` → delegates to `garbro::archive::try_open()`, returns archive_info with format
  - `open()` with invalid file → throws
  - `prepare_files()` → populates file list from bridge
  - `get_file()` → returns correct file_info by index
  - `get_file()` out of range → throws out_of_range
  - `extract_file()` → delegates to bridge extract with progress callback
  - `extract_file()` user abort → throws user_interrupt
  - Path separators normalized to backslash
- [ ] **2.2.2** [PROG-B] Implement `garbro_archive` in `src/modules/garbro/garbro_archive.h/cpp`:
  - Wraps `garbro::archive` (from bridge.h)
  - Adapts to same interface pattern as `archive::archive` but without extractor dependency
  - Converts `garbro::file_info` to internal file struct
- [ ] **2.2.3** [PROG-B] Verify tests pass, 100% coverage
- [ ] **2.2.4** [REVIEW-B] Review archive wrapper — check: exception translation, callback wiring, no resource leaks
- [ ] **2.2.5** [PROG-B] Fix review findings

---

## Phase 3: DLL Entry Points (Observer API)

Depends on Phase 2 interface being stable. Can start writing tests while Phase 1+2 finish.

### 3.1 — dll.cpp for GARbro Module

- [ ] **3.1.1** [PROG-C] Write tests for `OpenStorage`:
  - Valid archive → SOR_SUCCESS, storage handle set, StorageGeneralInfo populated
  - Invalid file → SOR_INVALID_FILE
  - Null storage pointer → SOR_INVALID_FILE
  - Format field ≤ 32 wchars
- [ ] **3.1.2** [PROG-C] Write tests for `CloseStorage`:
  - Close valid handle → no crash
  - Close null handle → no crash
- [ ] **3.1.3** [PROG-C] Write tests for `PrepareFiles`:
  - After open → TRUE
  - Null handle → FALSE
  - Double prepare → TRUE (idempotent)
- [ ] **3.1.4** [PROG-C] Write tests for `GetItem`:
  - Valid index → GET_ITEM_OK, StorageItemInfo populated (path, size, packed_size)
  - Index past end → GET_ITEM_NOMOREITEMS
  - Negative index → GET_ITEM_ERROR
  - Null handle → GET_ITEM_ERROR
  - Path encoding correct (Japanese filenames → wchar_t)
- [ ] **3.1.5** [PROG-C] Write tests for `ExtractItem`:
  - Valid extraction → SER_SUCCESS, file created at DestPath
  - Read error → SER_ERROR_READ
  - Write error → SER_ERROR_WRITE
  - User abort (callback returns false) → SER_USERABORT
  - Null handle → SER_ERROR_SYSTEM
- [ ] **3.1.6** [PROG-C] Write tests for `LoadSubModule` / `UnloadSubModule`:
  - LoadSubModule fills ModuleId, ModuleVersion, ApiVersion, ApiFuncs
  - All function pointers non-null
  - UnloadSubModule doesn't crash
- [ ] **3.1.7** [PROG-C] Implement `src/modules/garbro/dll.cpp`:
  - `LoadSubModule` → call `garbro::init(module_dir)`, fill params
  - `UnloadSubModule` → call `garbro::shutdown()`
  - `OpenStorage` → create garbro_archive, try open
  - Other functions follow existing dll.cpp pattern
- [ ] **3.1.8** [PROG-C] Verify tests pass, 100% coverage
- [ ] **3.1.9** [REVIEW-C] Review dll.cpp — check: matches existing module pattern, handle lifecycle, exception safety at C boundary, no exceptions escape extern "C"
- [ ] **3.1.10** [PROG-C] Fix review findings

---

## Phase 4: Integration Testing

Depends on Phases 1, 2, 3 all complete.

### 4.1 — End-to-End Tests via DLL Loading

- [ ] **4.1.1** [PROG-D] Create test archives from at least 5 different GARbro-supported formats:
  - KiriKiri XP3
  - AliceSoft ALD
  - Ren'Py RPA (overlap with existing module — verify both work)
  - RPG Maker (overlap with existing module — verify both work)
  - One more format (e.g., NScripter NSA, Majiro ARC)
- [ ] **4.1.2** [PROG-D] Generate `.expected.json` baselines for each test archive (hash + size)
- [ ] **4.1.3** [PROG-D] Write `src/tests/garbro.cpp` — Catch2 test cases using existing `test::observer` framework:
  - Load garbro.so module
  - Open each test archive
  - List files, verify count and names
  - Extract all files, verify hashes match baseline
- [ ] **4.1.4** [PROG-D] Run integration tests, fix failures
- [ ] **4.1.5** [REVIEW-D] Review integration tests — check: deterministic, no hardcoded paths, cleanup temp files, covers error paths too
- [ ] **4.1.6** [PROG-D] Fix review findings

### 4.2 — Coexistence Test

- [ ] **4.2.1** [PROG-D] Test that garbro module and existing modules (renpy, rpgmaker) can load simultaneously
- [ ] **4.2.2** [PROG-D] Test that Observer tries garbro module when existing modules reject a file
- [ ] **4.2.3** [PROG-D] Test that existing modules take priority for their registered extensions (*.rpa, *.rgss3a)

---

## Phase 5: Packaging

Can start partially during Phase 4.

### 5.1 — CPack Integration

- [ ] **5.1.1** [PROG-B] Add CPack rules for garbro module:
  - `garbro-{DATE}-{ARCH}-dll.zip` contents:
    - `garbro.so` (platform-specific)
    - `observer_user.ini` (generated by gen_ini)
    - `garbro/GARbro.Core.dll` (Any CPU, ILRepack-merged)
    - `garbro/Formats.dat`
    - `licenses/`
  - `garbro-{DATE}-{ARCH}-pdb.zip` with debug symbols
  - Both x86 and x64 packages (same GARbro.Core.dll, different garbro.so)
- [ ] **5.1.2** [REVIEW-B] Review packaging — check: all runtime files included, folder structure correct, .NET Framework 4.7.2 noted as prerequisite
- [ ] **5.1.3** [PROG-B] Fix review findings

---

## Phase 6: Final Review

- [ ] **6.1** [REVIEW-ALL] Full code review of all files:
  - Consistent style with existing ObserverModules code
  - No memory leaks across managed/native boundary
  - No exceptions escaping extern "C" functions
  - All error paths tested
  - 100% line + branch coverage confirmed
- [ ] **6.2** [PROG-ALL] Fix all final review findings
- [ ] **6.3** [REVIEW-ALL] Confirm zero open findings
- [ ] **6.4** Run full test suite (all modules including garbro), both x86 and x64
- [ ] **6.5** Build release packages for x86 and x64

---

## Parallelism Map

```
Phase 0.1 (bridge.h interface)
    │
    ├───────────────────────┬──────────────────┐
    ▼                       ▼                  ▼
Phase 0.2               Phase 0.3          Phase 0.4
(CMake skeleton)        (GARbro build      (gen_ini tool)
[PROG-B]                + ILRepack)        [PROG-C]
                        [PROG-B]
    │                       │                  │
    ├───────────────────────┴──────────────────┘
    │
    ├──────────────────┬──────────────────┐
    ▼                  ▼                  ▼
Phase 1             Phase 2            Phase 3.1.1-3.1.6
(bridge impl)       (archive layer)    (dll.cpp tests)
[PROG-A]            [PROG-B]           [PROG-C]
    │                  │                  │
    └──────────────────┴──────────────────┘
                       │
                       ▼
                 Phase 3.1.7-3.1.10
                 (dll.cpp impl)
                       │
                       ▼
                   Phase 4
                 (integration)
                   [PROG-D]
                       │
                       ▼
                   Phase 5
                 (packaging)
                       │
                       ▼
                   Phase 6
                 (final review)
```

## Agent Roles

| Role | Responsibility |
|------|---------------|
| **PROG-A** | Bridge layer (C++/CLI) — `bridge.h`, `bridge.cpp`, bridge tests |
| **PROG-B** | Build system + GARbro build + ILRepack + archive layer + mock + packaging |
| **PROG-C** | gen_ini tool + DLL entry points — `dll.cpp`, Observer API tests |
| **PROG-D** | Integration tests — end-to-end, coexistence, test data |
| **REVIEW-A** | Reviews PROG-A output |
| **REVIEW-B** | Reviews PROG-B output |
| **REVIEW-C** | Reviews PROG-C output |
| **REVIEW-D** | Reviews PROG-D output |
| **REVIEW-ALL** | Final cross-cutting review |

## Technical Notes

- `/clr` is incompatible with `/EHsc` — use `/EHa` for `bridge.cpp`
- `/clr` is incompatible with static CRT (`/MT`) — the entire garbro module must use `/MD` (dynamic CRT); this does NOT affect other modules (renpy, rpgmaker, zanzarah) which keep `/MT`
- `gcroot<T^>` is the correct way to store managed references in native classes (inside pimpl)
- GARbro uses `BinaryFormatter` for `Formats.dat` — requires .NET security settings in 4.7.2+
- ILRepack NuGet: `dotnet tool install --global ILRepack` or download from https://github.com/gluck/il-repack
- `GARbro.Core.dll` is Any CPU — works in both x86 and x64 CLR contexts
- Test archives should be small (< 1MB each) and committed to the test data directory
- To update GARbro: `cd extern/GARbro && git pull && cd ../.. && git add extern/GARbro && git commit`

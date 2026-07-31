# QuickBMS Observer Module — Implementation Plan

## Architecture

```
dll.cpp                    (C, Observer API exports — same pattern as other modules)
  → quickbms_archive.h/cpp  (pure C++, orchestration + scripts.ini parsing)
    → quickbms.lib           (QuickBMS compiled as static library, pure C)
```

Everything is **native C/C++**. No .NET, no interop, no managed code.

Does NOT use `extractor.h` — QuickBMS extraction is opaque (BMS script handles
everything internally via `dumpa()`), incompatible with chunk-based `decrypt()` model.

### Distribution Layout

```
modules/
├── quickbms.so                ← native C/C++ DLL
├── observer_user.ini          ← generated from scripts.ini (all mapped extensions)
└── quickbms/
    ├── scripts.ini            ← extension → BMS script mapping (user-editable)
    ├── scripts/
    │   ├── kirikiri_xp3.bms   ← example scripts (bundled)
    │   ├── unreal_pak.bms
    │   ├── unity_assets.bms
    │   ├── rpgmaker_vxace.bms
    │   └── ...
    └── docs/
        └── bms_syntax.md      ← BMS scripting reference
```

### Repository Layout

```
ObserverModules/
├── extern/
│   └── quickbms/                      ← git submodule or vendored source
│       ├── quickbms.c
│       ├── bms.c, cmd.c, perform.c, file.c, var.c, ...
│       ├── compression/
│       ├── encryption/
│       └── libs/
├── tools/
│   └── gen_quickbms_ini/              ← generates observer_user.ini from scripts.ini
├── src/
│   ├── api.h
│   ├── modules/
│   │   └── quickbms/
│   │       ├── quickbms_archive.h     ← pure C++ archive wrapper
│   │       ├── quickbms_archive.cpp
│   │       ├── quickbms_wrapper.h     ← C++ wrapper around QuickBMS C internals
│   │       ├── quickbms_wrapper.cpp
│   │       ├── scripts_config.h       ← scripts.ini parser
│   │       ├── scripts_config.cpp
│   │       ├── dll.cpp                ← Observer API exports
│   │       ├── quickbms.def
│   │       └── observer_user.ini      ← template
│   └── tests/
│       ├── quickbms.cpp               ← integration tests
│       ├── quickbms_wrapper_test.cpp  ← wrapper unit tests
│       └── scripts_config_test.cpp   ← config parser tests
└── CMakeLists.txt
```

### CI Pipeline

```
Step 1: git submodule update --init (quickbms)
Step 2: cmake --preset x64-release  (builds quickbms.lib + quickbms.so)
Step 3: cmake --preset x86-release  (builds quickbms.lib + quickbms.so)
Step 4: python/script gen observer_user.ini from scripts.ini
Step 5: ctest (run all tests)
Step 6: cpack (package x86 + x64 ZIPs)
```

---

## scripts.ini Format

```ini
# Extension → BMS script mapping
# Multiple scripts separated by comma: tried in order until one succeeds
# Lines starting with # are comments

[Scripts]
*.xp3 = kirikiri_xp3.bms
*.arc = arc_will.bms, arc_lzss.bms
*.pak = unreal_pak.bms, quake_pak.bms
*.dat = bgi_arc.bms, falcom_dat.bms
*.cpz = cmvs_cpz.bms
*.ypf = yumemiru_ypf.bms
*.nsa = nscripter_nsa.bms
*.wolf = wolfrpg.bms
```

No fallback — only explicitly mapped extensions are handled.
Observer `OpenStorage` returns `SOR_INVALID_FILE` for unmapped extensions.

---

## Phase 0: Preparation

### 0.1 — QuickBMS Static Library Build (BLOCKING)

- [ ] **0.1.1** [PROG-A] Add QuickBMS source as git submodule at `extern/quickbms`
- [ ] **0.1.2** [PROG-A] Patch `file.c` — add progress callback hook into `dumpa()`:
  - Add global function pointer: `int (*g_observer_progress_callback)(void *ctx, int64_t bytes) = NULL;`
  - Add global context pointer: `void *g_observer_progress_context = NULL;`
  - After each `write()` call inside `dumpa()`, invoke the callback:
    ```c
    if (g_observer_progress_callback) {
        if (!g_observer_progress_callback(g_observer_progress_context, bytes_written)) {
            return -1;  // user abort
        }
    }
    ```
  - This gives real-time progress updates and user abort support
  - Keep patch minimal — only touch `dumpa()` write loop
- [ ] **0.1.3** [PROG-A] Create CMake target for `quickbms_lib` (static library):
  - Compile all QuickBMS .c files except `main()` wrapper
  - Define `QUICKBMS_AS_LIB` or similar preprocessor macro
  - Handle the `#include`-based build: QuickBMS includes .c files from quickbms.c
  - Static link all compression/encryption deps (zlib, lzma, etc. — already vendored in quickbms)
  - Suppress warnings from QuickBMS code (`/W0` or pragma push/pop)
  - Build for both x86 and x64
- [ ] **0.1.4** [PROG-A] Verify `quickbms_lib` compiles cleanly on both architectures
- [ ] **0.1.5** [REVIEW-A] Review CMake integration and `dumpa()` patch — check: no symbol conflicts with other modules, all deps statically linked, no undefined symbols, patch is minimal and correct, abort path doesn't leak resources
- [ ] **0.1.6** [PROG-A] Fix review findings

### 0.2 — C++ Wrapper Interface Design (BLOCKING for Phases 1-3)

- [ ] **0.2.1** [PROG-B] Define `quickbms_wrapper.h` — clean C++ interface over QuickBMS C internals:
  ```cpp
  namespace quickbms {
      struct file_entry {
          std::string name;
          int64_t offset;
          int64_t size;          // uncompressed
          int64_t packed_size;   // compressed
      };

      // Progress callback: receives context + bytes written.
      // Return true to continue, false to abort.
      using progress_callback = std::function<bool(int64_t bytes)>;

      class engine {
      public:
          engine();
          ~engine();
          bool load_script(const std::filesystem::path &bms_path);
          bool open_archive(const std::filesystem::path &archive_path);
          std::vector<file_entry> list_files();
          bool extract_file(size_t index, const std::filesystem::path &dest_path,
                           progress_callback progress);
          void reset();
      private:
          struct impl;
          std::unique_ptr<impl> impl_;
      };
  }
  ```
  - Pimpl hides all QuickBMS globals
  - Each `engine` instance assumes exclusive access (QuickBMS global state)
  - `reset()` calls `bms_init(0)` to clean up between uses
  - `extract_file()` sets `g_observer_progress_callback` / `g_observer_progress_context`
    before calling `start_bms()`, resets them after. The patched `dumpa()` calls back
    per write chunk, giving real-time progress and user abort support.
- [ ] **0.2.2** [REVIEW-B] Review wrapper interface — check: no C types leak, lifecycle correct, thread-safety documented (single-threaded only)
- [ ] **0.2.3** [PROG-B] Fix review findings

### 0.3 — scripts.ini Parser Design (parallel with 0.1, 0.2)

- [ ] **0.3.1** [PROG-C] Define `scripts_config.h`:
  ```cpp
  namespace quickbms {
      struct script_mapping {
          std::string extension;           // e.g. "xp3"
          std::vector<std::string> scripts; // e.g. ["kirikiri_xp3.bms"]
      };

      class scripts_config {
      public:
          explicit scripts_config(const std::filesystem::path &ini_path);
          std::vector<std::string> find_scripts(const std::string &extension) const;
          std::vector<std::string> all_extensions() const;
      private:
          std::vector<script_mapping> mappings_;
      };
  }
  ```
- [ ] **0.3.2** [REVIEW-C] Review config interface — check: case-insensitive extension matching, handles dots/wildcards correctly
- [ ] **0.3.3** [PROG-C] Fix review findings

---

## Phase 1: scripts.ini Parser

Can start immediately after 0.3 is finalized.

### 1.1 — Implementation

- [ ] **1.1.1** [PROG-C] Write tests for `scripts_config`:
  - Parse valid INI → correct mappings
  - Extension lookup case-insensitive ("XP3" == "xp3")
  - Multiple scripts per extension → returned in order
  - Unknown extension → empty vector
  - `all_extensions()` returns sorted unique list
  - Missing file → throws
  - Empty file → no mappings
  - Comments (# lines) ignored
  - Malformed lines skipped gracefully
- [ ] **1.1.2** [PROG-C] Implement `scripts_config.cpp`
- [ ] **1.1.3** [PROG-C] Verify tests pass, 100% line+branch coverage
- [ ] **1.1.4** [REVIEW-C] Review — check: no buffer overflows on long lines, handles BOM, handles \r\n and \n
- [ ] **1.1.5** [PROG-C] Fix review findings

---

## Phase 2: QuickBMS Wrapper

Depends on Phase 0.1 (static lib) and 0.2 (wrapper interface).
Can run **in parallel** with Phase 1 and Phase 3.

### 2.1 — Init / Reset / Cleanup

- [ ] **2.1.1** [PROG-A] Write tests for `quickbms::engine` construction and destruction:
  - Constructor initializes QuickBMS (`quickbms_dll_init`, `bms_init`)
  - Destructor cleans up (`bms_finish`)
  - `reset()` clears state for reuse
  - Double reset is safe
- [ ] **2.1.2** [PROG-A] Implement constructor, destructor, `reset()` in `quickbms_wrapper.cpp`
- [ ] **2.1.3** [PROG-A] Verify tests pass, 100% coverage
- [ ] **2.1.4** [REVIEW-A] Review — check: all QuickBMS globals properly initialized, no memory leaks from QuickBMS internal allocations
- [ ] **2.1.5** [PROG-A] Fix review findings

### 2.2 — Script Loading

- [ ] **2.2.1** [PROG-A] Write tests for `load_script()`:
  - Valid .bms file → returns true
  - Non-existent file → returns false
  - Invalid/empty script → returns false
  - Load replaces previous script (after reset)
- [ ] **2.2.2** [PROG-A] Implement `load_script()`:
  - Call `bms_init(0)` to reset state
  - Open script file
  - Call `parse_bms(fds, NULL, 0, 0)`
  - Return success/failure
- [ ] **2.2.3** [PROG-A] Verify tests pass, 100% coverage
- [ ] **2.2.4** [REVIEW-A] Review — check: file handle closed on error, state consistent after failure
- [ ] **2.2.5** [PROG-A] Fix review findings

### 2.3 — Archive Opening + File Listing

- [ ] **2.3.1** [PROG-A] Write tests for `open_archive()` + `list_files()`:
  - Valid archive + matching script → file list with correct names, sizes
  - Wrong script for archive → returns false or empty list
  - `list_files()` without `open_archive()` → empty list
  - File names contain subdirectories → paths preserved
  - Packed entries → packed_size != size
- [ ] **2.3.2** [PROG-A] Implement `open_archive()` and `list_files()`:
  - `open_archive()`: call `fdnum_open(path, 0, 1)`
  - `list_files()`: set `g_list_only = 1`, call `start_bms(...)`, iterate `g_extracted_file` linked list
  - Convert `extracted_file_t` → `quickbms::file_entry`
- [ ] **2.3.3** [PROG-A] Verify tests pass, 100% coverage
- [ ] **2.3.4** [REVIEW-A] Review — check: g_list_only properly set/unset, extracted_file_t iteration safe, memory ownership
- [ ] **2.3.5** [PROG-A] Fix review findings

### 2.4 — File Extraction with Progress

- [ ] **2.4.1** [PROG-A] Write tests for `extract_file()`:
  - Extract known file → output matches expected bytes (hash check)
  - Extract compressed file → decompressed correctly
  - Progress callback called with byte counts (per write chunk from `dumpa()`)
  - Progress callback returning false → extraction aborts, returns false
  - After abort, engine is in clean state (can extract another file)
  - Index out of range → returns false
  - Dest path with subdirectories → created automatically
- [ ] **2.4.2** [PROG-A] Implement `extract_file()`:
  - Set `g_list_only = 0`, `g_void_dump = 0`
  - Set `g_output_folder` to dest directory
  - Install progress hook: set `g_observer_progress_callback` to a static trampoline
    that calls the `progress_callback`, set `g_observer_progress_context` to `this`
  - Re-run `start_bms(...)` — QuickBMS re-executes script and extracts
  - Filter output to only the requested file (by index/name)
  - `dumpa()` calls our hook per write chunk → real-time progress to Observer
  - If hook returns false (user abort), `dumpa()` returns -1, script terminates
  - Uninstall progress hook after extraction (set both globals to NULL)
  - Note: QuickBMS re-executes entire script per extract; acceptable for typical use
- [ ] **2.4.3** [PROG-A] Verify tests pass, 100% coverage
- [ ] **2.4.4** [REVIEW-A] Review — check: hook installed/uninstalled correctly (RAII guard), output path injection (sanitize filenames), abort leaves no partial files, re-execution overhead acceptable
- [ ] **2.4.5** [PROG-A] Fix review findings

---

## Phase 3: Archive Layer + DLL Entry Points

Can run **in parallel** with Phase 2 using mock wrapper.

### 3.1 — Mock Wrapper

- [ ] **3.1.1** [PROG-B] Create `mock_quickbms_wrapper.h/cpp` — test double for `quickbms::engine`:
  - Configurable: set file list, set extract behavior
  - Tracks calls: script loaded, archive opened, files extracted
  - Configurable error injection
- [ ] **3.1.2** [REVIEW-B] Review mock
- [ ] **3.1.3** [PROG-B] Fix review findings

### 3.2 — quickbms_archive

- [ ] **3.2.1** [PROG-B] Write tests for `quickbms_archive`:
  - `open(path)` → reads scripts.ini, finds scripts for extension, tries each until one works
  - `open(path)` with unmapped extension → throws (SOR_INVALID_FILE)
  - `open(path)` with mapped extension but wrong format → tries all scripts, then throws
  - `prepare_files()` → delegates to engine.list_files()
  - `get_file(index)` → returns file info
  - `get_file()` out of range → throws out_of_range
  - `extract_file()` → delegates to engine.extract_file() with progress callback
  - `extract_file()` user abort → throws user_interrupt
  - Path separators normalized to backslash
  - `archive_info` format field contains BMS script name or detected format
- [ ] **3.2.2** [PROG-B] Implement `quickbms_archive` in `src/modules/quickbms/quickbms_archive.h/cpp`:
  - Owns `quickbms::engine` and `quickbms::scripts_config`
  - On `open()`: get extension, look up scripts, try each with engine
  - Stores successful script name and file list
- [ ] **3.2.3** [PROG-B] Verify tests pass, 100% coverage
- [ ] **3.2.4** [REVIEW-B] Review — check: exception translation, script path resolution, lifecycle
- [ ] **3.2.5** [PROG-B] Fix review findings

### 3.3 — dll.cpp

- [ ] **3.3.1** [PROG-C] Write tests for Observer API functions:
  - `LoadSubModule` → fills ModuleId, ModuleVersion, ApiVersion, ApiFuncs
  - `OpenStorage` → valid archive with mapped extension → SOR_SUCCESS
  - `OpenStorage` → unmapped extension → SOR_INVALID_FILE
  - `CloseStorage` → no crash (null and valid handle)
  - `PrepareFiles` → TRUE after open
  - `GetItem` → correct file info, GET_ITEM_NOMOREITEMS at end
  - `ExtractItem` → SER_SUCCESS, file created, SER_USERABORT on cancel
  - `UnloadSubModule` → clean shutdown
- [ ] **3.3.2** [PROG-C] Implement `src/modules/quickbms/dll.cpp`:
  - `LoadSubModule` → resolve module dir via `GetModuleFileName`, load scripts.ini from `quickbms/` subfolder
  - `OpenStorage` → create quickbms_archive, try open
  - Other functions follow existing dll.cpp pattern
- [ ] **3.3.3** [PROG-C] Create `src/modules/quickbms/quickbms.def`
- [ ] **3.3.4** [PROG-C] Verify tests pass, 100% coverage
- [ ] **3.3.5** [REVIEW-C] Review dll.cpp — check: no exceptions escape extern "C", handle lifecycle, path resolution
- [ ] **3.3.6** [PROG-C] Fix review findings

---

## Phase 4: Integration Testing

Depends on Phases 1, 2, 3 all complete.

### 4.1 — End-to-End Tests

- [ ] **4.1.1** [PROG-D] Select 3-5 test archives with publicly available BMS scripts:
  - At least one simple format (no compression)
  - At least one with compression
  - At least one with encryption
  - Small archives (< 1MB each)
- [ ] **4.1.2** [PROG-D] Create scripts.ini with mappings for test archives
- [ ] **4.1.3** [PROG-D] Generate `.expected.json` baselines (hash + size)
- [ ] **4.1.4** [PROG-D] Write `src/tests/quickbms.cpp` — Catch2 tests using `test::observer` framework:
  - Load quickbms.so module
  - Open each test archive
  - List files, verify count and names
  - Extract all files, verify hashes
- [ ] **4.1.5** [PROG-D] Run integration tests, fix failures
- [ ] **4.1.6** [REVIEW-D] Review — check: deterministic, no hardcoded paths, temp cleanup
- [ ] **4.1.7** [PROG-D] Fix review findings

### 4.2 — Coexistence Test

- [ ] **4.2.1** [PROG-D] Test that quickbms.so, garbro.so, and existing modules load simultaneously
- [ ] **4.2.2** [PROG-D] Test module priority: native modules → garbro → quickbms (by extension ordering in observer_user.ini)

---

## Phase 5: Documentation & Example Scripts

Can run **in parallel** with Phase 4.

### 5.1 — BMS Syntax Reference

- [ ] **5.1.1** [PROG-D] Find or write `quickbms/docs/bms_syntax.md`:
  - Core commands: Get, Set, Log, CLog, GoTo, For/Next, If/Else/EndIf
  - Data types: Long, Short, Byte, String, ThreeByte, etc.
  - Math operations
  - Compression (ComType) and Encryption commands
  - Variables: FILENAME, FILESIZE, etc.
  - Example script walkthrough
  - Link to full QuickBMS documentation (zenhax.com/quickbms)
- [ ] **5.1.2** [REVIEW-D] Review documentation — check: accurate, covers essentials for writing custom scripts
- [ ] **5.1.3** [PROG-D] Fix review findings

### 5.2 — Example Scripts

- [ ] **5.2.1** [PROG-D] Bundle 5-10 popular BMS scripts in `quickbms/scripts/`:
  - Include license/attribution for each script
  - Cover variety: simple raw, compressed, encrypted
  - Include a `README.txt` in scripts/ explaining how to add more
- [ ] **5.2.2** [REVIEW-D] Review script selection — check: licenses allow redistribution, scripts tested
- [ ] **5.2.3** [PROG-D] Fix review findings

### 5.3 — observer_user.ini Generator

- [ ] **5.3.1** [PROG-C] Create script/tool that reads `scripts.ini` and generates `observer_user.ini`:
  - Extracts all extensions from `[Scripts]` section
  - Outputs `[Filters]` with comma-separated `*.ext` list
  - Simple: Python script or shell script (no need for C#)
- [ ] **5.3.2** [REVIEW-C] Review generator
- [ ] **5.3.3** [PROG-C] Fix review findings

---

## Phase 6: Packaging

### 6.1 — CPack Integration

- [ ] **6.1.1** [PROG-B] Add CPack rules for quickbms module:
  - `quickbms-{DATE}-{ARCH}-dll.zip` contents:
    - `quickbms.so` (platform-specific)
    - `observer_user.ini` (generated)
    - `quickbms/scripts.ini`
    - `quickbms/scripts/*.bms` (example scripts)
    - `quickbms/docs/bms_syntax.md`
    - `licenses/`
  - `quickbms-{DATE}-{ARCH}-pdb.zip` with debug symbols
  - Both x86 and x64 packages
- [ ] **6.1.2** [REVIEW-B] Review packaging — check: all files included, scripts.ini editable post-install
- [ ] **6.1.3** [PROG-B] Fix review findings

---

## Phase 7: Final Review

- [ ] **7.1** [REVIEW-ALL] Full code review:
  - Consistent style with existing ObserverModules code
  - No memory leaks (QuickBMS global state properly managed)
  - No exceptions escaping extern "C" functions
  - All error paths tested
  - 100% line + branch coverage confirmed
  - QuickBMS global state properly reset between archives
- [ ] **7.2** [PROG-ALL] Fix all final review findings
- [ ] **7.3** [REVIEW-ALL] Confirm zero open findings
- [ ] **7.4** Run full test suite (all modules), both x86 and x64
- [ ] **7.5** Build release packages for x86 and x64

---

## Parallelism Map

```
Phase 0.1 (quickbms.lib)     Phase 0.2 (wrapper.h)     Phase 0.3 (config.h)
[PROG-A]                     [PROG-B]                  [PROG-C]
    │                             │                         │
    │                             │                         ▼
    │                             │                     Phase 1
    │                             │                     (config parser)
    │                             │                     [PROG-C]
    │                             │                         │
    ├─────────────────────────────┘                         │
    ▼                                                       │
Phase 2                       Phase 3.1 (mock)              │
(wrapper impl)                [PROG-B]                      │
[PROG-A]                          │                         │
    │                             ▼                         │
    │                         Phase 3.2-3.3                 │
    │                         (archive + dll.cpp)           │
    │                         [PROG-B, PROG-C]              │
    │                             │                         │
    └─────────────────────────────┴─────────────────────────┘
                                  │
                                  ▼
                    Phase 4 (integration)  ←→  Phase 5 (docs + scripts)
                    [PROG-D]                   [PROG-D, PROG-C]
                                  │
                                  ▼
                            Phase 6 (packaging)
                            [PROG-B]
                                  │
                                  ▼
                            Phase 7 (final review)
```

## Agent Roles

| Role | Responsibility |
|------|---------------|
| **PROG-A** | QuickBMS static lib build + C++ wrapper (`quickbms_wrapper.*`) |
| **PROG-B** | CMake integration + mock + archive layer + packaging |
| **PROG-C** | scripts.ini parser + dll.cpp + observer_user.ini generator |
| **PROG-D** | Integration tests + documentation + example scripts |
| **REVIEW-A** | Reviews PROG-A output |
| **REVIEW-B** | Reviews PROG-B output |
| **REVIEW-C** | Reviews PROG-C output |
| **REVIEW-D** | Reviews PROG-D output |
| **REVIEW-ALL** | Final cross-cutting review |

## Technical Notes

- **Symbol visibility**: `quickbms.so` must export ONLY `LoadSubModule` and `UnloadSubModule` (via `.def` file). All QuickBMS internals (700+ compression funcs, crypto, globals) must be hidden:
  - `.def` file lists only the two exports — MSVC exports nothing else by default for DLLs with `.def`
  - QuickBMS static lib (`quickbms_lib`) is linked with `/OPT:REF` to strip unused code
  - All QuickBMS source compiled without `__declspec(dllexport)` — verify no accidental exports
  - This also solves symbol conflicts: if `garbro.so` and `quickbms.so` both link zlib, their internal zlib symbols are hidden and don't clash
- **Progress callback**: Patched `dumpa()` in `file.c` calls `g_observer_progress_callback` after each write. This gives real-time progress bars in FAR Manager and user abort support (callback returns false → `dumpa()` returns -1 → script terminates).
- **Global state**: QuickBMS uses ~50 global variables. Only one archive can be open at a time. `bms_init(0)` resets everything between uses.
- **Build quirk**: QuickBMS `#include`s .c files from `quickbms.c`. To build as library, either:
  - Compile `quickbms.c` with a macro that excludes `main()`, OR
  - Extract the `#include` list and compile files separately
- **Re-execution on extract**: `start_bms()` must re-run the entire script to extract a file. For archives with many files, this means O(N) re-executions. Acceptable for typical use (user extracts one file at a time in FAR Manager).
- **Output redirection**: QuickBMS writes to `g_output_folder`. For `ExtractItem`, set this to the directory of `DestPath` and filter by filename match.
- **No thread safety**: QuickBMS is inherently single-threaded due to global state. Observer calls are sequential, so this is fine.
- **Compression deps**: All vendored in `extern/quickbms/compression/` and `libs/` — no external dependencies needed.

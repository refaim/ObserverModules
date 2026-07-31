# Current Code Deep Dive

Static review of the current ObserverModules implementation. The project was not built and the tests were not run during this review.

## Scope

The review covered:

- the Observer ABI declarations and exported entry points;
- the common archive wrapper and extraction lifecycle;
- the Ren'Py, RPG Maker, and Zanzarah format implementations;
- the custom Ren'Py Pickle parser;
- the test harness and all current test cases;
- the history of the main format-related changes.

## Architecture

```text
FAR Manager
  -> Observer
      -> renpy.so
      -> rpgmaker.so
      -> zanzarah.so
           -> dll.cpp       Observer C ABI and HANDLE lifecycle
           -> archive.cpp   shared listing and extraction pipeline
           -> format.cpp    format-specific parser and decryption
```

There are two distinct plugin boundaries:

1. Observer dynamically loads each Windows DLL and obtains its function table through `LoadSubModule()`.
2. Inside a module, the archive format is selected statically at link time. Each CMake target links the common `dll.cpp` and `archive.cpp` with one implementation of the non-virtual `extractor::extractor` methods.

Despite its name, `extractor::extractor` is not a runtime-polymorphic format interface. Its destructor is virtual, but `get_archive_info()`, `list_files()`, and `decrypt()` are not. This distinction matters when evolving the internal module architecture.

### Storage lifecycle

1. `OpenStorage()` validates the supplied signature when present, opens an `ifstream`, and returns an `archive::archive*` cast to `HANDLE`.
2. `PrepareFiles()` parses and caches the archive index.
3. `GetItem()` exposes paths and sizes to Observer.
4. `ExtractItem()` copies a selected entry in 128 KiB chunks and invokes the progress callback.
5. `CloseStorage()` reconstructs a `unique_ptr` from the `HANDLE` and destroys the archive.

The common archive layer is a useful separation: format implementations provide indexing and block transformation while file I/O and the Observer-facing lifecycle remain shared.

## Format implementations

### Ren'Py

- Recognizes the `RPA-` signature and versions 2.0 and 3.0.
- Reads the compressed index at the offset stored in the archive header.
- RPA-3 offsets and lengths are decoded with the header key.
- Decompresses the whole index into memory with zlib/zstr.
- Parses the index with the local Pickle subset.
- Supports the optional per-entry prefix/header and excludes its length from the body copied from the archive.

The implementation only accepts one tuple segment per archive path. A valid index containing multiple segments reaches the explicit `Not implemented` branch in [`renpy.cpp`](../src/modules/renpy/renpy.cpp#L101).

### RPG Maker VX Ace

- Recognizes `RGSSAD\0\3`.
- Decodes index fields and file names using the archive magic.
- Stores a separate initial magic value for each file.
- Decrypts file bodies as a rolling 32-bit XOR stream.

The shared extraction buffer is divisible by four, which is important for preserving the rolling-magic state between chunks.

### Zanzarah

- Recognizes four zero bytes.
- Reads the file count followed by path, relative offset, and block size records.
- Treats data offsets as relative to the end of the index.
- Skips the four-byte per-block attribute and removes one leading `..\` from entry paths.

## Confirmed defects

### 1. Cancellation is reported as success

The progress adapter throws `archive::user_interrupt` when Observer requests cancellation in [`dll.cpp`](../src/dll.cpp#L110). However, [`archive::extract_file()`](../src/archive.cpp#L111) catches that exception and returns normally.

Consequences:

- the `SER_USERABORT` handler in `ExtractItem()` is unreachable for this path;
- Observer receives `SER_SUCCESS`;
- a partially extracted file remains at the destination.

The exception should reach the ABI adapter, or cancellation should be returned explicitly through the internal API.

### 2. Zanzarah uses vector capacity as the file count

[`zanzarah.cpp`](../src/modules/zanzarah/zanzarah.cpp#L59) calls `reserve(file_count)` and then iterates until `files.capacity()`.

The C++ standard only guarantees that capacity is at least the requested count. If an implementation allocates additional capacity, the parser reads records beyond the archive index. The decoded count must be stored separately and used as the loop bound.

### 3. Small archives break the test harness

The test adapter enables `failbit` exceptions, allocates a 128 KiB signature buffer, and unconditionally requests the entire buffer in [`observer.cpp`](../src/tests/framework/observer.cpp#L54). `ifstream::read()` sets `failbit` when a valid archive is shorter than 128 KiB, so `gcount()` is never used normally for such a file.

The harness should read up to the available length without treating a short final read as a test failure.

## Robustness and security risks

### Untrusted lengths and offsets

The parsers use archive-controlled values for allocations and stream positions without consistently checking them against the physical file size:

- Zanzarah file count, `path_len`, block offset, and block size;
- RPG Maker `name_len`, file offset, and file size;
- Ren'Py index offset, decoded entry offset and size, and decompressed index size.

A malformed archive can therefore cause excessive allocation, negative logical sizes, out-of-range seeks, or exceptions outside the intended format-specific error mapping. A bounded binary reader with checked arithmetic would remove most of this duplicated risk.

### ABI pointer and structure validation

The exported functions do not fully validate their C ABI inputs:

- `OpenStorage()` checks `storage` but not `params.FilePath` or `info`;
- `GetItem()` does not check `item_info`;
- `ExtractItem()` assumes `Callbacks.FileProgress` is non-null;
- `LoadSubModule()` does not check its pointer or `StructSize` and is declared `noexcept`;
- the `StructSize` members supplied by Observer are otherwise ignored.

Invalid host input can produce an access violation instead of a defined Observer error result.

### Missing signature data disables format validation

[`archive::open()`](../src/archive.cpp#L28) verifies the signature only when the `Data` span is non-empty. With empty signature data, any readable file reaches the format-specific `get_archive_info()`, which currently returns constant metadata.

Whether this is observable in production depends on Observer's exact two-stage open protocol. The test harness explicitly notes that it does not yet reproduce that protocol.

### Entry path handling

Archive paths are mostly passed through unchanged except for slash replacement. Zanzarah removes only one leading `..\`; the other formats do not normalize traversal components.

The final impact depends on how Observer constructs `DestPath`, because the module receives the destination rather than joining it itself. Nevertheless, the trust boundary should be made explicit and entry paths should be validated before exposure.

### Exceptions at the ABI boundary

The exported functions catch common `runtime_error` and `logic_error` cases, but allocation failures and some invalid-input failures are not covered consistently. C++ exceptions must not be allowed to escape into the Observer ABI.

Header writes are also outside the body-write error mapping in `archive::extract_file()`, so an output failure while writing an entry header is reported as a generic system error rather than `SER_ERROR_WRITE`.

## Ren'Py Pickle compatibility debt

The local parser is deliberately a subset rather than a general Pickle implementation. Important limitations include:

- `BINPUT`, `LONG_BINPUT`, and `MEMOIZE` store null placeholders;
- `BINGET` and `LONG_BINGET` push `None` instead of the memoized value;
- several declared opcodes have no implementation;
- `BINFLOAT` reads the payload as little-endian even though the Pickle opcode uses big-endian representation;
- `LONG1` values longer than eight bytes can overflow the signed 64-bit accumulator;
- frame sizes and protocol versions are read but not validated.

The current real-world corpus evidently stays inside the supported subset, but support for all valid RPA-2.0/RPA-3.0 indexes should not be assumed.

## Extraction semantics

- Progress is reported for body chunks but not for an optional Ren'Py entry header, even though the header contributes to the size exposed by `GetItem()`.
- Partial output is not cleaned up after cancellation or read failure.
- One archive object owns one mutable `ifstream`; concurrent extraction through the same storage handle would race on `seekg()` and `read()`.
- An empty archive is reparsed on each `PrepareFiles()` call because an empty `files_` vector is also used as the "not prepared" state.

These may be acceptable under current Observer call patterns, but those assumptions are not encoded in the internal API.

## Existing test coverage

The repository contains 26 end-to-end golden tests:

- 14 Ren'Py archives;
- 9 RPG Maker archives;
- 3 Zanzarah archives.

Each test compares the listed path, extracted size, and XXH3 hash. Loading all three modules for every archive also checks that exactly one module accepts the signature. This is a strong regression suite for the known corpus.

Current gaps:

- malformed and truncated archives;
- boundary values for lengths, counts, offsets, and headers;
- cancellation and progress accounting;
- invalid ABI pointers and structure sizes;
- short archives below 128 KiB;
- multi-segment Ren'Py entries and Pickle memo references;
- concurrency assumptions;
- the real Observer two-stage `OpenStorage()` sequence.

The corpus and expected listings live outside the repository under `M:\observer\_test`, so the tests are not self-contained.

## Suggested order for later remediation

1. Fix cancellation propagation and Zanzarah's capacity loop.
2. Introduce checked/bounded binary reads and validate entry ranges against the archive size.
3. Harden every exported ABI entry point and guarantee that no exception crosses it.
4. Decide whether to complete the Pickle subset or replace it with a narrower parser designed specifically for Ren'Py indexes.
5. Add focused unit and negative tests alongside the existing real-archive golden tests.
6. Define path-normalization, partial-output, progress, and concurrency contracts explicitly.

Build-system and development-workflow redesign are intentionally outside the scope of this document and can be addressed separately.

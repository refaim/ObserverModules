# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build Commands

**IMPORTANT: This project ONLY builds on Windows with MSVC toolchain. Do not attempt to build on Linux.**

This project uses CMake with vcpkg for dependency management and requires Visual Studio 2017+ on Windows:

```bash
# Configure build (requires VCPKG_ROOT environment variable)
cmake --preset x64-release  # or x86-release

# Build all modules
cmake --build build/x64-release

# Run tests
cd build/x64-release && ctest

# Package for distribution
cd build/x64-release && cpack --config CPackConfig.cmake -C RelWithDebInfo
```

Build presets available: `x64-release`, `x64-debug`, `x86-release`, `x86-debug`

## Development Guidelines

This project uses **C++23** and follows KISS (Keep It Simple, Stupid) and DRY (Don't Repeat Yourself) principles.

**Avoid magic numbers** - use named constants instead.

## Architecture Overview

This project implements Observer plugin modules for FAR Manager that handle exotic archive formats. The codebase follows a plugin architecture where each module implements the Observer API to support different archive formats.

### Core Components

- **API Layer** (`src/api.h`, `src/dll.cpp`): Implements the Observer plugin API with standard functions like `OpenStorage`, `CloseStorage`, `GetItem`, `ExtractItem`
- **Archive Wrapper** (`src/archive.h`, `src/archive.cpp`): Provides a unified interface that wraps format-specific extractors
- **Extractor Interface** (`src/modules/extractor.h`): Defines the abstract interface that all format extractors must implement

### Module Structure

Each supported format has its own module under `src/modules/`:
- `renpy/`: RenPy visual novel archives (.rpa files) with pickle support
- `zanzarah/`: Zanzarah game archives (.pak files)  
- `rpgmaker/`: RPG Maker archives (in development)

Each module contains:
- Format-specific implementation (e.g., `renpy.cpp`)
- Module definition file (`.def`) for DLL exports
- Configuration file (`observer_user.ini`)

### Data Flow

1. FAR Manager loads the module DLL via `LoadSubModule()`
2. `OpenStorage()` creates an archive wrapper with format-specific extractor
3. `PrepareFiles()` scans and indexes archive contents
4. `GetItem()` provides file metadata for FAR's file browser
5. `ExtractItem()` handles actual file extraction with progress callbacks

### Testing Framework

Located in `src/tests/` with a custom framework (`framework/observer.h`) that simulates the Observer API for testing archive operations without requiring FAR Manager.
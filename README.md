# Refaim's Observer Modules for FAR Manager

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![CI](https://github.com/refaim/ObserverModules/actions/workflows/main.yml/badge.svg)](https://github.com/refaim/ObserverModules/actions/workflows/main.yml)

## About the Project

This is a collection of my extension modules for the Observer plugin for FAR Manager file manager. These modules allow
you to browse and extract various exotic archive formats directly from FAR Manager without the need for separate
extraction tools. The archives open as regular folders, letting you navigate through their contents and extract only
specific files as needed without having to unpack the entire archive.

## Supported Formats

### Ren'Py Visual Novel Engine

- Supported extensions: `.rpa`
- Compatibility: RPA-3.0
- Description: Archives used in Ren'Py visual novels to store game resources (graphics, sound, scripts)

### Zanzarah: The Hidden Portal

- Supported extension: `.pak`
- Compatibility: Supports archives from both the original game version and the Steam version
- Description: Archive used to store all game resources

## Installation and Usage

### Prerequisites

- Installed **x64** version of [FAR Manager](https://farmanager.com/download.php?l=en)
- Installed **x64** version of [Observer](https://github.com/lazyhamster/Observer/releases) plugin

### Module Installation

1. Download the [latest release](https://github.com/refaim/ObserverModules/releases/tag/nightly) of the module you're
   interested in
2. Note that the archive contains an observer_user.ini file. If you're installing multiple modules or already have this
   file in your Observer modules folder, you'll need to manually merge these ini files to ensure all modules work
   correctly
3. Extract the contents of the archive to the `%FARHOME%\Plugins\Observer\modules\` directory (where `%FARHOME%` is the
   folder with your FAR Manager installation)
4. Restart FAR Manager for the changes to take effect

### Working with Archives

1. Find an archive of the supported format in FAR Manager
2. Press Enter or Ctrl+PgDn on this file
3. FAR Manager will use Observer to automatically detect the format and display the archive contents as a regular
   directory
4. You can browse the contents and extract files using standard FAR commands (F5 key for copying)

## Links

[Support Forum (in Russian)](https://forum.farmanager.com/viewtopic.php?t=12729)

## Third-party components

| Project                                             | License                             |
|-----------------------------------------------------|-------------------------------------|
| [json](https://github.com/nlohmann/json)            | [MIT](licenses/json.txt)            |
| [Catch2](https://github.com/catchorg/Catch2)        | [BSL-1.0](licenses/Boost.txt)       |
| [Observer](https://github.com/lazyhamster/Observer) | [LGPL-3.0](licenses/Observer.txt)   |
| [Python](https://www.python.org)                    | [PSF-2.0](licenses/Python.txt)      |
| [xxHash](https://github.com/Cyan4973/xxHash)        | [BSD-2-Clause](licenses/xxHash.txt) |
| [zlib](https://zlib.net)                            | [zlib](licenses/zlib.txt)           |
| [zstr](https://github.com/mateidavid/zstr)          | [MIT](licenses/zstr.txt)            |

## Sources of inspiration

| Project                                                          | License                          |
|------------------------------------------------------------------|----------------------------------|
| [rgssad](https://github.com/luxrck/rgssad)                       | [MIT](licenses/rgssad.txt)       |
| [rpatool](https://github.com/Shizmob/rpatool)                    | [WTFPL](licenses/rpatool.txt)    |
| [zanzapak](https://aluigi.altervista.org/papers.htm#others-file) | [GPL-3.0](licenses/zanzapak.txt) |

## Building from Source

### Prerequisites

- **Visual Studio 2017** compiler
- **CLion** and/or **CMake** (version 3.31+)
- **vcpkg**

You can open the included CLion project directly and build through the IDE or use CMake manually to generate the build
files, then compile using the Visual Studio compiler.

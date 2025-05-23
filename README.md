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
- Compatibility: RPA-2.0, RPA-3.0
- Description: Archives used in Ren'Py visual novels to store game resources (graphics, sound, scripts)

### RPG Maker

- Supported extensions: `.rgss3a`
- Compatibility: RPG Maker VX Ace (RGSS3)
- Description: Encrypted archives containing game assets like graphics, audio, and data files

### Zanzarah: The Hidden Portal

- Supported extensions: `.pak`
- Compatibility: Supports archives from both the original game version and the Steam version
- Description: Archive used to store all game resources

## Installation and Usage

### Prerequisites

- [FAR Manager](https://farmanager.com/download.php?l=en) (x86 or x64)
- [Observer](https://github.com/lazyhamster/Observer/releases) plugin (x86 or x64)

### Module Installation

1. Download the [latest release](https://github.com/refaim/ObserverModules/releases/latest) of the module you're
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

| Project                                                         | License                             |
|-----------------------------------------------------------------|-------------------------------------|
| [nlohmann/json](https://github.com/nlohmann/json)               | [MIT](licenses/json.txt)            |
| [catchorg/Catch2](https://github.com/catchorg/Catch2)           | [BSL-1.0](licenses/Catch2.txt)      |
| [lazyhamster/Observer](https://github.com/lazyhamster/Observer) | [LGPL-3.0](licenses/Observer.txt)   |
| [Cyan4973/xxHash](https://github.com/Cyan4973/xxHash)           | [BSD-2-Clause](licenses/xxHash.txt) |
| [zlib](https://zlib.net)                                        | [zlib](licenses/zlib.txt)           |
| [mateidavid/zstr](https://github.com/mateidavid/zstr)           | [MIT](licenses/zstr.txt)            |

## Sources of inspiration

| Project                                                               | License                          |
|-----------------------------------------------------------------------|----------------------------------|
| [luxrck/rgssad](https://github.com/luxrck/rgssad)                     | [MIT](licenses/rgssad.txt)       |
| [birkenfeld/serde-pickle](https://github.com/birkenfeld/serde-pickle) | [MIT](licenses/serde-pickle.txt) |
| [Shizmob/rpatool](https://github.com/Shizmob/rpatool)                 | [WTFPL](licenses/rpatool.txt)    |
| [zanzapak](https://aluigi.altervista.org/papers.htm#others-file)      | [GPL-3.0](licenses/zanzapak.txt) |

## Building from Source

### Prerequisites

- **Visual Studio 2017** compiler
- **CLion** and/or **CMake** (version 3.31+)
- **vcpkg**

You can open the included CLion project directly and build through the IDE or use CMake manually to generate the build
files, then compile using the Visual Studio compiler.

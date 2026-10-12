# unpackrat

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/refaim/ObserverModules/actions/workflows/main.yml/badge.svg)](https://github.com/refaim/ObserverModules/actions/workflows/main.yml)

unpackrat brings support for exotic game archives to FAR Manager. It ships as a set of Observer plugin
modules, one per archive format, that present an archive as an ordinary folder you can browse and
extract from. Each module is a single self-contained DLL with its dependencies built in and no
external runtime DLLs to install.

## Download

Install [FAR Manager](https://farmanager.com/download.php?l=en) and the
[Observer](https://github.com/lazyhamster/Observer/releases) plugin host first; unpackrat adds archive
support on top of them. Take the module ZIP whose architecture matches your FAR Manager and Observer
build (x86 or x64), not the architecture of your operating system. Each module owns its own
version and its own release listing; the versions below are what the current sources build, and each
listing shows the versions that are actually published.

| Module | Version | Formats | Release | ChangeLog |
|--------|---------|---------|---------|-----------|
| Ren'Py | 3.1.0 | `.rpa` (RPA-2.0, RPA-3.0) | [renpy releases](https://github.com/refaim/ObserverModules/releases?q=renpy%2Fv) | [RU](src/modules/renpy/ChangeLog.ru.txt) [EN](src/modules/renpy/ChangeLog.en.txt) |
| RPG Maker | 1.1.0 | `.rgss3a` (VX Ace, RGSS3) | [rpgmaker releases](https://github.com/refaim/ObserverModules/releases?q=rpgmaker%2Fv) | [RU](src/modules/rpgmaker/ChangeLog.ru.txt) [EN](src/modules/rpgmaker/ChangeLog.en.txt) |
| Zanzarah | 2.1.0 | `.pak` (original, Steam) | [zanzarah releases](https://github.com/refaim/ObserverModules/releases?q=zanzarah%2Fv) | [RU](src/modules/zanzarah/ChangeLog.ru.txt) [EN](src/modules/zanzarah/ChangeLog.en.txt) |

Each module ZIP carries the module DLL for one architecture, its `observer_user.ini`, both
human-written change logs and a generated `LICENSES.txt` at the archive root.

## Install

Unpack the ZIP for the architecture that matches your FAR Manager and Observer build into
`%FARHOME%\Plugins\Observer\modules\`, where `%FARHOME%` is your FAR Manager installation folder. If
you already have modules there, merge the archive's `observer_user.ini` with the existing file so
every module stays registered, then restart FAR Manager. After that, open a supported archive in FAR
and press Enter or Ctrl+PgDn: Observer shows its contents as a regular folder you can browse and
extract from with the usual FAR commands.

## License

unpackrat's own source is released under the [MIT License](LICENSE.txt).

One upstream file is an explicit exception: the Observer API header [`src/api.h`](src/api.h) stays
under LGPL-3.0-only. Its notice is reproduced as [`licenses/Observer.txt`](licenses/Observer.txt), and
because LGPLv3 incorporates the GNU GPLv3, the corresponding full GPLv3 text travels beside it as
[`licenses/GPL-3.0.txt`](licenses/GPL-3.0.txt). Those two texts are always retained in every packaged
module.

### Third-party components

| Project | License |
|---------|---------|
| [nlohmann/json](https://github.com/nlohmann/json) | [MIT](licenses/json.txt) |
| [catchorg/Catch2](https://github.com/catchorg/Catch2) | [BSL-1.0](licenses/Catch2.txt) |
| [lazyhamster/Observer](https://github.com/lazyhamster/Observer) | [LGPL-3.0](licenses/Observer.txt) plus [GPL-3.0](licenses/GPL-3.0.txt) |
| [Cyan4973/xxHash](https://github.com/Cyan4973/xxHash) | [BSD-2-Clause](licenses/xxHash.txt) |
| [zlib](https://zlib.net) | [zlib](licenses/zlib.txt) |

### Sources of inspiration

| Project | License |
|---------|---------|
| [luxrck/rgssad](https://github.com/luxrck/rgssad) | [MIT](licenses/rgssad.txt) |
| [birkenfeld/serde-pickle](https://github.com/birkenfeld/serde-pickle) | [MIT](licenses/serde-pickle.txt) |
| [Shizmob/rpatool](https://github.com/Shizmob/rpatool) | [WTFPL](licenses/rpatool.txt) |
| [pg83/ix](https://github.com/pg83/ix/tree/66726a904152246fbef8b27e26e878840f6d7fb7) | [MIT](licenses/IX.txt) |

### Format references

The project below describes or decodes a file format. It is a format reference that informed an
independent implementation, not a shipped-code dependency: no code from it is compiled into any
unpackrat module, so its license does not apply to this project and is not reproduced in the packaged
`LICENSES.txt` as a shipped notice.

| Project | License |
|---------|---------|
| [zanzapak](https://aluigi.altervista.org/papers.htm#others-file) | [GPL-2.0-or-later](licenses/zanzapak.txt) |

## Building

The build produces self-contained `/MT` MSVC modules for x86 and x64 with no external runtime
DLLs to ship. The full command contract, prerequisites and quality gates live in
[`docs/build-system.md`](docs/build-system.md); the common entry points are:

```powershell
.\build.ps1 doctor
.\build.ps1 build -Arch all -Config Release
.\build.ps1 test -Arch x86,x64 -Config Debug
```

Each module also publishes an optional `-pdb.zip` symbol bundle beside its module archives.

## Links

[Support Forum (in Russian)](https://forum.farmanager.com/viewtopic.php?t=12729)

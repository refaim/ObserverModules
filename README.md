# unpackrat

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/refaim/unpackrat/actions/workflows/main.yml/badge.svg)](https://github.com/refaim/unpackrat/actions/workflows/main.yml)

unpackrat brings support for exotic archives to FAR Manager. It ships as a set of Observer plugin
modules, one per archive format, that present an archive as an ordinary folder you can browse and
extract from. Each module is a single self-contained DLL with its dependencies built in and no
external runtime DLLs to install.

## Download

Install [FAR Manager](https://farmanager.com/download.php?l=en) and the
[Observer](https://github.com/lazyhamster/Observer/releases) plugin host first; unpackrat adds archive
support on top of them. Take the module ZIP whose architecture matches your FAR Manager and Observer
build (x86 or x64), not the architecture of your operating system. Choose the newest published release
from the module's release listing below.

| Module | Formats | Release | ChangeLog |
|--------|---------|---------|-----------|
| Ren'Py | `.rpa` (RPA-2.0, RPA-3.0) | [renpy releases](https://github.com/refaim/unpackrat/releases?q=renpy) | [RU](src/modules/renpy/ChangeLog.ru.txt) [EN](src/modules/renpy/ChangeLog.en.txt) |
| RPG Maker | `.rgssad` (XP, RGSS1), `.rgss2a` (VX, RGSS2), `.rgss3a` (VX Ace, RGSS3) | [rpgmaker releases](https://github.com/refaim/unpackrat/releases?q=rpgmaker) | [RU](src/modules/rpgmaker/ChangeLog.ru.txt) [EN](src/modules/rpgmaker/ChangeLog.en.txt) |
| Zanzarah | `.pak` (original, Steam) | [zanzarah releases](https://github.com/refaim/unpackrat/releases?q=zanzarah) | [RU](src/modules/zanzarah/ChangeLog.ru.txt) [EN](src/modules/zanzarah/ChangeLog.en.txt) |
| Godot | `.pck`, embedded PCK and same-stem EXE sidecars | [godot releases](https://github.com/refaim/unpackrat/releases?q=godot) | [RU](src/modules/godot/ChangeLog.ru.txt) [EN](src/modules/godot/ChangeLog.en.txt) |

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

## Building

The build produces self-contained `/MT` MSVC modules for x86 and x64 with no external runtime
DLLs to ship. The full command contract, prerequisites and quality gates live in
[`docs/build-system.md`](docs/build-system.md); the common entry points are:

```powershell
.\build.ps1 build -Arch x86,x64 -Config Release
.\build.ps1 test -Arch x86,x64 -Config Debug
.\build.ps1 ci-source
.\build.ps1 ci-arch -Arch x64
.\build.ps1 package
```

Each module also publishes an optional `-pdb.zip` symbol bundle beside its module archives.

## Links

[Support Forum (in Russian)](https://forum.farmanager.com/viewtopic.php?t=12729)

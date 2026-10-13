# unpackrat

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/refaim/unpackrat/actions/workflows/main.yml/badge.svg)](https://github.com/refaim/unpackrat/actions/workflows/main.yml)

unpackrat brings support for exotic game archives to FAR Manager. It ships as a set of Observer plugin
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

Godot restores imported assets to their original folders. In `[Godot]`, `DecodeResources=1`
shows a converted file in place of its compiled original, such as `art/hero.png` in place of
`art/hero.ctex`; unsupported resources keep their originals. The shipped INI enables this mode.
`DecodeResources=0`, or a missing setting, shows originals and skips resource conversion. Import
metadata remains visible in both modes; renamed paths are recorded in `name-map.tsv`.

Media decoding and encoding run during extraction. Converted entries display the packed source
size; converted extraction progress uses that same packed size. Texture containers are recognised by
content and can hold PNG, WebP or raw/GPU image data. FontFile payloads retain their original font
format. Conversion has no aggregate work quota or default image pixel/byte ceiling.

Encrypted packs use keys recovered by static inspection of neighbouring EXEs, with complete
plaintext MD5 verification. Games are never launched. If recovery cannot find a valid key, enter
its 64 hexadecimal digits in Observer's password dialog. An EXE without an embedded pack can open
its same-stem sibling `.pck`. Encrypted resources are decrypted in either presentation mode.

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

| Project | License | Modules |
|---------|---------|---------|
| [nlohmann/json](https://github.com/nlohmann/json) | [MIT](licenses/json.txt) | Tests only |
| [catchorg/Catch2](https://github.com/catchorg/Catch2) | [BSL-1.0](licenses/Catch2.txt) | Tests only |
| [lazyhamster/Observer](https://github.com/lazyhamster/Observer) | [LGPL-3.0](licenses/Observer.txt) plus [GPL-3.0](licenses/GPL-3.0.txt) | Ren'Py, RPG Maker, Zanzarah, Godot |
| [Cyan4973/xxHash](https://github.com/Cyan4973/xxHash) | [BSD-2-Clause](licenses/xxHash.txt) | Tests only |
| [zlib-ng](https://github.com/zlib-ng/zlib-ng) | [Zlib](licenses/zlib-ng.txt) | Ren'Py, Godot |
| [godotengine/godot](https://github.com/godotengine/godot) | [MIT](licenses/godot.txt) | Godot |
| [iOrange/bcdec](https://github.com/iOrange/bcdec) | [MIT](licenses/bcdec.txt) | Godot |
| [mackron/dr_libs](https://github.com/mackron/dr_libs) | [Unlicense OR MIT-0](licenses/dr_libs.txt) | Godot |
| [phoboslab/qoa](https://github.com/phoboslab/qoa) | [MIT](licenses/qoa.txt) | Godot |
| [randy408/libspng](https://github.com/randy408/libspng) | [BSD-2-Clause](licenses/libspng.txt) | Godot |
| [xiph/ogg](https://github.com/xiph/ogg) | [BSD-3-Clause](licenses/libogg.txt) | Godot |
| [ariya/FastLZ](https://github.com/ariya/FastLZ) | [MIT](licenses/fastlz.txt) | Godot |
| [facebook/zstd](https://github.com/facebook/zstd) | [BSD-3-Clause](licenses/zstd.txt) | Godot |
| [google/brotli](https://github.com/google/brotli) | [MIT](licenses/brotli.txt) | Godot |
| [Zydis](https://github.com/zyantific/zydis) | [MIT](licenses/zydis.txt) | Godot |
| [Zycore](https://github.com/zyantific/zycore-c) | [MIT](licenses/zycore.txt) | Godot |
| [Mbed TLS](https://github.com/Mbed-TLS/mbedtls) (mbedcrypto only) | [Apache-2.0](licenses/mbedcrypto.txt) | Godot |

The bcdec, dr_libs and QOA decoders are restored through [local vcpkg overlay ports](build/vcpkg/ports/README.md)
as static libraries. Their source revisions and downloaded header hashes are pinned there. QOA uses the header
from Godot 4.5-stable, which is the version tested with this module.

### Sources of inspiration

| Project | License | Modules |
|---------|---------|---------|
| [luxrck/rgssad](https://github.com/luxrck/rgssad) | [MIT](licenses/rgssad.txt) | RPG Maker |
| [birkenfeld/serde-pickle](https://github.com/birkenfeld/serde-pickle) | [MIT](licenses/serde-pickle.txt) | Ren'Py |
| [Shizmob/rpatool](https://github.com/Shizmob/rpatool) | [WTFPL](licenses/rpatool.txt) | Ren'Py |
| [pg83/ix](https://github.com/pg83/ix/tree/66726a904152246fbef8b27e26e878840f6d7fb7) | [MIT](licenses/IX.txt) | Historical build tooling |

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

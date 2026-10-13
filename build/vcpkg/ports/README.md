# Godot decoder overlays

These local ports build the exact single-header decoders that were tested for Godot 1.0.0. Each port
downloads only its pinned header files, checks their SHA-512 hashes, builds a static library for the
selected vcpkg triplet and installs a copyright notice identical to the one packaged with Godot.
The header SHA-256 values below are the fingerprints of the former `third_party` copies; changing a
revision requires a deliberate compatibility review and new codec tests.

| Port | Source revision | Installed header SHA-256 |
|------|-----------------|-------------------------|
| bcdec 0.985 | [iOrange/bcdec `80859ed3b7afb1c527a2a99d70c61457bea72d0c`](https://github.com/iOrange/bcdec/tree/80859ed3b7afb1c527a2a99d70c61457bea72d0c) | `bcdec.h` `134520764d96f70a27db814173616c89ad85db95fbd3328417c7a8c77e7ca189` |
| drlibs (`dr_mp3` 0.7.4, `dr_wav` 0.14.6) | [mackron/dr_libs `dfe8377631000664666519fdb83da193fd8037f4`](https://github.com/mackron/dr_libs/tree/dfe8377631000664666519fdb83da193fd8037f4) | `dr_mp3.h` `997b7ee18de6e6b81e2a83f1ea9fc62aef25c62b28d48db95635f49e65de0a2f`; `dr_wav.h` `03e70c1a2d9787cd7ed3e966c075bea7bac6373f759db9cd7ca9ccdfc4ec4493` |
| qoa | [Godot 4.5-stable `876b290332ec6f2e6d173d08162a02aa7e6ca46d`](https://github.com/godotengine/godot/tree/876b290332ec6f2e6d173d08162a02aa7e6ca46d) `thirdparty/misc/qoa.h` | `qoa.h` `ab501e91f4afd18c811e689713e5fd3956030562bf80cda784d304af17133f01` |

The QOA port deliberately uses Godot's fork rather than the current phoboslab header. The `drlibs`
port overrides the built-in vcpkg port because this project tested a later exact revision. The Godot
package's notices remain in `licenses/bcdec.txt`, `licenses/dr_libs.txt` and `licenses/qoa.txt`.

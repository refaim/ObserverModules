# Synthetic PCK v3 removal records

These two 248-byte fixtures contain only generated names and bytes. An external
Godot 4.6.2 corpus pack had 2,837 directory records: 2,835 ordinary entries and two
zero-length records with file flag `0x2`. The current reader rejects the whole pack
at the first such record. No external game path or payload was copied here.

Godot treats `PACK_FILE_REMOVAL` as a deletion instruction rather than a stored
file: [canonical pack reader](https://github.com/godotengine/godot/blob/001aa128b1cd80dc4e47e823c360bccf45ed6bad/core/io/file_access_pack.cpp).

Both fixtures use PCK v3, engine 4.6.2, relative-base flag `0x2`, file base 112 and
directory offset 128. They contain `keep.txt` with the bytes `fixture\n` and an
empty `removed.txt` record. Only the final four-byte entry-flags field differs:
`removal-v3.pck` uses `0x2`; `removal-v3-control.pck` uses `0x0`.

Current x64 Release ABI behavior: both return `OpenStorage=1`; the removal fixture
returns `PrepareFiles=0`, while the control returns `PrepareFiles=1` with two rows.
Future support should preserve extraction of ordinary files and recognize a
tombstone as a deletion record. It must not extract a tombstone as an ordinary
empty file or silently implement merging with another pack. The presentation of
deletion metadata remains a design decision.

`removal-v3-manifest.json` records every input field and both SHA-256 hashes. This
Python standard-library recipe regenerates the exact files from that manifest:

```python
import hashlib, json, struct
from pathlib import Path

folder = Path(__file__).parent  # Save this recipe next to the manifest.
m = json.loads((folder / "removal-v3-manifest.json").read_text())
for output in m["outputs"]:
    data = bytearray(struct.pack("<6I2Q", 0x43504447, m["format_version"],
        *m["engine_version"], m["pack_flags"], m["file_base"], m["directory_offset"]))
    data.extend(bytes(m["header_bytes"] - len(data)))
    data.extend(m["body_utf8"].encode())
    data.extend(bytes(m["body_padding_bytes"]))
    data.extend(struct.pack("<I", len(m["entries"])))
    for index, entry in enumerate(m["entries"]):
        name = entry["path"].encode()
        name += bytes((-len(name)) % 4)
        data.extend(struct.pack("<I", len(name)))
        data.extend(name)
        data.extend(struct.pack("<2Q", entry["stored_offset"], entry["logical_size"]))
        data.extend(bytes.fromhex(entry["md5"]))
        data.extend(struct.pack("<I", output["second_entry_flags"] if index else 0))
    assert hashlib.sha256(data).hexdigest() == output["sha256"]
    (folder / output["file"]).write_bytes(data)
```


## Converted resource size regression

`converted-size.pck` is a 479-byte synthetic PCK v2 (Godot 4.2.1). It contains
97 bytes of generated import metadata and a 126-byte GST2 texture carrying a
complete 70-byte PNG. The PNG independently decodes to one red RGBA pixel
`(255, 0, 0, 255)`. No game bytes, paths, images, or audio were copied.

With `DecodeResources=1`, the module exposes `art/pixel.png` without the compiled
sibling. The panel Size, PackedSize and successful extraction progress all report
126 stored bytes. The extracted PNG is still exactly 70 bytes. This is the
explicit packed-size presentation policy: listing inspects metadata only and
never encodes the image just to count its output. `DecodeResources=0` exposes
the original texture and preserves it byte for byte.

`converted-size-manifest.json` records source ranges, hashes, the complete PNG,
packed presentation sizes and the independently checked output length. Historical
observations in the manifest predate the current packed-size contract. The
following standard-library recipe regenerates the exact fixture from it:

```python
import hashlib, json, struct
from pathlib import Path

folder = Path(__file__).parent  # Save this recipe next to the manifest.
m = json.loads((folder / "converted-size-manifest.json").read_text())
png = bytes.fromhex(m["png_hex"])
texture = (b"GST2" + struct.pack("<8I", 1, 1, 1, 0, 0, 0, 0, 0)
           + struct.pack("<I2H3I", 1, 1, 1, 0, 5, len(png)) + png)
bodies = [m["metadata_utf8"].encode(), texture]
data = bytearray(struct.pack("<6IQ", 0x43504447, m["format_version"],
    *m["engine_version"], m["pack_flags"], m["file_base"]))
data.extend(bytes(64))
data.extend(struct.pack("<I", len(m["entries"])))
for entry, body in zip(m["entries"], bodies):
    name = entry["path"].encode()
    name += bytes((-len(name)) % 4)
    data.extend(struct.pack("<I", len(name)))
    data.extend(name)
    data.extend(struct.pack("<2Q", entry["stored_offset"], len(body)))
    data.extend(hashlib.md5(body).digest())
    data.extend(struct.pack("<I", entry["flags"]))
data.extend(bytes(m["file_base"] - len(data)))
for body in bodies:
    data.extend(body)
assert len(data) == m["file_size"]
assert hashlib.sha256(data).hexdigest() == m["sha256"]
(folder / m["fixture"]).write_bytes(data)
```

## Minimal Sample and FontFile media

`audio-font-minimal.pck` is a 783-byte synthetic PCK with two resources. The
Godot 3.0 binary-format-2 `Sample` uses four zero IMA seed bytes before two
packed data bytes and declares four mono frames. Extraction must produce a
52-byte PCM16 WAVE. The Godot 4 `FontFile` embeds a 16-byte `OTTO` signature
stub; extraction must preserve those 16 bytes as `.otf`. The stub is deliberately
not a renderable font, so this fixture tests the resource wrapper and byte
preservation. Independently checked real TTF files in the optional external
corpus cover renderable font bytes without copying any game assets here.

`audio-font-minimal-manifest.json` records exact source hashes, paths and output
sizes. Run `generate-audio-font-minimal.py` beside the fixture to regenerate
the PCK and manifest using only the Python standard library.

To reproduce `converted-size.pck` through the Observer API, enable
`DecodeResources=1`, call `OpenStorage` and `PrepareFiles`, select `art/pixel.png`
through `GetItem`, and extract it. Assert `Size=PackedSize=126`, successful
progress totals 126, and the destination equals the generated 70-byte PNG.
The native integration and ABI tests enforce this presentation contract.


## Probe budget starvation

`probe-budget-starvation.pck` is a 583-byte, repository-generated PCK v2. Its
SHA-256 is `572da6569ec456999efdc3310f73179a6a208f0e8c163d34633256575a663269`.
It isolates the missing-conversion bug seen when earlier expensive resources
exhausted one listing allowance and prevented later PNG/WebP texture headers
from being inspected.

The first entry, `budget/large.res`, is a 40-byte RSCC envelope. Its header and
five-entry block table are structurally valid and declare 67,108,865 logical
bytes (64 MiB + 1). The compressed blocks deliberately have zero stored bytes,
so this is an invalid compressed resource. Listing must reject its malformed
block stream without materializing the declared logical payload, while retaining
the 40 stored bytes as an ordinary raw fallback.

The next entry is a valid 126-byte GST2 texture containing the same independently
decodable 70-byte red-pixel PNG as `converted-size.pck`. A following 97-byte,
terminal-NUL import fragment restores the path `art/pixel.png`. The recorded
pre-fix Release DLL (`01ad386f0485b1c396dd142800c2fcf4b79daf0ecbf618d15f795cedd3710b45`)
opens and prepares this pack but only exposes `art/pixel.ctex`: the valid PNG
view disappears after the RSCC exhausts the shared allowance. The raw trigger
and import metadata still extract byte for byte, so the failure is isolated to
resource probing.

The current module has no aggregate conversion-work allowance. Metadata probing
of one malformed resource cannot prevent an unrelated texture from appearing.
With `DecodeResources=1`, `art/pixel.png` remains visible and extracts all 70 PNG
bytes, with `Size=PackedSize=126` and 126 successful progress bytes. The compiled
sibling is absent; the raw trigger and import metadata remain byte-exact. The
native integration regression is tagged `[probe-budget-fixture]`.

`probe-budget-starvation-manifest.json` records every source window, body hash,
and expected output. This standard-library recipe regenerates the exact bytes:

```python
import hashlib, json, struct
from pathlib import Path

folder = Path(__file__).parent  # Save this recipe next to the manifest.
m = json.loads((folder / "probe-budget-starvation-manifest.json").read_text())
png = bytes.fromhex(m["png_hex"])
trigger = (b"RSCC" + struct.pack("<3I", m["rscc_mode"], m["rscc_block_bytes"],
    m["rscc_declared_logical_bytes"])
    + struct.pack("<" + "I" * m["rscc_block_count"], *m["rscc_compressed_block_sizes"])
    + b"RSCC")
texture = (b"GST2" + struct.pack("<8I", 1, 1, 1, 0, 0, 0, 0, 0)
           + struct.pack("<I2H3I", 1, 1, 1, 0, 5, len(png)) + png)
bodies = [trigger, texture, m["metadata_utf8"].encode()]
data = bytearray(struct.pack("<6IQ", 0x43504447, m["format_version"],
    *m["engine_version"], m["pack_flags"], m["file_base"]))
data.extend(bytes(64))
data.extend(struct.pack("<I", len(m["entries"])))
for entry, body in zip(m["entries"], bodies):
    name = entry["path"].encode()
    name += bytes((-len(name)) % 4)
    data.extend(struct.pack("<I", len(name)))
    data.extend(name)
    data.extend(struct.pack("<2Q", entry["stored_offset"], len(body)))
    data.extend(hashlib.md5(body).digest())
    data.extend(struct.pack("<I", entry["flags"]))
data.extend(bytes(m["file_base"] - len(data)))
for body in bodies:
    data.extend(body)
assert len(data) == m["file_size"]
assert hashlib.sha256(data).hexdigest() == m["sha256"]
(folder / m["fixture"]).write_bytes(data)
```

## Omitted audio defaults

`audio-defaults.pck` is a 1,196-byte synthetic PCK v2 containing three Godot 4
`AudioStreamWAV` resources with locally constructed, one-frame QOA payloads:
mono at 44100 Hz omits `mix_rate` and `stereo`, mono at 22050 Hz omits `stereo`,
and stereo at 44100 Hz omits `mix_rate`. These descriptor shapes reproduce the
serialization omissions observed in the encrypted corpus without copying game
bytes, paths or keys. Every decoded channel contains exactly one PCM16 sample +1.

The canonical Godot defaults are `format=0`, `mix_rate=44100`, `stereo=false`:
[Godot 4.5 AudioStreamWAV](https://github.com/godotengine/godot/blob/876b290332ec6f2e6d173d08162a02aa7e6ca46d/scene/resources/audio_stream_wav.h#L118).
The [binary saver](https://github.com/godotengine/godot/blob/876b290332ec6f2e6d173d08162a02aa7e6ca46d/core/io/resource_format_binary.cpp#L2289)
omits values equal to their class defaults. Godot 3 `AudioStreamSample` uses the
same defaults, confirmed in its pinned 3.6 constructor. Explicit wrong types,
duplicate properties and QOA header/descriptor mismatches remain invalid.

`audio-defaults-manifest.json` records every body hash and expected WAV geometry.
The actual-DLL regression is tagged `[audio-defaults-fixture]`; unit tests tagged
`[audio-defaults]` also cover every omission combination for PCM8. This recipe,
saved next to the manifest, reproduces the exact fixture with the standard library:

```python
import hashlib, json, struct
from pathlib import Path
folder = Path(__file__).parent
m = json.loads((folder / "audio-defaults-manifest.json").read_text())
def text(value):
    value = value.encode() + b"\0"
    return struct.pack("<I", len(value)) + value
def resource(entry):
    payload = bytes.fromhex(entry["qoa_hex"])
    props = [(0, struct.pack("<II", 31, len(payload)) + payload + bytes(-len(payload) % 4)),
             (1, struct.pack("<Ii", 3, 3))]
    if entry["sample_rate"] != 44100:
        props.append((2, struct.pack("<Ii", 3, entry["sample_rate"])))
    if entry["channels"] == 2:
        props.append((3, struct.pack("<II", 2, 1)))
    data = bytearray(b"RSRC" + struct.pack("<5I", 0, 0, 4, 5, 6) + text("AudioStreamWAV"))
    data.extend(struct.pack("<QIQ", 0, 0, 0) + bytes(44) + struct.pack("<I", 4))
    for name in ("data", "format", "mix_rate", "stereo"):
        data.extend(text(name))
    data.extend(struct.pack("<II", 0, 1) + text("local://1"))
    position = len(data)
    data.extend(bytes(8))
    struct.pack_into("<Q", data, position, len(data))
    data.extend(text("AudioStreamWAV") + struct.pack("<I", len(props)))
    for index, value in props:
        data.extend(struct.pack("<I", index) + value)
    data.extend(b"RSRC")
    assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    return data
bodies = [resource(entry) for entry in m["entries"]]
data = bytearray(struct.pack("<6IQ", 0x43504447, 2, 4, 5, 0, 0, m["file_base"]) + bytes(64))
data.extend(struct.pack("<I", len(bodies)))
offset = 0
for entry, body in zip(m["entries"], bodies):
    name = entry["path"].encode()
    name += bytes(-len(name) % 4)
    data.extend(struct.pack("<I", len(name)) + name + struct.pack("<QQ", offset, len(body)))
    data.extend(hashlib.md5(body).digest() + bytes(4))
    offset += len(body)
data.extend(bytes(m["file_base"] - len(data)))
for body in bodies:
    data.extend(body)
assert len(data) == m["file_size"]
assert hashlib.sha256(data).hexdigest() == m["sha256"]
(folder / m["fixture"]).write_bytes(data)
```

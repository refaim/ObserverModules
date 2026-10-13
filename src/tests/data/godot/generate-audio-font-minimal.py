import hashlib
import json
import struct
from pathlib import Path


def u32(value):
    return struct.pack("<I", value)


def u64(value):
    return struct.pack("<Q", value)


def text(value):
    encoded = value.encode("utf-8") + b"\0"
    return u32(len(encoded)) + encoded


def value_string(value):
    return u32(5) + text(value)


def value_int(value):
    return u32(3) + struct.pack("<i", value)


def value_bool(value):
    return u32(2) + u32(int(value))


def value_bytes(value):
    return u32(31) + u32(len(value)) + value + bytes(-len(value) % 4)


def value_dict(entries):
    return u32(26) + u32(len(entries)) + b"".join(
        value_string(key) + val for key, val in entries
    )


def resource(main_type, major, minor, version, names, properties, main_path="local://1"):
    modern = major >= 4
    body = bytearray(b"RSRC" + struct.pack("<5I", 0, 0, major, minor, version))
    body += text(main_type) + u64(0)
    body += u32(0) + u64(0) + bytes(44) if modern else bytes(56)
    body += u32(len(names))
    for name in names:
        body += text(name)
    body += u32(0) + u32(1) + text(main_path)
    position = len(body)
    body += bytes(8)
    struct.pack_into("<Q", body, position, len(body))
    body += text(main_type) + u32(len(properties))
    for name, encoded in properties:
        body += u32(names.index(name)) + encoded
    body += b"RSRC"
    return bytes(body)


sample_payload = bytes(6)  # 4 zero seed bytes, then 2 IMA bytes = 4 PCM frames.
sample_data = value_dict([
    ("packing", value_string("raw")),
    ("format", value_string("ima_adpcm")),
    ("stereo", value_bool(False)),
    ("length", value_int(4)),
    ("data", value_bytes(sample_payload)),
])
sample = resource("Sample", 3, 0, 2, ["mix_rate", "data"], [
    ("mix_rate", value_int(8000)),
    ("data", sample_data),
])

# This is an intentionally non-renderable OpenType signature stub. It exercises
# the FontFile wrapper and exact byte preservation; real valid fonts are proven
# separately against the external corpus and are never copied into this fixture.
font_payload = b"OTTO" + bytes(12)
font = resource("FontFile", 4, 5, 6, ["data"], [("data", value_bytes(font_payload))])

entries = [
    ("res://audio/padded-sample.res", sample, "audio/padded-sample.wav", 52),
    ("res://fonts/generated-font.res", font, "fonts/generated-font.otf", len(font_payload)),
]
file_base = 256
packed = bytearray(struct.pack("<6IQ", 0x43504447, 2, 4, 5, 0, 0, file_base) + bytes(64))
packed += u32(len(entries))
offset = 0
for name, body, _, _ in entries:
    encoded = name.encode("utf-8")
    encoded += bytes(-len(encoded) % 4)
    packed += u32(len(encoded)) + encoded + u64(offset) + u64(len(body))
    packed += hashlib.md5(body).digest() + bytes(4)
    offset += len(body)
assert len(packed) <= file_base
packed += bytes(file_base - len(packed))
for _, body, _, _ in entries:
    packed += body

destination = Path(__file__).parent
fixture = "audio-font-minimal.pck"
(destination / fixture).write_bytes(packed)
manifest = {
    "fixture": fixture,
    "provenance": "All bytes generated locally from the standard-library recipe in this repository; no game data, keys or third-party font bytes.",
    "font_note": "The 16-byte OTTO stub is intentionally not renderable. It reproduces wrapper classification and byte-preserving extraction only; real TTF compatibility is checked separately against the external corpus.",
    "sample_note": "Godot 3.0 binary format 2 class Sample with a Godot 2-style data dictionary; four zero IMA seed bytes precede two nibbled data bytes, yielding exactly four mono PCM frames.",
    "format_version": 2,
    "engine_version": [4, 5, 0],
    "file_base": file_base,
    "file_size": len(packed),
    "sha256": hashlib.sha256(packed).hexdigest(),
    "entries": [
        {
            "path": name,
            "converted_path": converted,
            "logical_size": len(body),
            "md5": hashlib.md5(body).hexdigest(),
            "sha256": hashlib.sha256(body).hexdigest(),
            "expected_output_size": output_size,
            "output_sha256": hashlib.sha256(font_payload).hexdigest() if name.endswith("generated-font.res") else None,
        }
        for name, body, converted, output_size in entries
    ],
}
(destination / "audio-font-minimal-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(manifest["file_size"], manifest["sha256"])

# This second generated pack preserves two quirks found in the external Godot corpus.
# A Godot 3 main directory row may be named local://-1; typed imported MP3 can carry
# two leading bytes before its first MPEG frame. All payload bytes are synthetic.
legacy_sample = resource("Sample", 3, 0, 2, ["mix_rate", "data"], [
    ("mix_rate", value_int(8000)),
    ("data", sample_data),
], main_path="local://-1")
frame = bytes.fromhex("ff fb 90 c0") + bytes(413)
mp3_payload = b"\x89\x06" + frame * 4
prefixed_mp3 = resource("AudioStreamMP3", 4, 5, 6, ["data"], [
    ("data", value_bytes(mp3_payload)),
])
compat_entries = [
    ("res://audio/legacy-negative-main.sample", legacy_sample,
     "audio/legacy-negative-main.wav", 52),
    ("res://audio/prefixed.mp3", prefixed_mp3,
     "audio/prefixed.mp3", len(mp3_payload)),
]
compat = bytearray(struct.pack("<6IQ", 0x43504447, 2, 4, 5, 0, 0, file_base) + bytes(64))
compat += u32(len(compat_entries))
offset = 0
for name, body, _, _ in compat_entries:
    encoded = name.encode("utf-8")
    encoded += bytes(-len(encoded) % 4)
    compat += u32(len(encoded)) + encoded + u64(offset) + u64(len(body))
    compat += hashlib.md5(body).digest() + bytes(4)
    offset += len(body)
assert len(compat) <= file_base
compat += bytes(file_base - len(compat))
for _, body, _, _ in compat_entries:
    compat += body
compat_fixture = "media-compat.pck"
(destination / compat_fixture).write_bytes(compat)
compat_manifest = {
    "fixture": compat_fixture,
    "provenance": "Generated locally by this Python standard-library recipe; no game or third-party media bytes.",
    "sample_note": "Godot 3 Sample main resource path local://-1 and four-frame mono IMA WAV conversion.",
    "mp3_note": "Two leading bytes followed by four generated 417-byte MPEG-1 Layer III frames; extracted MP3 retains both bytes and decodes with ffmpeg.",
    "format_version": 2,
    "engine_version": [4, 5, 0],
    "file_base": file_base,
    "file_size": len(compat),
    "sha256": hashlib.sha256(compat).hexdigest(),
    "entries": [
        {
            "path": name,
            "converted_path": converted,
            "logical_size": len(body),
            "md5": hashlib.md5(body).hexdigest(),
            "sha256": hashlib.sha256(body).hexdigest(),
            "expected_output_size": output_size,
            "output_sha256": hashlib.sha256(mp3_payload).hexdigest()
            if name.endswith("prefixed.mp3") else None,
        }
        for name, body, converted, output_size in compat_entries
    ],
}
(destination / "media-compat-manifest.json").write_text(
    json.dumps(compat_manifest, indent=2) + "\n"
)
print(compat_manifest["file_size"], compat_manifest["sha256"])
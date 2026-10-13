"""The Godot single-header dependencies resolve through pinned vcpkg overlays."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
PORTS = REPOSITORY / "build" / "vcpkg" / "ports"

# These are the exact headers exercised before the move out of third_party.
HEADERS = {
    "bcdec": {
        "version": "0.985",
        "commit": "80859ed3b7afb1c527a2a99d70c61457bea72d0c",
        "files": {
            "bcdec.h": "17913d49e64755ba3b0a845352e0f36bcda6e80e653ab70c3c27ae7a3510d6a6f621a68ce374075fe67d1487d211f5f9e322e8f81817b4480f3f2eef5b4e5390",
        },
    },
    "drlibs": {
        "version": "2026-08-31",
        "commit": "dfe8377631000664666519fdb83da193fd8037f4",
        "files": {
            "dr_mp3.h": "c21d523bdd890bf8b2e84ff7ac63f72299b235834b424d95d281b57c4e22d260e23e423ed8d829e710cbd11d8814c69afef24cb9e576bbb9cb0b704be94bf8ed",
            "dr_wav.h": "277ada6b7cf940f557d38dea7985db129c8aaf034c23425da3f1681e901439bbd4152a766de5a26cc7ddb3f0a00f2ce8f6496f61c359bc65889eeac25e356bd3",
        },
    },
    "qoa": {
        "version": "4.5-stable",
        "commit": "876b290332ec6f2e6d173d08162a02aa7e6ca46d",
        "files": {
            "qoa.h": "4fc49c5fa4b9af72d4e1c33c9202e227b5cbb5024ec26cb5d6a11b295af3dfdeeeb0e7fa8f4d06fecafbd7e568b21d04a1ef8edf572f3df244bb49273a692b17",
        },
    },
}

CONSUMERS = {
    "src/core/codecs/bc_codec.cpp": "bcdec.h",
    "src/core/codecs/mp3_codec.cpp": "dr_mp3.h",
    "src/core/codecs/wav_codec.cpp": "dr_wav.h",
    "src/core/codecs/qoa_codec.cpp": "qoa.h",
    "src/tests/unit/audio_io.cpp": "dr_mp3.h",
    "src/tests/unit/godot_audio_resource.cpp": "qoa.h",
    "src/tests/unit/sample_codecs.cpp": "qoa.h",
    "src/tests/support/sample_fixtures.cpp": "qoa.h",
}


class GodotOverlayPortTests(unittest.TestCase):
    def test_manifest_resolves_the_three_static_decoder_ports(self) -> None:
        manifest = json.loads((REPOSITORY / "vcpkg.json").read_text(encoding="utf-8"))
        dependencies = {entry["name"] for entry in manifest["dependencies"]}
        self.assertTrue(HEADERS.keys() <= dependencies)

        configuration = json.loads((REPOSITORY / "vcpkg-configuration.json").read_text(encoding="utf-8"))
        self.assertIn("build/vcpkg/ports", configuration["overlay-ports"])

    def test_overlay_recipes_lock_the_tested_header_bytes(self) -> None:
        for port, expected in HEADERS.items():
            with self.subTest(port=port):
                descriptor = json.loads((PORTS / port / "vcpkg.json").read_text(encoding="utf-8"))
                self.assertEqual(descriptor["name"], port)
                self.assertEqual(descriptor["version-string"], expected["version"])
                recipe = (PORTS / port / "portfile.cmake").read_text(encoding="utf-8")
                self.assertIn(expected["commit"], recipe)
                self.assertNotIn("HEAD_REF", recipe)
                for header, digest in expected["files"].items():
                    with self.subTest(header=header):
                        self.assertIn(header, recipe)
                        self.assertIn(digest, recipe)

    def test_ports_carry_the_same_notices_as_godot_packages(self) -> None:
        notices = {"bcdec": "bcdec.txt", "drlibs": "dr_libs.txt", "qoa": "qoa.txt"}
        for port, notice in notices.items():
            with self.subTest(port=port):
                self.assertEqual(
                    (PORTS / port / "copyright").read_bytes(),
                    (REPOSITORY / "licenses" / notice).read_bytes(),
                )

    def test_decoder_implementations_link_from_port_libraries(self) -> None:
        implementation_macros = {
            "src/core/codecs/bc_codec.cpp": "BCDEC_IMPLEMENTATION",
            "src/core/codecs/mp3_codec.cpp": "DR_MP3_IMPLEMENTATION",
            "src/core/codecs/wav_codec.cpp": "DR_WAV_IMPLEMENTATION",
        }
        for source, macro in implementation_macros.items():
            with self.subTest(source=source):
                self.assertNotIn(f"#define {macro}", (REPOSITORY / source).read_text(encoding="utf-8"))

        self.assertFalse((REPOSITORY / "src/core/codecs/qoa_reference.c").exists())
        for port in HEADERS:
            with self.subTest(port=port):
                cmake = (PORTS / port / "CMakeLists.txt").read_text(encoding="utf-8")
                self.assertIn("STATIC", cmake)
                self.assertIn("vcpkg_cmake_configure", (PORTS / port / "portfile.cmake").read_text(encoding="utf-8"))
        for project in ("godot", "tests"):
            with self.subTest(project=project):
                text = (REPOSITORY / f"build/projects/{project}.vcxproj").read_text(encoding="utf-8")
                for library in ("bcdec.lib", "drlibs.lib", "qoa.lib"):
                    self.assertIn(library, text)
                self.assertNotIn("qoa_reference.c", text)

    def test_consumers_use_installed_headers_and_vendored_copies_are_absent(self) -> None:
        for source, header in CONSUMERS.items():
            with self.subTest(source=source):
                text = (REPOSITORY / source).read_text(encoding="utf-8")
                self.assertIn(f"#include <{header}>", text)
                self.assertNotIn("third_party/", text)
        for old_directory in ("bcdec", "dr_libs", "qoa"):
            with self.subTest(vendor=old_directory):
                self.assertFalse((REPOSITORY / "third_party" / old_directory).exists())


if __name__ == "__main__":
    unittest.main()

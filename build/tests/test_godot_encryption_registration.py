"""Pinned native dependencies and redistribution contract for encrypted Godot packs."""
import json
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "build"))
import main

NS = {"m": "http://schemas.microsoft.com/developer/msbuild/2003"}

class EncryptionRegistration(unittest.TestCase):
    def test_manifest_pins_decoder_and_crypto_dependencies(self):
        manifest = json.loads((ROOT / "vcpkg.json").read_text(encoding="utf-8"))
        deps = {item["name"]: item.get("version>=") for item in manifest["dependencies"]}
        self.assertEqual(deps.get("zydis"), "4.1.1")
        self.assertEqual(deps.get("mbedtls"), "3.6.5")

    def test_production_and_tests_link_only_crypto_and_static_decoder(self):
        for name in ("godot", "tests"):
            with self.subTest(project=name):
                tree = ET.parse(ROOT / "build/projects" / (name + ".vcxproj"))
                links = ";".join(x.text or "" for x in tree.findall(".//m:Link/m:AdditionalDependencies", NS))
                for library in ("mbedcrypto.lib", "Zydis.lib", "Zycore.lib"):
                    self.assertIn(library, links)
                self.assertNotIn("mbedtls.lib", links)
                self.assertNotIn("mbedx509.lib", links)

    def test_godot_packages_include_dependency_grants(self):
        for name, marker in (("zydis.txt", "MIT License"),
                             ("zycore.txt", "MIT License"),
                             ("mbedcrypto.txt", "Apache License")):
            with self.subTest(notice=name):
                self.assertIn(name, main.LICENSES["godot"])
                self.assertIn(marker, (ROOT / "licenses" / name).read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()

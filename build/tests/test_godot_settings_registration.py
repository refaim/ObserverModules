"""The packaged Godot section opts in to decoded resource views."""

import configparser
from pathlib import Path
import unittest


class GodotSettingsRegistrationTests(unittest.TestCase):
    def test_shipped_godot_section_explicitly_enables_resource_decoding(self) -> None:
        source = Path(__file__).resolve().parents[2] / "src/modules/godot/observer_user.ini"
        config = configparser.ConfigParser(interpolation=None)
        config.read(source, encoding="utf-8")
        self.assertEqual(config.get("Godot", "DecodeResources", fallback=""), "1")


if __name__ == "__main__":
    unittest.main()

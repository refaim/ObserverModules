"""The published component inventory accounts for every retained license notice."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]


class LicenseInventory(unittest.TestCase):
    def test_every_license_notice_is_linked_from_the_component_inventory(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        linked = set(re.findall(r"\(licenses/([^()]+\.txt)\)", readme))
        retained = {path.name for path in (ROOT / "licenses").glob("*.txt")}
        self.assertEqual(retained, linked)

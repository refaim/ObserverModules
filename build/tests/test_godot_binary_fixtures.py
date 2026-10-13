"""Repository-owned PCK fixtures must survive a Git checkout byte for byte."""
from pathlib import Path
import hashlib
import json
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "src/tests/data/godot"


class GodotBinaryFixtureContract(unittest.TestCase):
    def test_git_disables_text_conversion_for_every_pck_fixture(self) -> None:
        paths = sorted(path.relative_to(ROOT).as_posix() for path in FIXTURES.glob("*.pck"))
        self.assertTrue(paths)
        result = subprocess.run(
            ["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "check-attr", "-z", "--stdin", "text"],
            input=("\0".join(paths) + "\0").encode(), capture_output=True, check=True,
        )
        fields = result.stdout.decode().rstrip("\0").split("\0")
        self.assertEqual(len(fields), len(paths) * 3)
        for index in range(0, len(fields), 3):
            with self.subTest(path=fields[index]):
                self.assertEqual(fields[index + 2], "unset")

    def test_every_fixture_matches_its_existing_complete_file_manifest(self) -> None:
        expected = {}
        for manifest in FIXTURES.glob("*-manifest.json"):
            document = json.loads(manifest.read_text(encoding="utf-8"))
            if "fixture" in document:
                expected[document["fixture"]] = (document["file_size"], document["sha256"])
            else:
                for row in document["outputs"]:
                    expected[row["file"]] = (row["bytes"], row["sha256"])
        self.assertEqual(set(expected), {path.name for path in FIXTURES.glob("*.pck")})
        for name, (size, digest) in expected.items():
            with self.subTest(fixture=name):
                data = (FIXTURES / name).read_bytes()
                self.assertEqual(len(data), size)
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest)

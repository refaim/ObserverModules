"""Independent integrity check for the small oversized Ren'Py index fixture."""

from __future__ import annotations

import hashlib
from pathlib import Path
import unittest
import zlib


FIXTURE = Path(__file__).resolve().parents[2] / "src/tests/data/renpy/expanded-index-over-budget.rpa"
EXPANDED_SIZE = 64 * 1024 * 1024 + 1
ARCHIVE_SHA256 = "47638c44d5e160baaff37211fac2661279277295553ad9784e955738761b8200"


class RenpyMetadataFixtureTests(unittest.TestCase):
    def test_oversized_index_archive_is_small_and_expands_to_exactly_the_budget_boundary(self) -> None:
        archive = FIXTURE.read_bytes()
        self.assertEqual(len(archive), 65_271)
        self.assertEqual(hashlib.sha256(archive).hexdigest(), ARCHIVE_SHA256)
        self.assertEqual(archive[:25], b"RPA-2.0 0000000000000020\n")
        self.assertEqual(archive[25:32], bytes(7))

        inflater = zlib.decompressobj()
        expanded_size = 0
        for offset in range(32, len(archive), 4096):
            expanded = inflater.decompress(archive[offset : offset + 4096])
            self.assertEqual(expanded.count(b"N"), len(expanded))
            expanded_size += len(expanded)
        tail = inflater.flush()
        self.assertEqual(tail.count(b"N"), len(tail))
        expanded_size += len(tail)
        self.assertEqual(expanded_size, EXPANDED_SIZE)
        self.assertTrue(inflater.eof)
        self.assertEqual(inflater.unused_data, b"")
        self.assertEqual(inflater.unconsumed_tail, b"")


if __name__ == "__main__":
    unittest.main()

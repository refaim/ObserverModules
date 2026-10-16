"""Regression coverage for the reviewed libspng declared-license expression.

The CI libspng 0.7.4#1 install emits its raw vcpkg SPDX ``licenseConcluded`` as
``BSD-2-Clause AND libpng-2.0`` with no surrounding parentheses.  ``prepare``
resolves a component's declared expression with an exact, deliberate string
comparison against the reviewed inventory, so the inventory has to record the
emission byte for byte; an accidental outer parenthesisation makes the exact
comparison reject a faithful emission as a "changed license".

These tests read the recorded CI emission and the real reviewed inventory
directly, drive the same exact declared-resolution that ``prepare`` performs
through ``_check_components``, and fail while the reviewed data disagrees with
the actual emission.  The exact-comparison behaviour itself is intentionally
left unchanged, and a genuinely changed license is still rejected by the
existing pipeline coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import licenses  # noqa: E402


REPOSITORY = Path(__file__).resolve().parents[2]
FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures" / "ort-samples" / "libspng" / "vcpkg.spdx.json"
)
INVENTORY = REPOSITORY / "build" / "license-inventory.json"
LIBSPNG_EMISSION = "BSD-2-Clause AND libpng-2.0"


class ActualLibspngInventory(unittest.TestCase):
    """The reviewed inventory must record the emission the CI install actually produced."""

    def _sbom(self):
        return licenses.parse_sbom(json.loads(FIXTURE.read_text(encoding="utf-8")))

    def _inventory_component(self) -> dict:
        inventory = licenses.load_inventory(INVENTORY)
        return next(
            component
            for component in inventory["components"]
            if isinstance(component, dict) and component.get("vcpkgName") == "libspng"
        )

    def _reference(self, sbom) -> dict:
        """The declared-resolution view ``prepare`` builds from an exported package."""

        return {
            "name": sbom.port,
            "version": sbom.version,
            "fullVersion": sbom.version,
            "declaredExpression": sbom.expression,
            "resources": [
                {"name": resource.name, "url": resource.url, "sha512": resource.sha512}
                for resource in sbom.resources
            ],
        }

    def test_fixture_records_the_actual_ci_emission_verbatim(self) -> None:
        self.assertEqual(self._sbom().expression, LIBSPNG_EMISSION)

    def test_reviewed_inventory_records_the_actual_emission(self) -> None:
        # The inventory's declaredExpression is what prepare's exact comparison approves, so it
        # must equal the recorded emission rather than a re-parenthesised restatement of it.
        self.assertEqual(
            self._inventory_component()["declaredExpression"], LIBSPNG_EMISSION
        )

    def test_prepare_declared_resolution_preserves_the_actual_emission(self) -> None:
        sbom = self._sbom()
        resolved = licenses._resolve_declared(
            "libspng", self._reference(sbom), self._inventory_component()
        )
        self.assertEqual(resolved, sbom.expression)


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the real vcpkg header-bundle artifacts and their selectors.

The two header-only vcpkg ports ``bcdec`` and ``drlibs`` are shipped as a
deterministic, uncompressed ``.tar`` bundle of the exact review headers.  ``license-prepare``
preserves the raw vcpkg *resource download filenames*, which carry a git-commit
prefix, so the bundle members keep those full names.  An earlier manual audit had
bundled the same headers under shortened names, leaving the ORT package
configurations pointing at members that the real artifact does not contain and the
approved artifact table carrying the wrong address.

These tests pin the actual production artifact addresses and member paths emitted by
a local replay of the real ``build/main.py license-prepare`` over the immutable
exports captured from CI run 38889306396 at commit
e9199ab4132c4218ab76d9fcdb57a95255c7cb99 (that CI run's own license-prepare stopped
at the libspng declared-expression mismatch).  The historical recorded gzip evidence
from that local replay is retained only as a record; because gzip frames are not
byte-stable across platforms, the shipped bundle is now the portable TAR counterpart,
which carries the same uncompressed payload and member set.  These tests exercise the
production bundle-naming mechanism at the external fetch boundary against that
portable TAR form, and do not claim the current TAR bytes were emitted by that old CI
run.
They fail against the shortened audit data and are meant to pass once the selectors
and the artifact table carry the real names.  The production red was proven by the
primary against the real CLI archives; this unit fixture is a narrow selector and
naming pin, not a claim of full end-to-end coverage.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import licenses  # noqa: E402


CONFIGURATIONS = REPOSITORY / ".ort" / "package-configurations"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "license_package_configurations"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _sha512(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def _bundle_members(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        return archive.getnames()


class ActualHeaderBundles(unittest.TestCase):
    """The ORT selectors and the approved artifact table target the real bundles."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.actual = _load("actual_header_bundles.json")
        cls.expected = {
            component["name"]: component
            for component in _load("expected_artifacts.json")["components"]
        }

    def _configuration(self, name: str) -> dict:
        return json.loads((CONFIGURATIONS / f"{name}.yml").read_text(encoding="utf-8"))

    def test_approved_artifact_table_matches_the_real_bundles(self) -> None:
        for component in self.actual["components"]:
            name = component["name"]
            with self.subTest(component=name):
                approved = self.expected[name]
                self.assertTrue(approved["bundle"])
                self.assertEqual(approved["sha256"], component["sha256"])
                self.assertEqual(
                    approved["artifact_filename"], component["artifact_filename"]
                )

    def test_configurations_address_the_real_artifact(self) -> None:
        for component in self.actual["components"]:
            name = component["name"]
            with self.subTest(component=name):
                configuration = self._configuration(name)
                self.assertEqual(configuration["id"], component["id"])
                self.assertEqual(
                    configuration["source_artifact_url"], component["source_artifact_url"]
                )

    def test_curation_selectors_address_the_real_bundle_members(self) -> None:
        for component in self.actual["components"]:
            name = component["name"]
            with self.subTest(component=name):
                configuration = self._configuration(name)
                members = {member["path"] for member in component["members"]}
                curated = {
                    curation["path"]
                    for curation in configuration["license_finding_curations"]
                }
                self.assertEqual(curated, members)

    def test_production_naming_keeps_the_resource_download_filenames(self) -> None:
        payloads = {
            "bcdec": b"bcdec tiny payload\n",
            "drlibs": b"dr_libs tiny payload\n",
        }
        for component in self.actual["components"]:
            name = component["name"]
            with self.subTest(component=name):
                payload = payloads[name]
                resources = [
                    {
                        "name": resource["name"],
                        "url": resource["url"],
                        "sha512": _sha512(payload),
                    }
                    for resource in component["resources"]
                ]
                data, extension = licenses._fetch_source(
                    {"name": name}, resources, lambda url: payload
                )
                self.assertEqual(extension, ".tar")
                self.assertEqual(
                    sorted(_bundle_members(data)),
                    sorted(member["path"] for member in component["members"]),
                )

    def _inventory_component(self, name: str) -> dict:
        inventory = json.loads(
            (REPOSITORY / "build" / "license-inventory.json").read_text(encoding="utf-8")
        )
        return next(
            component
            for component in inventory["components"]
            if component.get("name") == name
        )

    def test_redundant_header_choices_are_omitted(self) -> None:
        # ORT AND-combines each header's declared expression with its genuine footer
        # detections and then keeps only the valid remaining choices. For bcdec and drlibs
        # those detections already leave a single valid license combination, so an explicit
        # OR license_choices entry is redundant and only produces an "unused choice"
        # warning. The declared expression stays verbatim, and the approved inventory record
        # must carry no licenseChoice so production _license_choice emits nothing.
        expected_declared = {
            "bcdec": "(MIT OR Unlicense)",
            "drlibs": "(Unlicense OR MIT-0)",
        }
        for name, declared in expected_declared.items():
            with self.subTest(component=name):
                component = self._inventory_component(name)
                self.assertEqual(component["declaredExpression"], declared)
                self.assertIsNone(licenses._license_choice(component))

    def test_remaining_dual_license_choices_are_still_emitted(self) -> None:
        # mbedtls and zstd keep a genuine dual-license choice: their detected findings leave
        # both alternatives valid, so the explicit OR choice must remain, carrying the
        # reviewed selection. The "given" is read straight from the inventory so no copyleft
        # identifier is restated in this uncurated test.
        expected_choice = {"mbedtls": "Apache-2.0", "zstd": "BSD-3-Clause"}
        for name, chosen in expected_choice.items():
            with self.subTest(component=name):
                component = self._inventory_component(name)
                choice = licenses._license_choice(component)
                self.assertIsNotNone(choice)
                self.assertEqual(choice["choice"], chosen)
                self.assertTrue(choice["given"])
                self.assertEqual(choice["given"], component["licenseChoice"]["given"])


if __name__ == "__main__":
    unittest.main()

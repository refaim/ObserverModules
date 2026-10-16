"""Reviewed first-party build/test tooling is isolated as content-addressed components.

The thirteen reference-heavy first-party Python build/test files are scanned in full, but
each is bound to a reviewed, byte-addressed component so that any later edit changes the
component version and every curation keyed to the old bytes stops applying (fail closed).

The reviewed artifact and curation table lives in
``fixtures/license_package_configurations/firstparty_artifacts.json`` so this test source
stays free of unreviewed license-identifier example literals: the table is the single
source of truth and every value below is compared against it, never restated here.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_ROOT = REPO_ROOT / "build"
sys.path.insert(0, str(BUILD_ROOT))

import licenses  # noqa: E402


FIXTURE = (
    REPO_ROOT
    / "build"
    / "tests"
    / "fixtures"
    / "license_package_configurations"
    / "firstparty_artifacts.json"
)
CONFIG_DIR = REPO_ROOT / ".ort" / "package-configurations"
INVENTORY = REPO_ROOT / "build" / "license-inventory.json"
DOCUMENTATION = REPO_ROOT / "docs" / "build-system.md"
LOOPBACK_PREFIX = "http://127.0.0.1:8765/sources/"
PACKAGE_TYPE = "SpdxDocumentFile"
OWN_NOTICE = "LICENSE.txt"
ALLOWED_REASONS = {
    "CODE",
    "DATA_OF",
    "DOCUMENTATION_OF",
    "INCORRECT",
    "NOT_DETECTED",
    "REFERENCE",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _expected_conclusion() -> str:
    """The single reviewed conclusion, owned by the fixture table and read back from it."""

    return _load(FIXTURE)["expected_concluded_license"]


def _entries() -> list[dict]:
    return _load(FIXTURE)["components"]


def _configs() -> dict[str, dict]:
    configs = {}
    for path in sorted(CONFIG_DIR.glob("unpackrat-*.yml")):
        config = _load(path)
        configs[config["id"].split(":")[2]] = config
    return configs


def _inventory_components() -> dict[str, dict]:
    document = _load(INVENTORY)
    return {
        component["name"]: component
        for component in document["components"]
        if isinstance(component, dict) and component.get("name")
    }


def _normalized_documentation() -> str:
    """The build-system docs with every whitespace run collapsed to one space.

    Comparing collapsed text keeps the contract about meaning rather than about how the
    paragraph happens to be wrapped, so rewrapping a sentence never breaks the test.
    """

    return " ".join(DOCUMENTATION.read_text(encoding="utf-8").split())


def _start_lines(value):
    if isinstance(value, int):
        return [value]
    return [int(part) for part in str(value).split(",")]


class FirstPartyInventory(unittest.TestCase):
    """Membership and metadata of the isolated first-party components."""

    def setUp(self) -> None:
        self.entries = _entries()
        self.inventory = _inventory_components()

    def test_the_fixture_covers_exactly_the_thirteen_first_party_files(self) -> None:
        self.assertEqual(len(self.entries), 13)
        members = {entry["member"] for entry in self.entries}
        self.assertEqual(len(members), 13)
        self.assertEqual(len({entry["name"] for entry in self.entries}), 13)
        # The two adopted build-tooling sources must be present as reviewed members.
        for member in (
            "build/licenses.py",
            "build/tests/test_license_source_membership.py",
        ):
            self.assertIn(member, members)
        for entry in self.entries:
            with self.subTest(member=entry["member"]):
                self.assertTrue(entry["member"].startswith("build/"))
                self.assertTrue((REPO_ROOT / entry["member"]).is_file())

    def test_every_fixture_entry_has_a_reviewed_inventory_component(self) -> None:
        for entry in self.entries:
            with self.subTest(component=entry["name"]):
                component = self.inventory[entry["name"]]
                self.assertEqual(component["kind"], "adopted-source")
                self.assertEqual(component["scope"], "build")
                self.assertEqual(component["source"]["origin"], "repository")
                self.assertIs(component["incorporated"], True)
                self.assertIs(component["packaged"], False)
                self.assertEqual(component["appliesTo"], [])
                self.assertEqual(component["declaredExpression"], "MIT")
                self.assertEqual(component["notices"], [OWN_NOTICE])
                self.assertEqual(component["members"], [entry["member"]])
                self.assertTrue(component["notes"].strip())

    def test_each_first_party_file_is_claimed_by_exactly_one_component(self) -> None:
        claimed = licenses._claimed_members(list(self.inventory.values()))
        members = [entry["member"] for entry in self.entries]
        for member in members:
            with self.subTest(member=member):
                self.assertIn(member, claimed)
                self.assertEqual(members.count(member), 1)

    def test_first_party_components_stay_scanned_not_excluded(self) -> None:
        # Each member is read as real bytes and stays linked into the package graph.
        own = next(c for c in self.inventory.values() if c["kind"] == "own-source")
        for entry in self.entries:
            with self.subTest(member=entry["member"]):
                self.assertFalse(licenses._matches_any(entry["member"], own["exclude"]))
                files = {entry["member"]: (REPO_ROOT / entry["member"]).read_bytes()}
                component = licenses._repository_component(
                    entry["name"], self.inventory[entry["name"]], files
                )
                self.assertEqual(component["shareData"], files)
                self.assertEqual(component["scope"], "build")
                self.assertFalse(component["staticallyLinked"])


class FirstPartyContentAddress(unittest.TestCase):
    """The component identity is a deterministic content address of the live bytes."""

    def setUp(self) -> None:
        self.entries = _entries()

    def test_component_versions_bind_to_the_live_file_bytes(self) -> None:
        for entry in self.entries:
            with self.subTest(member=entry["member"]):
                data = licenses.deterministic_tar(
                    {entry["member"]: (REPO_ROOT / entry["member"]).read_bytes()}
                )
                self.assertEqual(_sha256(data), entry["sha256"])

    def test_a_changed_member_changes_the_content_address(self) -> None:
        for entry in self.entries:
            with self.subTest(member=entry["member"]):
                changed = licenses.deterministic_tar(
                    {entry["member"]: b"# edited first-party source\n"}
                )
                self.assertNotEqual(_sha256(changed), entry["sha256"])

    def test_deterministic_tar_is_byte_identical_for_the_same_member(self) -> None:
        member = self.entries[0]["member"]
        payload = (REPO_ROOT / member).read_bytes()
        self.assertEqual(
            licenses.deterministic_tar({member: payload}),
            licenses.deterministic_tar({member: payload}),
        )


class FirstPartyPackageConfiguration(unittest.TestCase):
    """Every first-party component has one reviewed, byte-bound package configuration."""

    def setUp(self) -> None:
        self.entries = _entries()
        self.configs = _configs()

    def test_one_configuration_per_first_party_component(self) -> None:
        self.assertEqual(len(self.configs), 13)
        self.assertEqual(
            sorted(self.configs), sorted(entry["name"] for entry in self.entries)
        )

    def test_configuration_ids_bind_the_exact_content_address(self) -> None:
        for entry in self.entries:
            config = self.configs[entry["name"]]
            with self.subTest(component=entry["name"]):
                self.assertEqual(
                    config["id"],
                    f"{PACKAGE_TYPE}:vcpkg:{entry['name']}:{entry['sha256']}",
                )
                self.assertEqual(entry["version"], entry["sha256"])
                self.assertEqual(
                    config["source_artifact_url"],
                    LOOPBACK_PREFIX + entry["sha256"] + ".tar",
                )
                self.assertEqual(entry["artifact_filename"], entry["sha256"] + ".tar")
                self.assertEqual(entry["extension"], ".tar")
                for char in ("*", "(", ")", "[", "]", ",", "?"):
                    self.assertNotIn(char, config["id"])

    def test_every_curation_is_exact_well_formed_and_removes_the_finding(self) -> None:
        for entry in self.entries:
            config = self.configs[entry["name"]]
            with self.subTest(component=entry["name"]):
                self.assertTrue(config.get("license_finding_curations"))
                for curation in config["license_finding_curations"]:
                    self.assertEqual(curation["path"], entry["member"])
                    lines = _start_lines(curation["start_lines"])
                    self.assertTrue(lines)
                    self.assertTrue(all(line > 0 for line in lines))
                    self.assertIsInstance(curation["line_count"], int)
                    self.assertGreater(curation["line_count"], 0)
                    self.assertTrue(curation["detected_license"])
                    self.assertIn(curation["reason"], ALLOWED_REASONS)
                    self.assertTrue(curation["comment"].strip())
                    self.assertEqual(curation["concluded_license"], _expected_conclusion())
                    self.assertNotEqual(
                        curation["detected_license"], curation["concluded_license"]
                    )

    def test_the_fixture_owns_the_expected_conclusion_that_every_curation_reaches(self) -> None:
        # The reviewed conclusion is declared once in the fixture table and only consumed
        # here, so the shipped configurations and this expectation share one owned source.
        expected = _expected_conclusion()
        self.assertIsInstance(expected, str)
        self.assertTrue(expected.strip())
        for entry in self.entries:
            config = self.configs[entry["name"]]
            for curation in config["license_finding_curations"]:
                with self.subTest(component=entry["name"]):
                    self.assertEqual(curation["concluded_license"], expected)

    def test_the_reviewed_curation_table_is_present_verbatim(self) -> None:
        keys = (
            "path",
            "start_lines",
            "line_count",
            "detected_license",
            "reason",
            "concluded_license",
        )
        for entry in self.entries:
            config = self.configs[entry["name"]]
            actual = [
                {key: curation[key] for key in keys}
                for curation in config["license_finding_curations"]
            ]
            expected = [{key: curation[key] for key in keys} for curation in entry["curations"]]
            with self.subTest(component=entry["name"]):
                self.assertEqual(expected, actual)

    def test_no_configuration_excludes_a_path(self) -> None:
        # These files are included build/test tooling, never path-excluded data.
        for entry in self.entries:
            with self.subTest(component=entry["name"]):
                self.assertNotIn("path_excludes", self.configs[entry["name"]])


class LicenseAuditDocumentation(unittest.TestCase):
    """The build-system docs describe the license policy data truthfully."""

    def setUp(self) -> None:
        self.docs = _normalized_documentation()

    def test_the_tracked_base_ort_yml_is_documented_as_carrying_no_resolution(self) -> None:
        # The tracked base file is an empty resolution skeleton; the scoped acceptance is
        # generated at prepare time, so the docs must not claim the base already carries it.
        self.assertIn(
            "the tracked base carries no resolutions and no package choices of its own",
            self.docs,
        )

    def test_the_documentation_attributes_the_scoped_resolution_to_license_prepare(self) -> None:
        self.assertIn(
            "`license-prepare` generates the prepared `.ort.yml` by merging that base",
            self.docs,
        )
        self.assertIn("the scoped Observer", self.docs)

    def test_the_documentation_no_longer_claims_the_base_carries_the_resolution(self) -> None:
        self.assertNotIn("carries no repository-wide OR choices", self.docs)

    def test_the_documentation_explains_the_scanned_first_party_components(self) -> None:
        self.assertIn("still read as real bytes and scanned in full", self.docs)
        self.assertIn(
            "each is a content-addressed component whose version is the exact tar digest",
            self.docs,
        )
        self.assertIn(
            "Editing any one of the thirteen changes the deterministic archive id",
            self.docs,
        )

    def test_the_documentation_binds_curation_to_the_exact_artifact_and_expression(self) -> None:
        self.assertIn(
            "A native curation applies only to the reviewed path, the exact reviewed line set and "
            "the full detected expression",
            self.docs,
        )
        self.assertIn("that exact artifact id, version and source artifact URL", self.docs)

    def test_the_documentation_requires_review_and_forbids_blanket_exclusion(self) -> None:
        self.assertIn("re-review the real native ScanCode findings", self.docs)
        self.assertIn("A blanket exclusion is never the fix", self.docs)
        self.assertIn("needs no exemption", self.docs)
        self.assertIn("still fails the evaluator", self.docs)

    def test_the_documentation_points_at_the_reviewed_data_and_test(self) -> None:
        self.assertIn(
            "build/tests/fixtures/license_package_configurations/firstparty_artifacts.json",
            self.docs,
        )
        self.assertIn(".ort/package-configurations/*.yml", self.docs)
        self.assertIn("build/license-inventory.json", self.docs)
        self.assertIn("build/tests/test_firstparty_license_curations.py", self.docs)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

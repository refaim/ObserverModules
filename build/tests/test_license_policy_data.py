"""The ORT license policy is repository data, not a first-party license engine.

The reviewed policy lives in ``.ort.yml`` (currently no waivers), in the ORT global configuration,
and in the pinned, unmodified upstream rules and classifications that CI fetches at run time.  The
smoke fixtures are project-style SPDX 2.3 documents (an index project plus a dependency) and
configuration data the primary will run through the real container; these tests pin the data, not a
re-implemented evaluator.

Upstream facts drive the shape of this data and are asserted directly:

* ``LicenseView.CONCLUDED_OR_DECLARED_AND_DETECTED`` uses the concluded licenses when they are
  present and otherwise falls back to declared/detected.  A dependency must therefore carry
  ``licenseConcluded == "NOASSERTION"`` so a scanned GPL/unknown finding is never hidden; the real
  port expression travels in ``licenseDeclared``.
* ``getLinkageForDependency`` defaults to ``DYNAMIC`` unless the SPDX document carries a
  ``STATIC_LINK`` (or ``CONTAINS``) relationship, and ``COPYLEFT_LIMITED_IN_DEPENDENCY`` only fires
  for a *statically linked* dependency.  Our runtime dependencies are statically linked, so every
  fixture carries a truthful ``STATIC_LINK`` relationship.
* The default ``detectedLicenseMapping`` maps ``LicenseRef-scancode-unknown``,
  ``LicenseRef-scancode-unknown-spdx``, ``LicenseRef-scancode-free-unknown``,
  ``LicenseRef-scancode-other-copyleft`` and ``LicenseRef-scancode-other-permissive`` to
  ``NOASSERTION``.  An unknown or category-"other" finding would therefore be silently hidden, so
  the configuration pins those keys to themselves (identity) and keeps the remaining trigger
  mappings unchanged.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
FIXTURES = REPOSITORY / "build" / "tests" / "fixtures" / "ort-smoke"

# name -> (declared, outcome).  Every dependency conclusion is NOASSERTION.
# The forbidden-gpl, forbidden-lgpl and unknown dependencies keep the same MIT baseline declared as
# the allowed case and only differ in their source payload, so the audit must fail on a *detected*
# finding.  or-choice-gpl-file selects the Apache option of a real OR yet still carries a detected
# GPL file, so a choice cannot discharge a detected copyleft obligation.
SMOKE = {
    "allowed-mit": ("MIT", "pass"),
    "forbidden-gpl": ("MIT", "fail"),
    "forbidden-lgpl": ("MIT", "fail"),
    "unknown": ("MIT", "fail"),
    "or-apache-gpl": ("Apache-2.0 OR GPL-2.0-or-later", "pass"),
    "or-choice-gpl-file": ("Apache-2.0 OR GPL-2.0-or-later", "fail"),
    "and-mit-gpl": ("MIT AND GPL-3.0-only", "fail"),
}

# The ORT identifier is ``type:namespace:name:version``.  The namespace is taken from the SPDX
# supplier organization, so a package with ``supplier == "Organization: godot-fixture"`` has exactly
# four coordinates.  The earlier five-coordinate ``SpdxDocumentFile::godot-fixture:godot:1.0.1``
# treated the organization as the name of an unnamed package and was wrong.
GODOT_ID = "SpdxDocumentFile:godot-fixture:godot:1.0.1"
RENPY_ID = "SpdxDocumentFile:renpy-fixture:renpy:3.1.3"

# The poison payload of each failing dependency carries an explicit SPDX license identifier, so the
# detected finding is deterministic instead of relying on prose matching.
POISON = {
    "forbidden-gpl": "spdx-license-identifier: gpl-3.0-only",
    "forbidden-lgpl": "spdx-license-identifier: lgpl-3.0-only",
    "unknown": "spdx-license-identifier: licenseref-unreviewed",
}

# Representative violation messages produced by the pinned upstream rule templates.  The copyleft
# rule formats: "The dependency '<coordinates>' is licensed under the ScanCode 'copyleft' categorized
# license <license>."  The limited-copyleft rule adds "statically linked and".  The unknown rule
# spells out the detected trigger.  No waiver may match any of these forms.
COPYLEFT_MESSAGE = (
    f"The dependency '{GODOT_ID}' is licensed under the ScanCode 'copyleft' "
    "categorized license GPL-3.0-only."
)
COPYLEFT_LIMITED_MESSAGE = (
    f"The dependency '{RENPY_ID}' is statically linked and licensed under the "
    "ScanCode 'copyleft-limited' categorized license LGPL-3.0-only."
)
UNKNOWN_MESSAGE = (
    f"The dependency '{RENPY_ID}' might contain a license which is unknown to the  "
    "tooling. It was detected as LicenseRef-scancode-unknown-spdx which is just a trigger, but not a "
    "real license. Please create a dedicated license identifier if the finding is valid."
)

# The ScanCode license keys whose default mapping would hide a new finding.  Each must keep its own
# identity instead of the default NOASSERTION.
UNKNOWN_IDENTITY_MAPPINGS = (
    "LicenseRef-scancode-unknown",
    "LicenseRef-scancode-unknown-spdx",
    "LicenseRef-scancode-unknown-license-reference",
    "LicenseRef-scancode-free-unknown",
    "LicenseRef-scancode-other-copyleft",
    "LicenseRef-scancode-other-permissive",
)


class RepositoryConfigurationTests(unittest.TestCase):
    """``.ort.yml`` is JSON (a valid YAML subset) with no blanket or component waiver."""

    def setUp(self) -> None:
        self.text = (REPOSITORY / ".ort.yml").read_text(encoding="utf-8")
        self.config = json.loads(self.text)

    def test_the_base_configuration_is_json_so_a_generated_choice_file_merges_trivially(self) -> None:
        self.assertIsInstance(self.config, dict)

    def test_the_repository_configuration_carries_no_repository_wide_or_choices(self) -> None:
        # The OR choices are per package and version, generated from verified metadata; a
        # repository-wide choice would apply to every dependency and must not exist here.
        self.assertNotIn("license_choices", self.config)
        self.assertNotIn("license_choices:", self.text)

    def test_the_base_configuration_waives_no_rule_violation(self) -> None:
        # The Observer API-header acceptance is still pending the primary's real identity/provenance
        # and content decision, so the base carries no resolution at all.  A fabricated waiver (for
        # example a dynamic-linkage exception for a copied header) must never exist, and no pattern
        # may match a detected copyleft, limited-copyleft or unknown finding.
        resolutions = self.config.get("resolutions", {})
        self.assertEqual(resolutions.get("rule_violations", []), [])

        entries = resolutions.get("rule_violations", [])
        for message in (COPYLEFT_MESSAGE, COPYLEFT_LIMITED_MESSAGE, UNKNOWN_MESSAGE):
            for entry in entries:
                with self.subTest(message=message):
                    self.assertIsNone(re.search(entry["message"], message))
        self.assertNotIn("DYNAMIC_LINKAGE_EXCEPTION", self.text)
        self.assertNotIn("CANT_FIX_EXCEPTION", self.text)

    def test_the_repository_configuration_carries_no_first_party_evaluator_code(self) -> None:
        # Policy is data: no Kotlin rule bodies, no custom engine, nothing to cover.
        self.assertNotIn("fun ", self.text)
        self.assertNotIn("RuleSet", self.text)


class OrtConfigTests(unittest.TestCase):
    """The ORT global configuration is our data; the upstream policy is fetched, not vendored."""

    def setUp(self) -> None:
        self.config = (REPOSITORY / ".ort" / "config" / "config.yml").read_text(encoding="utf-8")
        self.data_lines = [
            line
            for line in self.config.splitlines()
            if line.strip() and line.strip() != "---" and not line.lstrip().startswith("#")
        ]

    def test_the_global_configuration_is_rooted_at_ort_and_keeps_scans_of_concluded_packages(self) -> None:
        self.assertEqual(self.data_lines[0], "ort:")
        self.assertIn("skipConcluded: false", self.config)
        self.assertIn("enableRepositoryPackageCurations: true", self.config)
        self.assertIn("enableRepositoryPackageConfigurations: true", self.config)
        self.assertIn("severeRuleViolationThreshold: ERROR", self.config)
        self.assertIn("severeIssueThreshold: ERROR", self.config)

    def test_only_the_spdx_importer_and_artifact_downloads_are_enabled(self) -> None:
        self.assertIn("enabledPackageManagers: [SpdxDocumentFile]", self.config)
        self.assertIn("sourceCodeOrigins: [ARTIFACT]", self.config)

    def test_the_unknown_license_keys_are_pinned_to_their_own_identity(self) -> None:
        # A default NOASSERTION mapping would delete the finding before the evaluator sees it, so the
        # unknown and category-"other" keys must map to themselves.
        for key in UNKNOWN_IDENTITY_MAPPINGS:
            with self.subTest(key=key):
                self.assertIn(f'{key}: "{key}"', self.config)

    def test_the_one_curation_provider_is_our_explicit_prepared_file(self) -> None:
        # A single explicit File provider replaces the implicit DefaultDir/DefaultFile/Spring
        # providers, so a missing prepared curations file fails instead of silently curating nothing.
        self.assertIn("packageCurationProviders:", self.config)
        self.assertIn("id: PreparedInputs", self.config)
        self.assertIn("path: /ort/home/config/curations.yml", self.config)
        self.assertIn("mustExist: true", self.config)
        for implicit in ("type: DefaultDir", "type: DefaultFile", "type: Spring"):
            with self.subTest(provider=implicit):
                self.assertNotIn(implicit, self.config)

    def test_the_scan_resource_limit_is_explicit_and_the_cache_uses_local_storage(self) -> None:
        self.assertIn("commandLineNonConfig: '--processes,2'", self.config)
        self.assertIn("directory: /ort/home/scanner/archive", self.config)
        self.assertIn("directory: /ort/home/scanner/file-lists", self.config)
        self.assertIn("directory: /ort/home/scanner/provenance", self.config)
        self.assertIn("storageReaders: [local]", self.config)
        self.assertIn("storageWriters: [local]", self.config)

    def test_the_named_local_scanner_storage_is_defined_for_the_readers_and_writers(self) -> None:
        # storageReaders/storageWriters name the storage ``local``, so that name must resolve to a
        # real backend or the scan exits with "Could not resolve storage 'local'".  The one backend is
        # the native local file storage under the same cached ORT data subtree as the other storages.
        self.assertIn("storageReaders: [local]", self.config)
        self.assertIn("storageWriters: [local]", self.config)
        lines = self.config.splitlines()
        storages = lines.index("    storages:")
        local = lines.index("      local:", storages)
        backend = lines.index("        backend:", local)
        local_file_storage = lines.index("          localFileStorage:", backend)
        options = []
        for line in lines[local_file_storage + 1:]:
            if line.strip() and not line.startswith("            "):
                break
            options.append(line.strip())
        self.assertIn("directory: /ort/home/scanner/results", options)
        self.assertIn("compression: false", options)

    def test_the_upstream_rules_and_classifications_are_not_vendored_as_project_source(self) -> None:
        # They are Apache-2.0 upstream artifacts; CI fetches the pinned commit and copies them at run
        # time so the repository does not carry a second licensed source component with its notices.
        self.assertFalse((REPOSITORY / ".ort" / "config" / "evaluator.rules.kts").exists())
        self.assertFalse((REPOSITORY / ".ort" / "config" / "license-classifications.yml").exists())


class SmokeFixtureTests(unittest.TestCase):
    """Each smoke fixture is a project-style SPDX 2.3 document: an index project plus a dependency."""

    def _load(self, name: str) -> dict:
        return json.loads((FIXTURES / name / "unpackrat.spdx.json").read_text(encoding="utf-8"))

    @staticmethod
    def _project_and_dependency(document: dict) -> tuple[dict, dict]:
        project = next(pkg for pkg in document["packages"] if not pkg.get("packageFileName"))
        dependency = next(pkg for pkg in document["packages"] if pkg.get("packageFileName"))
        return project, dependency

    def test_the_manifest_lists_every_smoke_fixture_once(self) -> None:
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        names = [entry["name"] for entry in manifest["fixtures"]]
        self.assertEqual(sorted(names), sorted(SMOKE))
        self.assertEqual(
            {entry["name"]: entry["outcome"] for entry in manifest["fixtures"]},
            {name: outcome for name, (_, outcome) in SMOKE.items()},
        )

    def test_every_fixture_is_a_well_formed_project_style_spdx_document(self) -> None:
        for name in SMOKE:
            with self.subTest(fixture=name):
                document = self._load(name)
                self.assertEqual(document["spdxVersion"], "SPDX-2.3")
                self.assertEqual(document["dataLicense"], "CC0-1.0")
                self.assertEqual(document["SPDXID"], "SPDXRef-DOCUMENT")
                self.assertTrue(document["documentNamespace"])
                self.assertTrue(document["creationInfo"]["created"])
                # An index project alone does not test the dependency policy: the document must
                # describe a project and at least one real dependency.
                self.assertEqual(len(document["packages"]), 2)
                project, dependency = self._project_and_dependency(document)
                self.assertNotEqual(project["SPDXID"], dependency["SPDXID"])
                relationships = document["relationships"]
                self.assertTrue(
                    any(
                        relation["relationshipType"] == "DESCRIBES"
                        and relation["spdxElementId"] == "SPDXRef-DOCUMENT"
                        and relation["relatedSpdxElement"] == project["SPDXID"]
                        for relation in relationships
                    )
                )
                self.assertTrue(
                    any(
                        relation["relationshipType"] == "DEPENDENCY_OF"
                        and relation["spdxElementId"] == dependency["SPDXID"]
                        and relation["relatedSpdxElement"] == project["SPDXID"]
                        for relation in relationships
                    )
                )

    def test_every_dependency_is_statically_linked_into_the_project(self) -> None:
        # The importer reads a default-scope linkage relationship as "<dependant> depends on
        # <dependency>": ``getLinkageForDependency`` looks up ``getRelationships(dependant,
        # dependency)`` and ``hasDefaultScopeLinkage`` reads ``A STATIC_LINK B`` as A depending on B.
        # The truthful edge therefore runs from the project (dependant) to the dependency, exactly the
        # reverse of the ``DEPENDENCY_OF`` scope edge.  The earlier dependency->project edge was read
        # as a second, opposite dependency (an analyzer cycle), and ``getLinkageForDependency(project,
        # dependency)`` still fell back to DYNAMIC because no relationship started from the project.
        for name in SMOKE:
            with self.subTest(fixture=name):
                document = self._load(name)
                project, dependency = self._project_and_dependency(document)
                relationships = document["relationships"]
                static_edges = [
                    relation
                    for relation in relationships
                    if relation["relationshipType"] == "STATIC_LINK"
                ]
                # Exactly one static edge, and it points from the dependant (project) to the
                # dependency, never the reversed direction.
                self.assertEqual(len(static_edges), 1)
                self.assertEqual(static_edges[0]["spdxElementId"], project["SPDXID"])
                self.assertEqual(static_edges[0]["relatedSpdxElement"], dependency["SPDXID"])
                self.assertTrue(
                    any(
                        relation["relationshipType"] == "DEPENDENCY_OF"
                        and relation["spdxElementId"] == dependency["SPDXID"]
                        and relation["relatedSpdxElement"] == project["SPDXID"]
                        for relation in relationships
                    ),
                    "the static edge must be paired with the reverse DEPENDENCY_OF scope edge",
                )
                # A reversed or duplicated linkage edge would be an extra opposite dependency and the
                # importer's cycle; no relationship may link the dependency back to the project as a
                # linkage relationship.
                self.assertFalse(
                    any(
                        relation["relationshipType"] in {"STATIC_LINK", "DYNAMIC_LINK", "CONTAINS"}
                        and relation["spdxElementId"] == dependency["SPDXID"]
                        and relation["relatedSpdxElement"] == project["SPDXID"]
                        for relation in relationships
                    ),
                    "the fixture must not contain a reversed linkage edge (an analyzer cycle)",
                )

    def test_every_dependency_concludes_nothing_so_scanned_findings_are_never_hidden(self) -> None:
        for name in SMOKE:
            with self.subTest(fixture=name):
                _, dependency = self._project_and_dependency(self._load(name))
                self.assertEqual(dependency["licenseConcluded"], "NOASSERTION")
                self.assertTrue(
                    dependency["downloadLocation"].startswith("http://127.0.0.1:8765/sources/")
                )

    def test_each_fixture_encodes_the_reviewed_smoke_case(self) -> None:
        for name, (declared, outcome) in SMOKE.items():
            with self.subTest(fixture=name):
                _, dependency = self._project_and_dependency(self._load(name))
                self.assertEqual(dependency["licenseDeclared"], declared)
                expected = json.loads((FIXTURES / name / "expected.json").read_text(encoding="utf-8"))
                self.assertEqual(expected["fixture"], name)
                self.assertEqual(expected["outcome"], outcome)
                self.assertEqual(expected["declared"], declared)
                self.assertEqual(expected["concluded"], "NOASSERTION")
                self.assertEqual(expected["linkage"], "STATIC")

    def test_the_forbidden_and_unknown_fixtures_keep_permissive_metadata_and_carry_poison(self) -> None:
        # The metadata is intentionally unchanged: the dependency declares the same MIT baseline as
        # the allowed case, yet the payload carries a new copyleft, limited-copyleft or unrecognised
        # file so a detected finding must still fail the audit.
        for name in POISON:
            with self.subTest(fixture=name):
                _, dependency = self._project_and_dependency(self._load(name))
                self.assertEqual(dependency["licenseDeclared"], "MIT")
                poison_files = sorted((FIXTURES / name / "payload").glob("*.c"))
                self.assertTrue(poison_files, "a poison payload file must be present")

    def test_each_poison_payload_declares_its_deterministic_finding(self) -> None:
        for name, identifier in POISON.items():
            with self.subTest(fixture=name):
                combined = "\n".join(
                    path.read_text(encoding="utf-8")
                    for path in sorted((FIXTURES / name / "payload").glob("*.c"))
                ).lower()
                self.assertIn(identifier, combined)

    def test_the_reviewed_or_choice_uses_the_four_coordinate_package_id(self) -> None:
        # The ORT identifier takes its namespace from the SPDX supplier organization, so the choice
        # for the godot-fixture package must be the four-coordinate id, never the earlier
        # five-coordinate form with an empty namespace.
        expected = json.loads(
            (FIXTURES / "or-apache-gpl" / "expected.json").read_text(encoding="utf-8")
        )
        choice = expected["package_license_choice"]
        self.assertEqual(choice["id"], GODOT_ID)
        self.assertEqual(choice["id"].split(":"), GODOT_ID.split(":"))
        self.assertEqual(len(choice["id"].split(":")), 4)
        self.assertNotIn("::", choice["id"])
        self.assertIn(choice["given"], expected["declared"])
        self.assertIn(choice["choice"], choice["given"].split(" OR "))
        self.assertNotIn("GPL", choice["choice"])

    def test_the_or_choice_cannot_hide_a_detected_copyleft_file(self) -> None:
        # A per-package OR choice reduces the effective expression only where a genuine OR alternative
        # exists; a detected GPL file in the same package keeps its own obligation.
        expected = json.loads(
            (FIXTURES / "or-choice-gpl-file" / "expected.json").read_text(encoding="utf-8")
        )
        self.assertEqual(expected["outcome"], "fail")
        self.assertEqual(expected["package_license_choice"]["id"], GODOT_ID)
        _, dependency = self._project_and_dependency(self._load("or-choice-gpl-file"))
        self.assertIn(" OR ", dependency["licenseDeclared"])

    def test_the_and_case_keeps_its_obligation(self) -> None:
        and_expected = json.loads(
            (FIXTURES / "and-mit-gpl" / "expected.json").read_text(encoding="utf-8")
        )
        # A choice may reduce a genuine OR alternative; it never discharges an AND obligation.
        self.assertNotIn("package_license_choice", and_expected)
        self.assertIn(" AND ", and_expected["declared"])


class DataAdapterFixtureTests(unittest.TestCase):
    """The data-adapter gap fixtures the primary's adapter tests consume."""

    def test_the_missing_notice_and_missing_component_gaps_are_recorded(self) -> None:
        for name, gap in (("missing-notice", "notice"), ("missing-component", "component")):
            with self.subTest(fixture=name):
                path = FIXTURES / "data-adapter" / name / "gap.json"
                record = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(record["fixture"], name)
                self.assertEqual(record["gap"], gap)
                self.assertEqual(record["expected"], "fail")
                self.assertTrue(record["comment"])
                # The adapter owns the readiness rules; the fixture only supplies data.
                self.assertIsInstance(record["packages"], list)
                self.assertTrue(record["packages"])


if __name__ == "__main__":
    unittest.main()

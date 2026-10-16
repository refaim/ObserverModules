"""The reviewed component inventory is the single source of license provenance.

The README no longer keeps a hand-maintained notice table; every retained notice,
upstream identity and reviewed scope lives in ``build/license-inventory.json`` and must
stay complete and internally consistent.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "build" / "license-inventory.json"
ALLOWED_SCOPES = {"runtime", "test", "build", "reference"}

BUILD_ROOT = ROOT / "build"
sys.path.insert(0, str(BUILD_ROOT))

import licenses  # noqa: E402
import main  # noqa: E402


YY_COMMIT = "b41fb80b8d316581eec0959600aeb0e7339d0e5e"
YY_ARCHIVE = (
    "https://github.com/Chuyu-Team/YY-Thunks/archive/" + YY_COMMIT + ".tar.gz"
)
YY_RECIPE = "build/vcpkg/ports/yy-thunks/portfile.cmake"
YY_MANIFEST = "build/vcpkg/ports/yy-thunks/vcpkg.json"
YY_PATCHES = (
    "build/vcpkg/ports/yy-thunks/ordinal-fallback.patch",
    "build/vcpkg/ports/yy-thunks/hardened-build.patch",
)
YY_TRIPLETS = (
    "build/vcpkg/triplets/unpackrat-x86-windows-static.cmake",
    "build/vcpkg/triplets/unpackrat-x64-windows-static.cmake",
)


class LicenseInventory(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(INVENTORY.read_text(encoding="utf-8"))

    def test_inventory_schema_and_shape(self) -> None:
        self.assertEqual(self.document["schema"], "unpackrat-license-inventory/3")
        self.assertIsInstance(self.document["rootPackage"], dict)
        components = self.document["components"]
        self.assertIsInstance(components, list)
        self.assertTrue(components)
        for component in components:
            with self.subTest(component=component.get("name") if isinstance(component, dict) else component):
                self.assertIsInstance(component, dict)
                self.assertTrue(component["name"])
                self.assertIn(
                    component["kind"], {"vcpkg-package", "adopted-source", "own-source"}
                )
                self.assertIn(component["scope"], ALLOWED_SCOPES)
                self.assertIsInstance(component["source"]["origin"], str)

    def test_every_retained_notice_is_named_by_the_component_inventory(self) -> None:
        # Every retained upstream notice is referenced by exactly the reviewed inventory; no notice
        # may drift in or out of the inventory unnoticed.
        notices = [
            str(notice)
            for component in self.document["components"]
            for notice in component.get("notices", [])
        ] + [str(notice) for notice in self.document["rootPackage"]["notices"]]
        referenced = {
            notice.split("/", 1)[1] for notice in notices if notice.startswith("licenses/")
        }
        retained = {path.name for path in (ROOT / "licenses").glob("*.txt")}
        self.assertEqual(retained, referenced)

    def test_every_inventory_notice_resolves_to_a_real_file(self) -> None:
        notices = {
            str(notice)
            for component in self.document["components"]
            for notice in component.get("notices", [])
        } | {str(notice) for notice in self.document["rootPackage"]["notices"]}
        for notice in sorted(notices):
            with self.subTest(notice=notice):
                self.assertTrue((ROOT / notice).is_file(), notice)

    def test_every_vcpkg_component_records_a_stable_source_origin(self) -> None:
        # Ordinary version updates stay allowed only because the reviewed source identity is pinned.
        for component in self.document["components"]:
            if component.get("kind") != "vcpkg-package":
                continue
            with self.subTest(component=component["name"]):
                self.assertTrue(component["vcpkgName"])
                self.assertIsInstance(component.get("features", []), list)
                if component["source"].get("url"):
                    self.assertTrue(component["source"].get("identity"))


class ObserverApiResolution(unittest.TestCase):
    """The one reviewed can't-fix resolution stays truthful and fail-closed.

    The retained Observer API header is adapted, not an unmodified upstream copy, so the
    recorded comment must say so and the approved-member digest must still bind the exact
    live bytes: any edit changes the digest and the resolution fails closed until re-reviewed.
    """

    def setUp(self) -> None:
        document = json.loads(INVENTORY.read_text(encoding="utf-8"))
        self.component = next(
            component
            for component in document["components"]
            if isinstance(component, dict) and component.get("name") == "observer-api"
        )
        self.policy = self.component["policyResolution"]

    def test_the_resolution_keeps_the_reviewed_exception_identity(self) -> None:
        self.assertEqual(self.policy["reason"], "CANT_FIX_EXCEPTION")
        self.assertEqual(self.policy["license"], self.component["declaredExpression"])
        self.assertEqual(self.component["members"], ["src/api.h"])
        self.assertEqual(self.component["scope"], "runtime")
        self.assertEqual(self.component["source"]["origin"], "repository")

    def test_the_comment_describes_an_adaptation_not_an_unmodified_header(self) -> None:
        comment = self.policy["comment"]
        # The header carries local includes and platform adaptations, so a claim of byte
        # identity with upstream is false and must not appear.
        self.assertNotIn("unmodified", comment.lower())
        self.assertTrue(
            "retains" in comment.lower() or "adapts" in comment.lower(),
            comment,
        )
        self.assertIn(self.component["declaredExpression"], comment)
        self.assertTrue(comment.strip())

    def test_the_approved_member_digest_binds_the_live_bytes(self) -> None:
        live = hashlib.sha256((ROOT / "src" / "api.h").read_bytes()).hexdigest()
        self.assertEqual(self.policy["approvedMembers"], {"src/api.h": live})

    def test_a_changed_header_would_not_match_the_approved_digest(self) -> None:
        approved = self.policy["approvedMembers"]["src/api.h"]
        edited = hashlib.sha256(b"// edited header\n").hexdigest()
        self.assertNotEqual(approved, edited)


class YyThunksRuntimeDependency(unittest.TestCase):
    """The pinned YY-Thunks object is a reviewed runtime dependency of every module.

    The XP WinAPI fallback object is compiled into all four modules, so its runtime
    provenance (source origin, license, notice) and the packaging route that ships its
    MIT grant must stay truthful.  The reviewed build configuration must also bind the
    live recipe, both patches and both Release triplets, so a change to any build input
    that selects what is compiled forces a renewed review.
    """

    def setUp(self) -> None:
        self.document = json.loads(INVENTORY.read_text(encoding="utf-8"))
        self.component = next(
            (
                component
                for component in self.document["components"]
                if isinstance(component, dict) and component.get("name") == "yy-thunks"
            ),
            None,
        )

    def test_the_runtime_component_is_reviewed_for_every_module(self) -> None:
        self.assertIsNotNone(self.component, "yy-thunks is absent from the inventory")
        component = self.component
        self.assertEqual(component["kind"], "vcpkg-package")
        self.assertEqual(component["scope"], "runtime")
        self.assertIs(component["incorporated"], True)
        self.assertIs(component["packaged"], True)
        self.assertEqual(set(component["appliesTo"]), set(main.MODULES))
        self.assertEqual(component["declaredExpression"], "MIT")
        self.assertEqual(component["vcpkgName"], "yy-thunks")
        self.assertEqual(component["features"], [])
        self.assertTrue(component["notes"].strip())

    def test_the_reviewed_source_is_the_pinned_chuyu_team_archive(self) -> None:
        self.assertIsNotNone(self.component, "yy-thunks is absent from the inventory")
        source = self.component["source"]
        self.assertEqual(source["origin"], "overlay")
        self.assertEqual(source["identity"], "github.com/chuyu-team/yy-thunks")
        self.assertEqual(source["recipe"], YY_RECIPE)
        self.assertEqual(source["patches"], list(YY_PATCHES))
        self.assertEqual(source["url"], YY_ARCHIVE)

    def test_the_mit_notice_is_retained_and_shipped_to_every_module(self) -> None:
        self.assertIsNotNone(self.component, "yy-thunks is absent from the inventory")
        self.assertEqual(self.component["notices"], ["licenses/YY-Thunks.txt"])
        text = (ROOT / "licenses" / "YY-Thunks.txt").read_text(encoding="utf-8")
        self.assertIn("MIT License", text)
        self.assertIn("Chuyu-Team", text)
        for module in main.MODULES:
            with self.subTest(module=module):
                self.assertIn("YY-Thunks.txt", self.document["packaging"][module])

    def test_the_reviewed_packaging_matches_the_real_module_notice_route(self) -> None:
        # The inventory is the reviewed source of the packaged notice set, so it must
        # agree exactly with the route the build actually ships, module by module.
        for module in main.MODULES:
            with self.subTest(module=module):
                self.assertEqual(
                    set(self.document["packaging"][module]), set(main.LICENSES[module])
                )
                for name in self.document["packaging"][module]:
                    self.assertTrue((ROOT / "licenses" / name).is_file(), name)

    def test_the_source_port_recipe_and_both_patches_are_present(self) -> None:
        recipe = (ROOT / YY_RECIPE).read_text(encoding="utf-8")
        self.assertIn("Chuyu-Team/YY-Thunks", recipe)
        self.assertIn(YY_COMMIT, recipe)
        self.assertIn(
            "b949f296a412dfbf901060d4903d3b1dcc1b9afdad61b7eefbee7b86b77e5869"
            "0fd17827e75b2337a3535fc6fe306976d6714058be1d427963c3358c29d273aa",
            recipe,
        )
        for patch in YY_PATCHES:
            with self.subTest(patch=patch):
                self.assertTrue((ROOT / patch).is_file())

    def test_the_build_configuration_binds_the_live_recipe_patches_and_triplets(self) -> None:
        self.assertIsNotNone(self.component, "yy-thunks is absent from the inventory")
        guard = self.component["buildConfiguration"]
        expected = {YY_RECIPE, YY_MANIFEST, *YY_PATCHES, *YY_TRIPLETS}
        self.assertEqual(set(guard), expected)
        identity = licenses.build_input_identity(ROOT)
        for path in sorted(expected):
            with self.subTest(path=path):
                self.assertEqual(guard[path], identity[path])

    def test_the_release_triplets_carry_the_libmaker_guard(self) -> None:
        for triplet in YY_TRIPLETS:
            text = (ROOT / triplet).read_text(encoding="utf-8")
            with self.subTest(triplet=triplet):
                self.assertIn('if(PORT STREQUAL "yy-thunks")', text)
                self.assertIn("VCPKG_HASH_ADDITIONAL_FILES", text)

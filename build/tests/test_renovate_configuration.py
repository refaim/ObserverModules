"""Renovate reports every direct dependency without changing pins before approval."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
UPSTREAMS = {
    "catch2": "catchorg/Catch2",
    "nlohmann-json": "nlohmann/json",
    "xxhash": "Cyan4973/xxHash",
    "zlib": "zlib-ng/zlib-ng",
    "libspng": "randy408/libspng",
    "libogg": "xiph/ogg",
    "brotli": "google/brotli",
    "zstd": "facebook/zstd",
    "zydis": "zyantific/zydis",
    "mbedtls": "Mbed-TLS/mbedtls",
}
BRANCHES = {
    "fastlz": ("ariya/FastLZ", "master"),
    "bcdec": ("iOrange/bcdec", "main"),
    "drlibs": ("mackron/dr_libs", "master"),
}


def expression(pattern: str) -> re.Pattern[str]:
    """Run the shared ECMAScript/RE2 subset with Python named-group syntax."""
    return re.compile(re.sub(r"\(\?<([A-Za-z][A-Za-z0-9_]*)>", r"(?P<\1>", pattern))


class RenovateConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.configuration = json.loads((REPOSITORY / ".github/renovate.json").read_text(encoding="utf-8"))
        self.manifest = json.loads((REPOSITORY / "vcpkg.json").read_text(encoding="utf-8"))
        self.managers = {manager["depNameTemplate"]: manager for manager in self.configuration["customManagers"]}

    def matches(self, name: str, contents: str | None = None) -> list[dict[str, str]]:
        manager = self.managers[name]
        if contents is None:
            file_pattern, = manager["managerFilePatterns"]
            files = [REPOSITORY / "vcpkg.json", *REPOSITORY.glob("build/vcpkg/ports/*/vcpkg.json"),
                     *REPOSITORY.glob("build/vcpkg/ports/*/portfile.cmake")]
            selected = [path for path in files if re.search(file_pattern[1:-1], path.relative_to(REPOSITORY).as_posix())]
            self.assertEqual(len(selected), 1, name)
            contents = selected[0].read_text(encoding="utf-8")
        return [match.groupdict() for pattern in manager["matchStrings"] for match in expression(pattern).finditer(contents)]

    def test_dashboard_requires_approval_and_has_no_automatic_upgrade_path(self) -> None:
        self.assertTrue(self.configuration["dependencyDashboard"])
        self.assertTrue(self.configuration["dependencyDashboardApproval"])
        self.assertFalse(self.configuration["automerge"])
        self.assertTrue(self.configuration["draftPR"])
        self.assertEqual(self.configuration["enabledManagers"], ["custom.regex"])
        self.assertNotIn("extends", self.configuration)
        self.assertNotIn("postUpgradeTasks", self.configuration)
        self.assertNotIn("packageRules", self.configuration)

    def test_every_direct_dependency_has_a_live_version_or_source_ref(self) -> None:
        self.assertEqual(set(self.managers), {dependency["name"] for dependency in self.manifest["dependencies"]})
        for dependency in self.manifest["dependencies"]:
            name = dependency["name"]
            with self.subTest(dependency=name):
                matches = self.matches(name)
                self.assertTrue(matches)
                if name in UPSTREAMS:
                    self.assertEqual(matches, [{"currentValue": dependency["version>="]}])
                elif name == "qoa":
                    descriptor = json.loads((REPOSITORY / "build/vcpkg/ports/qoa/vcpkg.json").read_text(encoding="utf-8"))
                    self.assertEqual(matches, [{"currentValue": descriptor["version-string"]}])
                else:
                    self.assertTrue(all(re.fullmatch("[a-f0-9]{40}", match["currentDigest"]) for match in matches))

    def test_release_sources_use_the_actual_libraries_and_only_stable_tags(self) -> None:
        for name, upstream in UPSTREAMS.items():
            manager = self.managers[name]
            with self.subTest(dependency=name):
                self.assertEqual(manager["packageNameTemplate"], upstream)
                self.assertEqual(manager["datasourceTemplate"], "github-tags")
                self.assertEqual(manager["versioningTemplate"], "semver")
                policy = expression(manager["extractVersionTemplate"])
                self.assertEqual(policy.fullmatch("v99.1.2").group("version"), "99.1.2")
                self.assertEqual(policy.fullmatch("99.1.2").group("version"), "99.1.2")
                self.assertIsNone(policy.fullmatch("v99.1.2-rc1"))
                # The captured version must follow a manual change to the live manifest.
                changed = json.dumps({"dependencies": [{"name": name, "version>=": "99.1.2"}]})
                self.assertEqual(self.matches(name, changed), [{"currentValue": "99.1.2"}])
                self.assertEqual(self.matches(name, '{"name":"unrelated","version>=":"99.1.2"}'), [])

    def test_commit_pins_watch_explicit_upstream_branches(self) -> None:
        for name, (upstream, branch) in BRANCHES.items():
            manager = self.managers[name]
            with self.subTest(dependency=name):
                self.assertEqual(manager["packageNameTemplate"], f"https://github.com/{upstream}")
                self.assertEqual(manager["datasourceTemplate"], "git-refs")
                self.assertEqual(manager["currentValueTemplate"], branch)
                self.assertEqual(manager["versioningTemplate"], "exact")
        drlibs = self.matches("drlibs")
        self.assertEqual(len(drlibs), 2)
        self.assertEqual(drlibs[0], drlibs[1])

    def test_fastlz_metadata_records_the_source_for_its_calendar_version(self) -> None:
        fastlz = next(dependency for dependency in self.manifest["dependencies"] if dependency["name"] == "fastlz")
        self.assertEqual(fastlz["$upstream-ref"], "b1342dabcf5257ab303743c9332fe75e9147a011")
        self.assertEqual(self.matches("fastlz"), [{"currentDigest": fastlz["$upstream-ref"]}])
        changed = json.dumps({"dependencies": [{"name": "fastlz", "version>=": "2099-01-01", "$upstream-ref": "a" * 40}]})
        self.assertEqual(self.matches("fastlz", changed), [{"currentDigest": "a" * 40}])

    def test_qoa_tracks_godot_stable_releases_instead_of_upstream_qoa(self) -> None:
        manager = self.managers["qoa"]
        self.assertEqual(manager["packageNameTemplate"], "godotengine/godot")
        self.assertEqual(manager["datasourceTemplate"], "github-tags")
        policy = expression(manager["versioningTemplate"].removeprefix("regex:"))
        for stable in ("4.5-stable", "4.5.1-stable", "4.6-stable", "5.0-stable"):
            self.assertIsNotNone(policy.fullmatch(stable))
        for unstable in ("4.6-dev1", "4.6-rc1", "4.6-beta1", "4.6", "0.1"):
            self.assertIsNone(policy.fullmatch(unstable))


if __name__ == "__main__":
    unittest.main()

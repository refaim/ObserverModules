"""Contract tests for the ORT native package configurations under .ort/package-configurations.

The files are JSON (which is valid YAML) so they can be validated without a YAML
parser.  Each file binds one exact vcpkg dependency (id + content-addressed source
artifact URL) to the scoped path excludes and license finding curations that were
reviewed against the 2026-10-16 source audit.

Every archived source keeps its upstream top directory in the real ORT ScanCode
tree, so each path/pattern is anchored on that exact archive prefix (the
native_scan_findings fixture records the verified prefixes and the raw findings).
"""

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / ".ort" / "package-configurations"
FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "license_package_configurations"
)
FIXTURE = FIXTURE_DIR / "expected_artifacts.json"
FIRSTPARTY_FIXTURE = FIXTURE_DIR / "firstparty_artifacts.json"
SCANCODE_EVIDENCE = FIXTURE_DIR / "scancode_evidence.json"
NATIVE_FINDINGS = FIXTURE_DIR / "native_scan_findings.json"

LOOPBACK_PREFIX = "http://127.0.0.1:8765/sources/"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
URL_RE = re.compile(
    r"^http://127\.0\.0\.1:8765/sources/[0-9a-f]{64}\.(?:tar|tar\.gz|tar\.bz2)$"
)
ID_RE = re.compile(r"^SpdxDocumentFile:vcpkg:([A-Za-z0-9._-]+):([^\s:]+)$")
# Characters that would introduce an Ivy range or a wildcard version/URL.
WILDCARD_CHARS = ("*", "(", ")", "[", "]", ",", "?")

# The exact ORT 97 PathExcludeReason enum: any other token (for example the rejected
# DEVELOPMENT_TOOL_OF or PROVIDED_BY_PACKAGE_MANAGER) is refused by the real evaluator.
ALLOWED_PATH_EXCLUDE_REASONS = {
    "OTHER",
    "TEST_TOOL_OF",
    "PROVIDED_BY",
    "TEST_OF",
    "DATA_FILE_OF",
    "OPTIONAL_COMPONENT_OF",
    "DOCUMENTATION_OF",
    "BUILD_TOOL_OF",
    "EXAMPLE_OF",
}
ALLOWED_CURATION_REASONS = {
    "CODE",
    "DATA_OF",
    "DOCUMENTATION_OF",
    "INCORRECT",
    "NOT_DETECTED",
    "REFERENCE",
}

# Paths that must never appear as a blanket exclude/curation pattern.
BLANKET_PATTERNS = {"**", "**/*", "*", "*.*"}


def load_configs():
    configs = {}
    for path in sorted(CONFIG_DIR.glob("*.yml")):
        configs[path.name] = json.loads(path.read_text(encoding="utf-8"))
    return configs


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def load_firstparty_fixture():
    return json.loads(FIRSTPARTY_FIXTURE.read_text(encoding="utf-8"))


def load_scancode_evidence():
    return json.loads(SCANCODE_EVIDENCE.read_text(encoding="utf-8"))


def load_native_findings():
    return json.loads(NATIVE_FINDINGS.read_text(encoding="utf-8"))


def find_config(configs, pkg_name):
    return next(
        c for c in configs.values() if ID_RE.match(c["id"]).group(1) == pkg_name
    )


def curation_applies(curation, finding):
    """Minimal model of ORT's LicenseFindingCuration matching.

    A curation applies only when the finding path, the finding's start line and
    the finding's license id all match exactly.
    """
    return (
        curation["path"] == finding["path"]
        and curation["start_lines"] == finding["start_lines"]
        and curation["line_count"] == finding["line_count"]
        and curation["detected_license"] == finding["detected_license"]
    )


def provenance_matches(config, provenance_url):
    """A package configuration applies only to its own source artifact URL."""
    return config.get("source_artifact_url") == provenance_url


class PackageConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load_fixture()
        cls.library = {entry["name"]: entry for entry in cls.fixture["components"]}
        cls.firstparty = {
            entry["name"]: entry
            for entry in load_firstparty_fixture()["components"]
        }
        cls.components = {**cls.library, **cls.firstparty}
        cls.configs = load_configs()
        cls.native = load_native_findings()
        cls.library_prefixes = cls.native["prefixes"]
        cls.prefixes = {**cls.library_prefixes, **{name: "" for name in cls.firstparty}}

    def test_fixture_covers_all_sixteen_library_dependencies(self):
        self.assertEqual(16, len(self.library))
        self.assertEqual(
            {
                "bcdec",
                "brotli",
                "catch2",
                "drlibs",
                "fastlz",
                "libogg",
                "zlib",
                "libspng",
                "mbedtls",
                "nlohmann-json",
                "qoa",
                "xxhash",
                "zstd",
                "zycore",
                "zydis",
                "yy-thunks",
            },
            set(self.library),
        )
        self.assertEqual(set(self.library), set(self.library_prefixes))

    def test_firstparty_fixture_covers_the_isolated_first_party_files(self):
        # The thirteen reference-heavy first-party Python files are isolated as reviewed,
        # byte-addressed components; each gets exactly one reviewed package configuration.
        self.assertEqual(13, len(self.firstparty))
        configured = {
            ID_RE.match(config["id"]).group(1) for config in self.configs.values()
        }
        for name, entry in self.firstparty.items():
            with self.subTest(component=name):
                self.assertTrue(entry["member"].startswith("build/"))
                self.assertEqual(entry["version"], entry["sha256"])
                self.assertEqual(
                    entry["artifact_filename"], entry["sha256"] + ".tar"
                )
                self.assertEqual(entry["extension"], ".tar")
                self.assertIn(name, configured)

    def test_configs_are_json_yaml_objects_with_unique_package_ids(self):
        self.assertTrue(self.configs, "at least one package configuration is expected")
        ids = []
        for name, config in self.configs.items():
            with self.subTest(config=name):
                self.assertIsInstance(config, dict)
                self.assertNotIsInstance(config, list)
                ids.append(config["id"])
        self.assertEqual(len(ids), len(set(ids)), "duplicate package ids")

    def test_each_config_matches_exactly_one_exact_package_id(self):
        for name, config in self.configs.items():
            with self.subTest(config=name):
                match = ID_RE.match(config["id"])
                self.assertIsNotNone(match, config["id"])
                pkg_name, version = match.groups()
                self.assertIn(pkg_name, self.components)
                entry = self.components[pkg_name]
                self.assertEqual(entry["version"], version)
                for char in WILDCARD_CHARS:
                    self.assertNotIn(char, config["id"])

    def test_source_matcher_is_only_the_loopback_artifact_url(self):
        for name, config in self.configs.items():
            with self.subTest(config=name):
                self.assertIn("source_artifact_url", config)
                self.assertNotIn("vcs", config)
                self.assertNotIn("source_code_origin", config)
                self.assertNotIn("source_artifact_hash", config)

    def test_source_url_is_content_addressed_and_not_wildcarded(self):
        for name, config in self.configs.items():
            with self.subTest(config=name):
                url = config["source_artifact_url"]
                self.assertTrue(url.startswith(LOOPBACK_PREFIX), url)
                self.assertIsNotNone(URL_RE.match(url), url)
                self.assertEqual(url.count("*"), 0)

    def test_url_hash_equals_the_approved_artifact_hash(self):
        for name, config in self.configs.items():
            with self.subTest(config=name):
                url = config["source_artifact_url"]
                pkg_name = ID_RE.match(config["id"]).group(1)
                expected = self.components[pkg_name]
                tail = url[len(LOOPBACK_PREFIX):]
                digest, _, extension = tail.partition(".")
                self.assertTrue(SHA256_RE.match(digest), digest)
                self.assertEqual(expected["sha256"], digest)
                self.assertEqual(expected["artifact_filename"], tail)
                self.assertEqual(expected["extension"], "." + extension)

    def test_path_excludes_are_scoped_and_explained(self):
        for name, config in self.configs.items():
            for exclude in config.get("path_excludes", []):
                with self.subTest(config=name, pattern=exclude.get("pattern")):
                    self.assertIn("pattern", exclude)
                    self.assertIn(exclude["reason"], ALLOWED_PATH_EXCLUDE_REASONS)
                    self.assertTrue(exclude.get("comment", "").strip())
                    pattern = exclude["pattern"]
                    self.assertTrue(
                        _is_scoped_exclude(pattern), f"unscoped exclude: {pattern}"
                    )

    def test_license_finding_curations_are_exact_and_well_formed(self):
        for name, config in self.configs.items():
            for curation in config.get("license_finding_curations", []):
                with self.subTest(config=name, path=curation.get("path")):
                    self.assertIn("start_lines", curation)
                    # ORT serializes LicenseFindingCuration.startLines as a comma-separated
                    # string, so a reviewed line set is a CSV string and a single line may be
                    # given as a bare int. Both forms must hold only positive line numbers.
                    lines = [
                        int(part)
                        for part in str(curation["start_lines"]).split(",")
                        if part.strip()
                    ]
                    self.assertTrue(lines)
                    self.assertTrue(all(line > 0 for line in lines))
                    self.assertIsInstance(curation["line_count"], int)
                    self.assertGreater(curation["line_count"], 0)
                    self.assertTrue(curation["detected_license"])
                    self.assertIn(curation["reason"], ALLOWED_CURATION_REASONS)
                    self.assertTrue(curation.get("comment", "").strip())
                    self.assertTrue(curation["concluded_license"].strip())
                    self.assertNotEqual(
                        curation["detected_license"], curation["concluded_license"]
                    )
                    self.assertNotIn("NOASSERTION", curation["concluded_license"])
                    # ORT's LicenseFindingCuration.detectedLicense is a full
                    # SpdxExpression matched by equality, so compound expressions
                    # (for example a ScanCode rule that reported an OR/AND) are
                    # valid and must be curated with the complete expression.

    def test_curations_target_exact_files_never_globs(self):
        for name, config in self.configs.items():
            for curation in config.get("license_finding_curations", []):
                with self.subTest(config=name, path=curation["path"]):
                    self.assertNotIn(curation["path"], BLANKET_PATTERNS)
                    self.assertNotIn("**", curation["path"])
                    self.assertNotIn("*", curation["path"])

    def test_enumerated_curations_are_present_verbatim(self):
        expected = {
            "bcdec": [
                {
                    "path": "bcdec-80859ed3b7afb1c527a2a99d70c61457bea72d0c.h",
                    "start_lines": 1459,
                    "line_count": 4,
                    "detected_license": "GPL-2.0-only OR Apache-2.0",
                    "concluded_license": "MIT OR Unlicense",
                },
                {
                    "path": "bcdec-80859ed3b7afb1c527a2a99d70c61457bea72d0c.h",
                    "start_lines": 48,
                    "line_count": 1,
                    "detected_license": "LicenseRef-scancode-unknown-license-reference",
                    "concluded_license": "NONE",
                },
            ],
            "zlib": [
                {
                    "path": "zlib-ng-2.3.3/cmake/detect-install-dirs.cmake",
                    "start_lines": 3,
                    "line_count": 1,
                    "detected_license": (
                        "LicenseRef-scancode-proprietary-license AND Apache-2.0"
                    ),
                    "concluded_license": "Zlib",
                },
                {
                    "path": "zlib-ng-2.3.3/cmake/detect-install-dirs.cmake",
                    "start_lines": 3,
                    "line_count": 1,
                    "detected_license": "LicenseRef-scancode-unknown-license-reference",
                    "concluded_license": "NONE",
                },
                {
                    "path": "zlib-ng-2.3.3/FAQ.zlib",
                    "start_lines": 172,
                    "line_count": 1,
                    "detected_license": "GPL-1.0-or-later",
                    "concluded_license": "NONE",
                },
            ],
            "zstd": [
                {
                    "path": "zstd-1.5.7/COPYING",
                    "start_lines": 1,
                    "line_count": 339,
                    "detected_license": "GPL-2.0-only",
                    "concluded_license": "BSD-3-Clause",
                }
            ],
            "mbedtls": [
                {
                    "path": "mbedtls-4.1.1/LICENSE",
                    "start_lines": 2,
                    "line_count": 1,
                    "detected_license": "GPL-2.0-or-later",
                    "concluded_license": "Apache-2.0",
                },
                {
                    "path": "mbedtls-4.1.1/LICENSE",
                    "start_lines": 215,
                    "line_count": 339,
                    "detected_license": "GPL-2.0-only",
                    "concluded_license": "Apache-2.0",
                },
                {
                    "path": "mbedtls-4.1.1/tf-psa-crypto/LICENSE",
                    "start_lines": 2,
                    "line_count": 1,
                    "detected_license": "GPL-2.0-or-later",
                    "concluded_license": "Apache-2.0",
                },
                {
                    "path": "mbedtls-4.1.1/tf-psa-crypto/LICENSE",
                    "start_lines": 214,
                    "line_count": 339,
                    "detected_license": "GPL-2.0-only",
                    "concluded_license": "Apache-2.0",
                },
                {
                    "path": "mbedtls-4.1.1/tf-psa-crypto/include/psa/crypto_config.h",
                    "start_lines": 1625,
                    "line_count": 1,
                    "detected_license": "GPL-2.0-or-later",
                    "concluded_license": "NONE",
                },
            ],
        }
        for pkg_name, expected_curations in expected.items():
            config = next(
                c
                for c in self.configs.values()
                if ID_RE.match(c["id"]).group(1) == pkg_name
            )
            actual = config.get("license_finding_curations", [])
            for expected_curation in expected_curations:
                with self.subTest(pkg=pkg_name, path=expected_curation["path"]):
                    self.assertIn(
                        expected_curation,
                        [
                            {key: c[key] for key in expected_curation}
                            for c in actual
                        ],
                    )

    def test_only_documented_none_curations_remove_findings(self):
        removed = set()
        for name, config in self.configs.items():
            # The isolated first-party components are covered by their own reviewed
            # table (see test_firstparty_license_curations.py); this assertion pins the
            # reviewed library set only.
            if ID_RE.match(config["id"]).group(1) in self.firstparty:
                continue
            for curation in config.get("license_finding_curations", []):
                if curation["concluded_license"] == "NONE":
                    removed.add(
                        (
                            ID_RE.match(config["id"]).group(1),
                            curation["path"],
                            curation["start_lines"],
                            curation["detected_license"],
                        )
                    )
        self.assertEqual(
            {
                ("zlib", "zlib-ng-2.3.3/FAQ.zlib", 172, "GPL-1.0-or-later"),
                (
                    "mbedtls",
                    "mbedtls-4.1.1/tf-psa-crypto/include/psa/crypto_config.h",
                    1625,
                    "GPL-2.0-or-later",
                ),
                ("drlibs", "dr_libs-dfe8377631000664666519fdb83da193fd8037f4-dr_mp3.h", 2, "LicenseRef-scancode-unknown-license-reference"),
                ("drlibs", "dr_libs-dfe8377631000664666519fdb83da193fd8037f4-dr_wav.h", 2, "LicenseRef-scancode-unknown-license-reference"),
                ("bcdec", "bcdec-80859ed3b7afb1c527a2a99d70c61457bea72d0c.h", 48, "LicenseRef-scancode-unknown-license-reference"),
                (
                    "zlib",
                    "zlib-ng-2.3.3/cmake/detect-install-dirs.cmake",
                    3,
                    "LicenseRef-scancode-unknown-license-reference",
                ),
            },
            removed,
        )

    def test_enumerated_scope_excludes_are_present(self):
        expected = {
            "zstd": ["zstd-1.5.7/contrib/**", "zstd-1.5.7/tests/**"],
            "catch2": ["Catch2-3.16.1/tools/**", "Catch2-3.16.1/tests/**"],
            "nlohmann-json": ["json-3.12.0/tests/**"],
            "xxhash": ["xxHash-0.8.4/cli/**", "xxHash-0.8.4/tests/**"],
            "mbedtls": [
                "mbedtls-4.1.1/tf-psa-crypto/drivers/everest/**",
                "mbedtls-4.1.1/tf-psa-crypto/drivers/p256-m/**",
                "mbedtls-4.1.1/tf-psa-crypto/drivers/pqcp/**",
            ],
            "libspng": ["libspng-0.7.4/tests/**"],
            "brotli": ["brotli-1.2.0/CONTRIBUTING.md"],
        }
        for pkg_name, patterns in expected.items():
            config = next(
                c
                for c in self.configs.values()
                if ID_RE.match(c["id"]).group(1) == pkg_name
            )
            actual = {e["pattern"] for e in config.get("path_excludes", [])}
            for pattern in patterns:
                with self.subTest(pkg=pkg_name, pattern=pattern):
                    self.assertIn(pattern, actual)

    def test_config_paths_are_anchored_on_the_archive_prefix(self):
        """Every exclude/curation must start with the package's verified prefix.

        The real ORT ScanCode run keeps the upstream archive top directory in the
        extracted tree, so an entry that omits the prefix would silently miss the
        finding it was written for.
        """
        for name, config in self.configs.items():
            pkg_name = ID_RE.match(config["id"]).group(1)
            if pkg_name in self.firstparty:
                # A first-party component has no archive top directory: its source is the
                # exact tracked file, so every curation path must equal that file path.
                entries = [
                    c["path"] for c in config.get("license_finding_curations", [])
                ]
                self.assertTrue(entries, f"{name} has no scoped entries")
                for entry in entries:
                    with self.subTest(config=name, entry=entry):
                        self.assertEqual(entry, self.firstparty[pkg_name]["member"])
                continue
            prefix = self.prefixes[pkg_name]
            entries = [e["pattern"] for e in config.get("path_excludes", [])]
            entries += [
                c["path"] for c in config.get("license_finding_curations", [])
            ]
            self.assertTrue(entries, f"{name} has no scoped entries")
            for entry in entries:
                with self.subTest(config=name, entry=entry):
                    if prefix:
                        self.assertTrue(
                            entry.startswith(prefix),
                            f"{entry!r} is not anchored on {prefix!r}",
                        )
                    else:
                        self.assertEqual(
                            0,
                            entry.count("/"),
                            f"raw header bundle path {entry!r} must be a root file",
                        )

    def test_native_findings_use_the_verified_prefix(self):
        for finding in self.native["findings"]:
            with self.subTest(package=finding["package"], path=finding["path"]):
                prefix = self.prefixes[finding["package"]]
                self.assertTrue(finding["path"].startswith(prefix))
                self.assertIn(finding["disposition"], {"curated", "excluded"})

    def test_real_native_findings_are_covered(self):
        """Each real evaluator finding is either curated or excluded as recorded."""
        for finding in self.native["findings"]:
            pkg_name = finding["package"]
            config = find_config(self.configs, pkg_name)
            finding_shape = {
                "path": finding["path"],
                "start_lines": finding["start_lines"],
                "line_count": finding["end_lines"] - finding["start_lines"] + 1,
                "detected_license": finding["license"],
            }
            curated = any(
                curation_applies(c, finding_shape)
                for c in config.get("license_finding_curations", [])
            )
            excluded = any(
                _ant_match(e["pattern"], finding["path"])
                for e in config.get("path_excludes", [])
            )
            with self.subTest(package=pkg_name, path=finding["path"]):
                if finding["disposition"] == "curated":
                    self.assertTrue(
                        curated, f"no curation covers {finding}"
                    )
                else:
                    self.assertTrue(
                        excluded, f"no path exclude covers {finding}"
                    )

    def test_selected_native_scopes_stay_scanned(self):
        """The excludes must not swallow the code that really ships."""
        for protected in self.native["protected"]:
            pkg_name = protected["package"]
            config = find_config(self.configs, pkg_name)
            patterns = [e["pattern"] for e in config.get("path_excludes", [])]
            with self.subTest(package=pkg_name, path=protected["path"]):
                self.assertFalse(
                    any(_ant_match(p, protected["path"]) for p in patterns),
                    f"{protected['path']} would be excluded by {patterns}",
                )

    def test_poisoned_native_findings_still_alert(self):
        """A poisoned extra GPL file must not be hidden by any config."""
        poison = [
            {"path": "zstd-1.5.7/lib/legacy/new_gpl.c", "start_lines": 1,
             "line_count": 1, "detected_license": "GPL-2.0-only"},
            {"path": "bcdec-80859ed3b7afb1c527a2a99d70c61457bea72d0c.h", "start_lines": 1500, "line_count": 1,
             "detected_license": "GPL-2.0-only"},
            {"path": "brotli-1.2.0/c/enc/new_gpl.c", "start_lines": 1,
             "line_count": 1, "detected_license": "GPL-3.0-only"},
            {"path": "mbedtls-4.1.1/library/new_gpl.c", "start_lines": 1,
             "line_count": 1, "detected_license": "GPL-2.0-only"},
            {"path": "zstd-1.5.7/COPYING", "start_lines": 42, "line_count": 1,
             "detected_license": "GPL-2.0-only"},
        ]
        curations = [
            c
            for config in self.configs.values()
            for c in config.get("license_finding_curations", [])
        ]
        patterns = [
            e
            for config in self.configs.values()
            for e in config.get("path_excludes", [])
        ]
        for finding in poison:
            with self.subTest(finding=finding["path"]):
                self.assertFalse(
                    any(curation_applies(c, finding) for c in curations),
                    "a poisoned GPL finding was curated away",
                )
                self.assertFalse(
                    any(_ant_match(p["pattern"], finding["path"]) for p in patterns),
                    "a poisoned GPL finding was excluded away",
                )

    def test_brotli_excludes_only_the_contribution_guide(self):
        config = find_config(self.configs, "brotli")
        patterns = [e["pattern"] for e in config.get("path_excludes", [])]
        self.assertEqual(["brotli-1.2.0/CONTRIBUTING.md"], patterns)
        excluded = next(
            e for e in config["path_excludes"]
            if e["pattern"] == "brotli-1.2.0/CONTRIBUTING.md"
        )
        self.assertEqual("DOCUMENTATION_OF", excluded["reason"])
        # The MIT source grants and the encoded library must stay scanned.
        for path in ("brotli-1.2.0/LICENSE", "brotli-1.2.0/c/dec/decode.c",
                     "brotli-1.2.0/go/MODULE.bazel"):
            with self.subTest(path=path):
                self.assertFalse(any(_ant_match(p, path) for p in patterns))

    def test_curation_does_not_apply_to_mismatched_finding(self):
        curation = {
            "path": "bcdec-80859ed3b7afb1c527a2a99d70c61457bea72d0c.h",
            "start_lines": 1459,
            "line_count": 4,
            "detected_license": "GPL-2.0-only",
        }
        self.assertTrue(
            curation_applies(
                curation,
                {
                    "path": "bcdec-80859ed3b7afb1c527a2a99d70c61457bea72d0c.h",
                    "start_lines": 1459,
                    "line_count": 4,
                    "detected_license": "GPL-2.0-only",
                },
            )
        )
        for wrong in (
            {"path": "bcdec-80859ed3b7afb1c527a2a99d70c61457bea72d0c.h", "start_lines": 1466, "line_count": 17,
             "detected_license": "MIT"},
            {"path": "other.h", "start_lines": 1459, "line_count": 4,
             "detected_license": "GPL-2.0-only"},
            {"path": "bcdec-80859ed3b7afb1c527a2a99d70c61457bea72d0c.h", "start_lines": 1459, "line_count": 4,
             "detected_license": "MIT"},
        ):
            with self.subTest(finding=wrong):
                self.assertFalse(curation_applies(curation, wrong))

    def test_config_does_not_apply_to_a_different_source_artifact(self):
        config = {"id": "SpdxDocumentFile:vcpkg:bcdec:0.985",
                  "source_artifact_url": LOOPBACK_PREFIX + "a" * 64 + ".tar.gz"}
        self.assertTrue(
            provenance_matches(config, LOOPBACK_PREFIX + "a" * 64 + ".tar.gz")
        )
        self.assertFalse(
            provenance_matches(config, LOOPBACK_PREFIX + "b" * 64 + ".tar.gz")
        )
        self.assertFalse(provenance_matches(config, "https://example.com/bcdec.h"))

    def test_no_config_ids_share_a_package_name(self):
        names = [ID_RE.match(c["id"]).group(1) for c in self.configs.values()]
        self.assertEqual(len(names), len(set(names)))

    def test_ant_matcher_semantics(self):
        # '**' spans separators, '*' stays inside a single segment.
        self.assertTrue(_ant_match("tests/**", "tests/thirdparty/Fuzzer/a.cpp"))
        self.assertTrue(_ant_match("tests/*", "tests/README.md"))
        self.assertFalse(_ant_match("tests/*", "tests/thirdparty/a.cpp"))
        self.assertTrue(_ant_match("cmake/detect-install-dirs.cmake",
                                   "cmake/detect-install-dirs.cmake"))
        self.assertFalse(_ant_match("cmake/detect-install-dirs.cmake", "cmake/other.cmake"))
        # '?' matches exactly one non-separator character.
        self.assertTrue(_ant_match("a?c.c", "abc.c"))
        self.assertFalse(_ant_match("a?c.c", "ac.c"))
        self.assertFalse(_ant_match("a?c.c", "a/c.c"))
        # A prefix anchors the match.
        self.assertFalse(_ant_match("zstd-1.5.7/tests/**", "tests/gzip/gzip-env.sh"))

    def test_scoped_exclude_validation(self):
        self.assertFalse(_is_scoped_exclude("**"))
        self.assertFalse(_is_scoped_exclude("**/*"))
        self.assertFalse(_is_scoped_exclude("**/node_modules"))
        self.assertFalse(_is_scoped_exclude("tests/?.md"))
        self.assertFalse(_is_scoped_exclude("tests/a,b"))
        self.assertFalse(_is_scoped_exclude("*.c"))
        self.assertFalse(_is_scoped_exclude("*.h"))
        self.assertFalse(_is_scoped_exclude("*/tests"))
        self.assertTrue(_is_scoped_exclude("tests/**"))
        self.assertTrue(_is_scoped_exclude("CONTRIBUTING.md"))
        self.assertTrue(_is_scoped_exclude("tools/scripts/updateDocumentToC.py"))
        self.assertTrue(_is_scoped_exclude(".conan/**"))
        self.assertTrue(_is_scoped_exclude("Catch2-3.16.1/tools/**"))
        self.assertTrue(_is_scoped_exclude("brotli-1.2.0/CONTRIBUTING.md"))

    def test_curations_quote_the_complete_detected_expression(self):
        """A curation must match the whole raw ScanCode expression, not a token.

        The evidence table holds the raw ScanCode record (path, line range,
        raw_expression, rule id) from the 2026-10-16 adjudication. A curation
        that reduced ``GPL-2.0-only OR Apache-2.0`` or
        ``LicenseRef-scancode-proprietary-license AND Apache-2.0`` to a single
        token would not match the real finding, so each record needs an exact
        curation with the complete expression.
        """
        evidence = load_scancode_evidence()["records"]
        self.assertTrue(evidence)
        for record in evidence:
            with self.subTest(package=record["package"], rule=record["rule"]):
                config = find_config(self.configs, record["package"])
                start, end = record["start_lines"], record["end_lines"]
                matching = [
                    c
                    for c in config.get("license_finding_curations", [])
                    if c["path"] == record["path"]
                    and c["start_lines"] == start
                    and c["line_count"] == end - start + 1
                    and c["detected_license"] == record["raw_expression"]
                ]
                self.assertEqual(
                    1, len(matching), f"no curation matches raw record {record}"
                )
                self.assertNotEqual(
                    matching[0]["concluded_license"], record["raw_expression"]
                )

    def test_evidence_records_are_well_formed_raw_detections(self):
        evidence = load_scancode_evidence()["records"]
        for record in evidence:
            with self.subTest(rule=record["rule"]):
                self.assertTrue(record["path"])
                self.assertTrue(record["raw_expression"])
                self.assertGreaterEqual(record["score"], 10.0)
                self.assertIn("RULE", record["rule"])
                self.assertLessEqual(record["start_lines"], record["end_lines"])

    def test_matched_grant_lines_are_not_curated_away(self):
        """The real MIT and Unlicense grants in the bcdec footer stay uncurated."""
        config = find_config(self.configs, "bcdec")
        curated = {
            (c["start_lines"], c["detected_license"])
            for c in config["license_finding_curations"]
        }
        self.assertNotIn((1462, "MIT"), curated)
        self.assertNotIn((1485, "Unlicense"), curated)


def _ant_match(pattern, path):
    """Small AntPathMatcher subset: '**' spans separators, '*' stays in a segment."""
    regex = ""
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            regex += ".*"
            i += 2
        elif char == "*":
            regex += "[^/]*"
            i += 1
        elif char == "?":
            regex += "[^/]"
            i += 1
        else:
            regex += re.escape(char)
            i += 1
    return re.fullmatch(regex, path) is not None


def _is_scoped_exclude(pattern):
    """Return True only for a provenance-bound scope, not a blanket extension glob."""
    if pattern in BLANKET_PATTERNS or pattern.startswith("**"):
        return False
    if "?" in pattern or "," in pattern:
        return False
    if "/" not in pattern:
        # A bare entry must be an exact file name, not an extension glob.
        return "*" not in pattern
    # A scoped entry must anchor on a concrete directory, not a leading wildcard.
    return not pattern.startswith("*")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

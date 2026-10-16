"""Behavioral tests for the vcpkg/SPDX license export and preparation adapter.

The tests pin the observable contract of ``build/licenses.py`` and the two
``build/main.py`` command routes that drive it:

* ``license-inputs`` exports only the *existing* restore, never restoring,
  building or downloading, and preserves raw emitted SBOM/copyright bytes.
* ``license-prepare`` cross-checks both architectures, validates the reviewed
  inventory and writes a canonical SPDX root document, ORT config, curations,
  provenance and verified source artifacts.

Every generated dependency package must carry ``NOASSERTION`` as its
``licenseConcluded`` so ORT's source scanner is never masked by a curated
conclusion, while ``licenseDeclared`` keeps the emitted vcpkg expression
verbatim.  Reviewed OR choices travel as explicit ORT ``license_choices`` data.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import licenses  # noqa: E402
import main  # noqa: E402


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ort-samples"
JSON_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "vcpkg-json"
BCDEC_BYTES = b"bcdec single header\n"
BROTLI_BYTES = b"brotli release archive\n"
DR1_BYTES = b"dr_mp3 header\n"
DR2_BYTES = b"dr_wav header\n"
CONFIG_BYTES = b"cmake share helper\n"
COPYRIGHT_BYTES = b"installed copyright\n"
PROVENANCE = {
    "sourceCommit": "a" * 40,
    "sourceTree": "b" * 40,
    "runId": "42",
    "runAttempt": "1",
}

LOCAL_BCDEC = "https://raw.githubusercontent.com/iOrange/bcdec/abc/bcdec.h"
LOCAL_DR1 = "https://raw.githubusercontent.com/mackron/dr_libs/abc/dr_mp3.h"
LOCAL_DR2 = "https://raw.githubusercontent.com/mackron/dr_libs/abc/dr_wav.h"
BROTLI_GIT = "git+https://github.com/google/brotli@v1.2.0"
BROTLI_ARCHIVE = "https://github.com/google/brotli/archive/v1.2.0.tar.gz"
LIBSPNG_GIT = "git+https://github.com/randy408/libspng@v0.7.4"
LIBSPNG_ARCHIVE = "https://github.com/randy408/libspng/archive/v0.7.4.tar.gz"


def _sha512(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixture_json(name: str) -> dict:
    return json.loads((JSON_FIXTURES / name).read_text(encoding="utf-8"))


def _sbom(name, version, abi, expression, *, resources=(), share_files=None):
    packages = [
        {
            "name": name,
            "SPDXID": "SPDXRef-port",
            "versionInfo": version,
            "downloadLocation": "NOASSERTION",
            "licenseConcluded": expression,
            "licenseDeclared": "NOASSERTION",
        },
        {
            "name": f"{name}:triplet",
            "SPDXID": "SPDXRef-binary",
            "versionInfo": abi,
            "licenseConcluded": expression,
            "licenseDeclared": "NOASSERTION",
        },
    ]
    for index, resource in enumerate(resources):
        packages.append(
            {
                "SPDXID": f"SPDXRef-resource-{index}",
                "name": resource["name"],
                "downloadLocation": resource["url"],
                "checksums": [{"algorithm": "SHA512", "checksumValue": resource["sha512"]}],
            }
        )
    files = [
        {
            "fileName": f"./share/{name}/{relative}",
            "SPDXID": f"SPDXRef-binary-file-{index}",
            "checksums": [
                {"algorithm": "SHA256", "checksumValue": hashlib.sha256(data).hexdigest()}
            ],
        }
        for index, (relative, data) in enumerate(sorted((share_files or {}).items()))
    ]
    return {"spdxVersion": "SPDX-2.3", "packages": packages, "files": files}


def _entries(triplet):
    """The synthetic installed packages shared by both architecture trees."""

    def target(package, version, abi, expression, resources):
        return {
            "name": package,
            "version": version,
            "abi": abi,
            "triplet": triplet,
            "depends": "vcpkg-cmake:x64-windows",
            "sbom": _sbom(package, version, abi, expression, resources=resources),
        }

    return [
        target(
            "bcdec", "0.985", "b" * 64, "(MIT OR Unlicense)",
            [{"name": "bcdec.h", "url": LOCAL_BCDEC, "sha512": _sha512(BCDEC_BYTES)}],
        ),
        target(
            "brotli", "1.2.0", "c" * 64, "MIT",
            [{"name": "google/brotli", "url": BROTLI_GIT, "sha512": _sha512(BROTLI_BYTES)}],
        ),
        target(
            "drlibs", "2026-08-31", "d" * 64, "(Unlicense OR MIT-0)",
            [
                {"name": "dr_mp3.h", "url": LOCAL_DR1, "sha512": _sha512(DR1_BYTES)},
                {"name": "dr_wav.h", "url": LOCAL_DR2, "sha512": _sha512(DR2_BYTES)},
            ],
        ),
        target(
            "libspng", "0.7.4", "1" * 64, "BSD-2-Clause AND libpng-2.0",
            [{"name": "libspng", "url": LIBSPNG_GIT, "sha512": _sha512(BROTLI_BYTES)}],
        ),
        {
            "name": "vcpkg-cmake",
            "version": "2025-08-07",
            "abi": "e" * 64,
            "triplet": "x64-windows",
            "share_files": {"vcpkg_cmake_build.cmake": CONFIG_BYTES},
            "sbom": _sbom(
                "vcpkg-cmake", "2025-08-07", "e" * 64, "MIT",
                share_files={
                    "vcpkg_cmake_build.cmake": CONFIG_BYTES,
                    "copyright": COPYRIGHT_BYTES,
                },
            ),
        },
    ]


def _materialize(tree_root: Path, entries) -> None:
    """Write the emitted share tree (SBOM, copyright, helper files) for the entries."""

    for entry in entries:
        share = tree_root / entry["triplet"] / "share" / entry["name"]
        share.mkdir(parents=True, exist_ok=True)
        (share / "vcpkg.spdx.json").write_text(json.dumps(entry["sbom"]), encoding="utf-8")
        (share / "copyright").write_bytes(entry.get("copyright", COPYRIGHT_BYTES))
        for relative, data in entry.get("share_files", {}).items():
            (share / relative).write_bytes(data)


def _listing(entries) -> dict:
    """The ``vcpkg list --x-json`` map a real vcpkg run emits for these entries."""

    return {
        f"{entry['name']}:{entry['triplet']}": {
            "package_name": entry["name"],
            "triplet": entry["triplet"],
            "version": entry["version"],
            "port_version": entry.get("port_version", 0),
            "features": list(entry.get("features", [])),
            "desc": [],
        }
        for entry in entries
    }


def _info(entries) -> dict:
    """The ``vcpkg x-package-info --x-installed --x-json`` document for these entries."""

    return {
        "results": {
            f"{entry['name']}:{entry['triplet']}": {
                "version-string": entry["version"],
                "port-version": entry.get("port_version", 0),
                "triplet": entry["triplet"],
                "abi": entry["abi"],
                "dependencies": [
                    item.strip() for item in entry.get("depends", "").split(",") if item.strip()
                ],
                "owns": [],
            }
            for entry in entries
        }
    }


def _collector(entries):
    """A fake collector standing in for the external vcpkg JSON boundary."""

    listing = _listing(entries)
    info = _info(entries)

    def collect(root, repository):
        return listing, info

    return collect


OFFICIAL_ABI = {
    "bcdec": "ec47cdf5473b1c656a336af9ed94857f63f52d360532c3a7a9cd4218d103e0ab",
    "brotli": "03ea13758f92cc6b8d1529a6c3501572b4c399bbc0ef9812c0c93e0a86bcc5a1",
    "vcpkg-cmake": "95a1d1d9f69aeb6add047a4728bff6f0a96a79feb9d0fa6bd1f84bf08aa32b43",
    "vcpkg-cmake-config": "dbac67454e28ba3ac975cacb194d937cf691b37b5d3db12b73dc246f2df7eb23",
}


def _official_entries(triplet):
    """The installed set the recorded official CLI reports, with matching emitted share trees."""

    return [
        {
            "name": "bcdec", "version": "0.985", "abi": OFFICIAL_ABI["bcdec"],
            "triplet": triplet, "depends": "vcpkg-cmake:x64-windows",
            "sbom": _sbom(
                "bcdec", "0.985", OFFICIAL_ABI["bcdec"], "(MIT OR Unlicense)",
                resources=[{"name": "bcdec.h", "url": LOCAL_BCDEC,
                            "sha512": _sha512(BCDEC_BYTES)}],
            ),
        },
        {
            "name": "brotli", "version": "1.2.0", "abi": OFFICIAL_ABI["brotli"],
            "triplet": triplet,
            "depends": "vcpkg-cmake:x64-windows, vcpkg-cmake-config:x64-windows",
            "sbom": _sbom(
                "brotli", "1.2.0", OFFICIAL_ABI["brotli"], "MIT",
                resources=[{"name": "google/brotli", "url": BROTLI_GIT,
                            "sha512": _sha512(BROTLI_BYTES)}],
            ),
        },
        {
            "name": "vcpkg-cmake", "version": "2025-08-07", "abi": OFFICIAL_ABI["vcpkg-cmake"],
            "triplet": licenses.HOST_TRIPLET,
            "share_files": {"vcpkg_cmake_build.cmake": CONFIG_BYTES},
            "sbom": _sbom(
                "vcpkg-cmake", "2025-08-07", OFFICIAL_ABI["vcpkg-cmake"], "MIT",
                share_files={"vcpkg_cmake_build.cmake": CONFIG_BYTES,
                             "copyright": COPYRIGHT_BYTES},
            ),
        },
        {
            "name": "vcpkg-cmake-config", "version": "2026-07-21",
            "abi": OFFICIAL_ABI["vcpkg-cmake-config"], "triplet": licenses.HOST_TRIPLET,
            "share_files": {"vcpkg_cmake_config_fixup.cmake": CONFIG_BYTES},
            "sbom": _sbom(
                "vcpkg-cmake-config", "2026-07-21", OFFICIAL_ABI["vcpkg-cmake-config"], "MIT",
                share_files={"vcpkg_cmake_config_fixup.cmake": CONFIG_BYTES,
                             "copyright": COPYRIGHT_BYTES},
            ),
        },
    ]


SOURCE_TABLE = {
    BROTLI_ARCHIVE: BROTLI_BYTES,
    LIBSPNG_ARCHIVE: BROTLI_BYTES,
    LOCAL_BCDEC: BCDEC_BYTES,
    LOCAL_DR1: DR1_BYTES,
    LOCAL_DR2: DR2_BYTES,
}


def _fetch(url: str) -> bytes:
    return SOURCE_TABLE[url]


def _inventory_document() -> dict:
    return {
        "schema": "unpackrat-license-inventory/3",
        "rootPackage": {
            "name": "unpackrat",
            "license": "MIT",
            "notices": ["LICENSE.txt", "licenses/Observer.txt"],
        },
        "components": [
            {
                "name": "bcdec",
                "kind": "vcpkg-package",
                "scope": "runtime",
                "vcpkgName": "bcdec",
                "declaredExpression": "(MIT OR Unlicense)",
                "features": [],
                "licenseChoice": {
                    "given": "(MIT OR Unlicense)",
                    "choice": "MIT",
                    "comment": "MIT selected; Unlicense preserved.",
                },
                "source": {"origin": "overlay", "identity": "github.com/iorange/bcdec"},
                "notices": ["licenses/bcdec.txt"],
            },
            {
                "name": "brotli",
                "kind": "vcpkg-package",
                "scope": "runtime",
                "vcpkgName": "brotli",
                "declaredExpression": "MIT",
                "features": [],
                "source": {"origin": "registry", "identity": "github.com/google/brotli"},
                "notices": [],
            },
            {
                "name": "drlibs",
                "kind": "vcpkg-package",
                "scope": "runtime",
                "vcpkgName": "drlibs",
                "declaredExpression": "(Unlicense OR MIT-0)",
                "features": [],
                "licenseChoice": {
                    "given": "(Unlicense OR MIT-0)",
                    "choice": "Unlicense",
                    "comment": "Unlicense selected; MIT-0 preserved.",
                },
                "source": {"origin": "overlay", "identity": "github.com/mackron/dr_libs"},
                "notices": [],
            },
            {
                "name": "libspng",
                "kind": "vcpkg-package",
                "scope": "runtime",
                "vcpkgName": "libspng",
                "declaredExpression": "BSD-2-Clause AND libpng-2.0",
                "features": [],
                "source": {"origin": "registry", "identity": "github.com/randy408/libspng"},
                "notices": [],
            },
            {
                "name": "vcpkg-cmake",
                "kind": "vcpkg-package",
                "scope": "build",
                "vcpkgName": "vcpkg-cmake",
                "declaredExpression": "MIT",
                "features": [],
                "source": {"origin": "registry"},
                "notices": [],
            },
            {
                "name": "observer-api",
                "kind": "adopted-source",
                "scope": "runtime",
                "vcpkgName": None,
                "declaredExpression": "LGPL-3.0-only",
                "source": {"origin": "reference"},
                "notices": ["licenses/Observer.txt"],
            },
            "junk",
        ],
    }


class InstalledListing(unittest.TestCase):
    """The ``vcpkg list --x-json`` boundary facts."""

    def test_reads_the_recorded_listing(self) -> None:
        facts = licenses.parse_installed_list(_fixture_json("vcpkg-installed-list.json"))
        self.assertEqual(
            {(fact.name, fact.triplet) for fact in facts},
            {
                ("bcdec", "unpackrat-x64-windows-static"),
                ("brotli", "unpackrat-x64-windows-static"),
                ("vcpkg-cmake-config", "x64-windows"),
                ("vcpkg-cmake", "x64-windows"),
            },
        )
        by_name = {fact.name: fact for fact in facts}
        self.assertEqual(by_name["vcpkg-cmake"].version, "2025-08-07")
        self.assertEqual(by_name["bcdec"].features, ())
        self.assertEqual(by_name["bcdec"].port_version, 0)

    def test_features_are_preserved_verbatim(self) -> None:
        facts = licenses.parse_installed_list(
            {"x:t": {"package_name": "x", "triplet": "t", "version": "1",
                     "features": ["xxhsum"]}}
        )
        self.assertEqual(facts[0].features, ("xxhsum",))

    def test_rejects_a_non_object_document(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed_list([])

    def test_rejects_a_non_object_entry(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed_list({"x:t": "junk"})

    def test_rejects_a_missing_field(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed_list({"x:t": {"package_name": "x", "triplet": "t"}})

    def test_rejects_a_malformed_features_list(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed_list(
                {"x:t": {"package_name": "x", "triplet": "t", "version": "1",
                         "features": "xxhsum"}}
            )

    def test_rejects_a_malformed_port_version(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed_list(
                {"x:t": {"package_name": "x", "triplet": "t", "version": "1",
                         "port_version": "0"}}
            )


class InstalledInfo(unittest.TestCase):
    """The ``vcpkg x-package-info --x-installed --x-json`` boundary facts."""

    def test_reads_the_recorded_package_info(self) -> None:
        infos = licenses.parse_installed_info(_fixture_json("vcpkg-installed-info.json"))
        brotli = infos["brotli:unpackrat-x64-windows-static"]
        self.assertEqual(
            brotli.abi,
            "03ea13758f92cc6b8d1529a6c3501572b4c399bbc0ef9812c0c93e0a86bcc5a1",
        )
        self.assertEqual(
            brotli.dependencies,
            ("vcpkg-cmake:x64-windows", "vcpkg-cmake-config:x64-windows"),
        )
        self.assertEqual(infos["vcpkg-cmake:x64-windows"].dependencies, ())

    def test_rejects_a_non_object_document(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed_info([])

    def test_rejects_a_missing_results_object(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed_info({})

    def test_rejects_a_non_object_entry(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed_info({"results": {"x:t": "junk"}})

    def test_rejects_a_missing_abi(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed_info({"results": {"x:t": {"triplet": "t"}}})

    def test_rejects_a_malformed_dependencies_list(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed_info(
                {"results": {"x:t": {"triplet": "t", "abi": "a", "dependencies": "z"}}}
            )


class InstalledJoin(unittest.TestCase):
    """The two JSON documents are joined strictly; no node is dropped or invented."""

    def test_joins_the_recorded_documents(self) -> None:
        packages = licenses.parse_installed(
            _fixture_json("vcpkg-installed-list.json"),
            _fixture_json("vcpkg-installed-info.json"),
        )
        by_name = {package.name: package for package in packages}
        self.assertEqual(by_name["bcdec"].depends, ("vcpkg-cmake:x64-windows",))
        self.assertEqual(
            by_name["brotli"].abi,
            "03ea13758f92cc6b8d1529a6c3501572b4c399bbc0ef9812c0c93e0a86bcc5a1",
        )
        self.assertEqual(by_name["vcpkg-cmake-config"].version, "2026-07-21")

    def test_rejects_an_omitted_node(self) -> None:
        info = _fixture_json("vcpkg-installed-info.json")
        del info["results"]["bcdec:unpackrat-x64-windows-static"]
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed(_fixture_json("vcpkg-installed-list.json"), info)

    def test_rejects_an_unlisted_node(self) -> None:
        info = _fixture_json("vcpkg-installed-info.json")
        info["results"]["ghost:x64-windows"] = {"triplet": "x64-windows", "abi": "a" * 64}
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed(_fixture_json("vcpkg-installed-list.json"), info)

    def test_rejects_a_triplet_disagreement(self) -> None:
        info = _fixture_json("vcpkg-installed-info.json")
        info["results"]["bcdec:unpackrat-x64-windows-static"]["triplet"] = "x64-windows"
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed(_fixture_json("vcpkg-installed-list.json"), info)


class Selection(unittest.TestCase):
    def _packages(self):
        return licenses.parse_installed(
            _fixture_json("vcpkg-installed-list.json"),
            _fixture_json("vcpkg-installed-info.json"),
        )

    def test_selects_target_and_host_packages(self) -> None:
        selected = licenses.select_installed(self._packages(), "x64")
        self.assertEqual(
            {package.name for package in selected},
            {"bcdec", "brotli", "vcpkg-cmake", "vcpkg-cmake-config"},
        )

    def test_rejects_a_foreign_triplet_tree(self) -> None:
        packages = self._packages() + (
            licenses.InstalledPackage(
                "zlib", "observer-x64-windows-static", "1", 0, (), "a" * 64, ()
            ),
        )
        with self.assertRaises(licenses.LicenseError):
            licenses.select_installed(packages, "x64")

    def test_rejects_an_empty_selection(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.select_installed((), "x64")

    def test_rejects_an_unresolved_dependency(self) -> None:
        packages = (
            licenses.InstalledPackage(
                "zlib", "unpackrat-x64-windows-static", "1", 0, (), "a" * 64,
                ("ghost:x64-windows",),
            ),
        )
        with self.assertRaises(licenses.LicenseError):
            licenses.select_installed(packages, "x64")

    def test_resolves_a_untripleted_dependency(self) -> None:
        packages = (
            licenses.InstalledPackage(
                "zlib", "unpackrat-x64-windows-static", "1", 0, (), "a" * 64, ("helper",)
            ),
            licenses.InstalledPackage(
                "helper", "unpackrat-x64-windows-static", "1", 0, (), "b" * 64, ()
            ),
        )
        self.assertEqual(len(licenses.select_installed(packages, "x64")), 2)

    def test_full_version_keeps_a_nonzero_port_version(self) -> None:
        package = licenses.InstalledPackage("x", "t", "1.0", 2, (), "a", ())
        self.assertEqual(package.full_version, "1.0#2")


class SbomParsing(unittest.TestCase):
    def _fixture(self, name: str) -> dict:
        return json.loads((FIXTURES / name / "vcpkg.spdx.json").read_text(encoding="utf-8"))

    def test_reads_real_single_header_sample(self) -> None:
        sbom = licenses.parse_sbom(self._fixture("bcdec"))
        self.assertEqual(sbom.port, "bcdec")
        self.assertEqual(sbom.version, "0.985")
        self.assertEqual(
            sbom.abi, "ec47cdf5473b1c656a336af9ed94857f63f52d360532c3a7a9cd4218d103e0ab"
        )
        self.assertEqual(sbom.expression, "(MIT OR Unlicense)")
        self.assertEqual(
            sbom.purl, "pkg:vcpkg/bcdec@0.985?triplet=unpackrat-x64-windows-static"
        )
        self.assertEqual(
            [resource.url for resource in sbom.resources],
            ["https://raw.githubusercontent.com/iOrange/bcdec/"
             "80859ed3b7afb1c527a2a99d70c61457bea72d0c/bcdec.h"],
        )

    def test_reads_real_archive_sample(self) -> None:
        sbom = licenses.parse_sbom(self._fixture("brotli"))
        self.assertEqual(sbom.expression, "MIT")
        self.assertEqual(sbom.resources[0].url, BROTLI_GIT)

    def test_reads_real_host_helper_sample(self) -> None:
        sbom = licenses.parse_sbom(self._fixture("vcpkg-cmake"))
        self.assertEqual(sbom.expression, "MIT")
        self.assertEqual(sbom.resources, ())
        self.assertTrue(
            any(
                str(entry.get("fileName", "")).startswith("./share/vcpkg-cmake/")
                for entry in sbom.files
            )
        )

    def test_rejects_wrong_spdx_version(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_sbom({"spdxVersion": "SPDX-2.2"})

    def test_rejects_missing_packages(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_sbom({"spdxVersion": "SPDX-2.3"})

    def test_rejects_missing_port_or_binary(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_sbom(
                {"spdxVersion": "SPDX-2.3", "packages": [{"SPDXID": "SPDXRef-port"}]}
            )

    def test_rejects_resource_without_sha512_and_junk_entries(self) -> None:
        document = {
            "spdxVersion": "SPDX-2.3",
            "packages": [
                {"SPDXID": "SPDXRef-port", "name": "x", "versionInfo": "1"},
                {"SPDXID": "SPDXRef-binary", "name": "x:t", "versionInfo": "a"},
                "junk",
                {
                    "SPDXID": "SPDXRef-resource-0",
                    "name": "r",
                    "checksums": [{"algorithm": "MD5", "checksumValue": "z"}],
                },
            ],
            "files": [{"fileName": "./x"}, "junk"],
        }
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_sbom(document)

    def test_resource_without_external_refs_has_no_purl(self) -> None:
        sbom = licenses.parse_sbom(_sbom("bcdec", "1", "a" * 64, "MIT"))
        self.assertEqual(sbom.purl, "")
        self.assertEqual(sbom.resources, ())


class UrlResolution(unittest.TestCase):
    def test_github_git_resource_maps_to_archive(self) -> None:
        self.assertEqual(
            licenses.github_archive_url(BROTLI_GIT), BROTLI_ARCHIVE
        )

    def test_dot_git_suffix_is_stripped(self) -> None:
        self.assertEqual(
            licenses.github_archive_url("git+https://github.com/o/r.git@main"),
            "https://github.com/o/r/archive/main.tar.gz",
        )

    def test_non_github_resource_is_left_unchanged(self) -> None:
        self.assertIsNone(licenses.github_archive_url("https://example.com/x.h"))
        self.assertEqual(
            licenses.source_location(licenses.Resource("x", "https://example.com/x.h", "z")),
            "https://example.com/x.h",
        )

    def test_archive_resource_resolves_to_archive_url(self) -> None:
        resource = licenses.Resource("google/brotli", BROTLI_GIT, "z")
        self.assertEqual(licenses.source_location(resource), BROTLI_ARCHIVE)


class GitProvenance(unittest.TestCase):
    def test_success_reads_commit_and_tree(self) -> None:
        completed = mock.Mock(stdout="cafe\n")
        with mock.patch.object(licenses.subprocess, "run", return_value=completed):
            provenance = licenses.git_provenance("repo")
        self.assertEqual(provenance["sourceCommit"], "cafe")
        self.assertEqual(provenance["sourceTree"], "cafe")

    def test_missing_git_binary_is_reported(self) -> None:
        with mock.patch.object(licenses.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(licenses.LicenseError):
                licenses.git_provenance("repo")

    def test_failing_git_command_is_reported(self) -> None:
        import subprocess as real_subprocess

        error = real_subprocess.CalledProcessError(1, "git", stderr="boom")
        with mock.patch.object(licenses.subprocess, "run", side_effect=error):
            with self.assertRaises(licenses.LicenseError):
                licenses.git_provenance("repo")


class _PipelineBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / "licenses").mkdir(parents=True)
        (self.repo / "LICENSE.txt").write_bytes(b"MIT project text\n")
        (self.repo / "licenses" / "Observer.txt").write_bytes(b"LGPL\n")
        (self.repo / "licenses" / "bcdec.txt").write_bytes(b"bcdec notice\n")
        self.inventory_path = Path(self._tmp.name) / "inventory.json"
        self.inventory_path.write_text(json.dumps(_inventory_document()), encoding="utf-8")
        # Every export must be backed by a successful Release attestation, and the reviewed build
        # inputs and checkout identity are the deterministic boundaries the guard and attestation
        # read.  The fixed provenance and tracked build-input list stand in for git and the driver.
        (self.repo / "vcpkg.json").write_text('{"name": "unpackrat"}\n', encoding="utf-8")
        self._real_git_tracked = licenses._git_tracked
        self.enterContext(
            mock.patch.object(licenses, "_git_tracked", return_value=("vcpkg.json",))
        )
        self.enterContext(mock.patch.object(licenses, "git_provenance", return_value=PROVENANCE))
        for architecture in licenses.ARCHITECTURES:
            licenses.write_attestation(self.repo, architecture)

    def _export(self, architecture: str, inputs: Path | None = None) -> Path:
        inputs = inputs if inputs is not None else Path(self._tmp.name) / "inputs"
        existing = inputs / architecture / "installed.json"
        if existing.is_file():
            return inputs / architecture
        root = licenses.vcpkg_root(self.repo, architecture)
        entries = _entries(licenses.target_triplet(architecture))
        _materialize(root, entries)
        return licenses.export_inputs(
            self.repo, architecture, inputs / architecture,
            collect=_collector(entries),
        )

    def _prepare(self, fetch=None, name: str = "prepared") -> tuple[Path, dict]:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        output = Path(self._tmp.name) / name
        licenses.prepare(
            Path(self._tmp.name) / "inputs", output, inventory_path=self.inventory_path,
            repository=self.repo, fetch=fetch if fetch is not None else _fetch,
        )
        document = json.loads((output / "unpackrat.spdx.json").read_text(encoding="utf-8"))
        return output, document

    def _packages(self, document: dict) -> dict[str, dict]:
        return {package["name"]: package for package in document["packages"]}

    def _rewrite(self, architecture: str, name: str, **fields) -> None:
        manifest = Path(self._tmp.name) / "inputs" / architecture / "installed.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        for package in document["packages"]:
            if package["name"] == name:
                package.update(fields)
        manifest.write_text(json.dumps(document), encoding="utf-8")

    def _rewrite_both(self, name: str, **fields) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._rewrite(architecture, name, **fields)

    def _append_both(self, fields: dict) -> None:
        for architecture in licenses.ARCHITECTURES:
            manifest = Path(self._tmp.name) / "inputs" / architecture / "installed.json"
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["packages"] = document["packages"] + [fields]
            manifest.write_text(json.dumps(document), encoding="utf-8")

    def _export_with(self, entries) -> Path:
        _materialize(licenses.vcpkg_root(self.repo, "x64"), entries)
        return licenses.export_inputs(
            self.repo, "x64", Path(self._tmp.name) / "single",
            collect=_collector(entries),
        )

    def _rewrite_export_both(self, name: str, mutate) -> None:
        """Rewrite the authoritative raw SBOM and keep the manifest facts consistent with it."""

        for architecture in licenses.ARCHITECTURES:
            manifest_path = Path(self._tmp.name) / "inputs" / architecture / "installed.json"
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            for package in document["packages"]:
                if package["name"] != name:
                    continue
                sbom_path = manifest_path.parent / package["sbom"]["path"]
                raw = json.loads(sbom_path.read_text(encoding="utf-8"))
                mutate(raw, package)
                sbom_path.write_text(json.dumps(raw), encoding="utf-8")
                package["sbom"]["sha256"] = _sha256(sbom_path.read_bytes())
            manifest_path.write_text(json.dumps(document), encoding="utf-8")


class Pipeline(_PipelineBase):
    def test_export_inputs_writes_raw_bytes_and_metadata(self) -> None:
        output = self._export("x64")
        document = json.loads((output / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], "unpackrat-license-inputs/1")
        self.assertEqual(document["targetTriplet"], "unpackrat-x64-windows-static")
        self.assertEqual(document["provenance"], PROVENANCE)
        self.assertEqual(
            {package["name"] for package in document["packages"]},
            {"bcdec", "brotli", "drlibs", "libspng", "vcpkg-cmake"},
        )
        self.assertTrue((output / "sbom" / "bcdec.spdx.json").is_file())
        self.assertEqual((output / "copyright" / "bcdec.txt").read_bytes(), COPYRIGHT_BYTES)

    def test_export_rejects_unknown_architecture(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.export_inputs(
                self.repo, "arm64", Path(self._tmp.name) / "x", collect=_collector([])
            )

    def test_export_rejects_an_empty_installed_set(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.export_inputs(
                self.repo, "x64", Path(self._tmp.name) / "m",
                collect=_collector([]),
            )

    def test_export_rejects_a_malformed_cli_schema(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.export_inputs(
                self.repo, "x64", Path(self._tmp.name) / "m",
                collect=lambda root, repository: ([], {}),
            )

    def test_export_refuses_non_empty_output(self) -> None:
        entries = _entries(licenses.target_triplet("x64"))
        _materialize(licenses.vcpkg_root(self.repo, "x64"), entries)
        output = Path(self._tmp.name) / "reuse"
        output.mkdir()
        (output / "stale.txt").write_text("old\n", encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            licenses.export_inputs(
                self.repo, "x64", output, collect=_collector(entries)
            )

    def test_export_accepts_existing_empty_output(self) -> None:
        entries = _entries(licenses.target_triplet("x64"))
        _materialize(licenses.vcpkg_root(self.repo, "x64"), entries)
        output = Path(self._tmp.name) / "empty"
        output.mkdir()
        result = licenses.export_inputs(
            self.repo, "x64", output, collect=_collector(entries)
        )
        self.assertTrue((result / "installed.json").is_file())

    def test_export_rejects_sbom_port_mismatch(self) -> None:
        entries = _entries(licenses.target_triplet("x64"))
        entries[0]["sbom"]["packages"][0]["name"] = "other"
        with self.assertRaises(licenses.LicenseError):
            self._export_with(entries)

    def test_export_rejects_sbom_version_mismatch(self) -> None:
        entries = _entries(licenses.target_triplet("x64"))
        entries[0]["sbom"]["packages"][0]["versionInfo"] = "9.9"
        with self.assertRaises(licenses.LicenseError):
            self._export_with(entries)

    def test_export_rejects_sbom_abi_mismatch(self) -> None:
        entries = _entries(licenses.target_triplet("x64"))
        entries[0]["sbom"]["packages"][1]["versionInfo"] = "0" * 64
        with self.assertRaises(licenses.LicenseError):
            self._export_with(entries)

    def test_export_rejects_host_helper_share_checksum_mismatch(self) -> None:
        entries = _entries(licenses.target_triplet("x64"))
        entries[-1]["sbom"]["files"][0]["checksums"][0]["checksumValue"] = "0" * 64
        with self.assertRaises(licenses.LicenseError):
            self._export_with(entries)

    def test_export_rejects_host_helper_file_without_sha256(self) -> None:
        entries = _entries(licenses.target_triplet("x64"))
        entries[-1]["sbom"]["files"][0]["checksums"] = [
            {"algorithm": "MD5", "checksumValue": "z"}
        ]
        with self.assertRaises(licenses.LicenseError):
            self._export_with(entries)

    def test_export_ignores_unrelated_share_file_entries(self) -> None:
        entries = _entries(licenses.target_triplet("x64"))
        entries[-1]["sbom"]["files"].append({
            "fileName": "./lib/vcpkg_cmake_build.cmake",
            "checksums": [{"algorithm": "SHA256", "checksumValue": "a" * 64}],
        })
        output = self._export_with(entries)
        document = json.loads((output / "installed.json").read_text(encoding="utf-8"))
        helper = next(p for p in document["packages"] if p["name"] == "vcpkg-cmake")
        self.assertEqual(
            [entry["path"] for entry in helper["shareFiles"]],
            ["copyright", "vcpkg_cmake_build.cmake"],
        )

    def test_prepare_concludes_noassertion_and_preserves_declared(self) -> None:
        output, document = self._prepare()
        packages = self._packages(document)
        self.assertEqual(packages["unpackrat"]["licenseConcluded"], "NOASSERTION")
        self.assertEqual(packages["unpackrat"]["licenseDeclared"], "MIT")
        self.assertEqual(packages["unpackrat"]["packageFileName"], "")
        self.assertEqual(packages["bcdec"]["licenseConcluded"], "NOASSERTION")
        self.assertEqual(packages["bcdec"]["licenseDeclared"], "(MIT OR Unlicense)")
        self.assertEqual(packages["bcdec"]["supplier"], "Organization: vcpkg")
        self.assertEqual(
            packages["bcdec"]["packageFileName"],
            _sha256(licenses.deterministic_tar({"bcdec.h": BCDEC_BYTES})) + ".tar",
        )
        self.assertEqual(
            packages["libspng"]["licenseDeclared"], "BSD-2-Clause AND libpng-2.0"
        )
        self.assertFalse((output / "package-curations.yml").read_text(encoding="utf-8").strip() == "")

    def test_prepare_emits_license_choices_and_curations(self) -> None:
        output, _ = self._prepare()
        config = json.loads((output / ".ort.yml").read_text(encoding="utf-8"))
        by_id = {
            choice["package_id"]: choice["license_choices"]
            for choice in config["license_choices"]["package_license_choices"]
        }
        self.assertEqual(set(by_id), {
            "SpdxDocumentFile:vcpkg:bcdec:0.985",
            "SpdxDocumentFile:vcpkg:drlibs:2026-08-31",
        })
        self.assertEqual(
            by_id["SpdxDocumentFile:vcpkg:bcdec:0.985"],
            [{"given": "(MIT OR Unlicense)", "choice": "MIT"}],
        )
        curations = json.loads((output / "package-curations.yml").read_text(encoding="utf-8"))
        self.assertEqual({curation["id"] for curation in curations}, {
            "SpdxDocumentFile:vcpkg:bcdec:0.985",
            "SpdxDocumentFile:vcpkg:brotli:1.2.0",
            "SpdxDocumentFile:vcpkg:drlibs:2026-08-31",
            "SpdxDocumentFile:vcpkg:libspng:0.7.4",
            "SpdxDocumentFile:vcpkg:vcpkg-cmake:2025-08-07",
        })
        self.assertTrue(all(
            curation["curations"]["source_artifact"]["url"].startswith(
                "http://127.0.0.1:8765/sources/"
            )
            for curation in curations
        ))

    def test_prepare_writes_verified_sources_provenance_and_notices(self) -> None:
        output, _ = self._prepare()
        self.assertEqual(
            (output / "sources" / f"{_sha256(BROTLI_BYTES)}.tar.gz").read_bytes(), BROTLI_BYTES
        )
        provenance = json.loads((output / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(provenance["schema"], "unpackrat-license-prepared/1")
        self.assertEqual(provenance["provenance"], PROVENANCE)
        self.assertEqual(
            sorted(path.name for path in (output / "notices").iterdir()),
            ["LICENSE.txt", "Observer.txt", "bcdec.txt"],
        )
        self.assertTrue((output / "raw" / "x86" / "sbom" / "bcdec.spdx.json").is_file())

    def test_prepare_sources_are_deterministic(self) -> None:
        first, _ = self._prepare()
        second, _ = self._prepare(name="prepared-again")
        for artifact in (first / "sources").iterdir():
            self.assertEqual(
                artifact.read_bytes(), (second / "sources" / artifact.name).read_bytes()
            )

    def test_deterministic_tar_is_stable(self) -> None:
        self.assertEqual(
            licenses.deterministic_tar({"b": b"2", "a": b"1"}),
            licenses.deterministic_tar({"a": b"1", "b": b"2"}),
        )

    def test_prepare_rejects_cross_run_exports(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        manifest = Path(self._tmp.name) / "inputs" / "x64" / "installed.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["provenance"] = dict(PROVENANCE, runId="99")
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_rejects_both_exports_from_a_foreign_run(self) -> None:
        # Both exports agreeing with each other is not enough: they must agree with the *current*
        # checkout and CI run, so a whole run's exports copied onto a later run still fail.
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
            manifest = Path(self._tmp.name) / "inputs" / architecture / "installed.json"
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["provenance"] = dict(PROVENANCE, runId="99", runAttempt="7")
            manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_rejects_selection_difference(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        manifest = Path(self._tmp.name) / "inputs" / "x86" / "installed.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["packages"] = [
            package for package in document["packages"] if package["name"] != "bcdec"
        ]
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_rejects_architecture_parity_break(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self._rewrite("x64", "bcdec", declaredExpression="(MIT OR Unlicense) ")
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_rejects_absent_inventory_component(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        document = _inventory_document()
        document["components"] = [
            component for component in document["components"]
            if not (isinstance(component, dict) and component.get("name") == "brotli")
        ]
        inventory = Path(self._tmp.name) / "reduced.json"
        inventory.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=inventory, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_rejects_changed_license(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)

        def mutate(raw, package):
            for entry in raw["packages"]:
                if entry["SPDXID"] == "SPDXRef-port":
                    entry["licenseConcluded"] = "BSD-3-Clause"
            package["declaredExpression"] = "BSD-3-Clause"

        self._rewrite_export_both("brotli", mutate)
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_rejects_reference_component_installed(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        document = _inventory_document()
        for component in document["components"]:
            if isinstance(component, dict) and component.get("name") == "brotli":
                component["source"]["origin"] = "reference"
        inventory = Path(self._tmp.name) / "reference.json"
        inventory.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=inventory, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_rejects_uninstalled_inventory_package(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        document = _inventory_document()
        document["components"].append({
            "name": "extra", "kind": "vcpkg-package", "scope": "runtime",
            "vcpkgName": "extra", "declaredExpression": "MIT",
            "source": {"origin": "registry"}, "notices": [],
        })
        inventory = Path(self._tmp.name) / "extra.json"
        inventory.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=inventory, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_rejects_non_empty_output(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        output = Path(self._tmp.name) / "prepared"
        output.mkdir()
        (output / "old.txt").write_text("old\n", encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", output,
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_rejects_an_installed_package_absent_from_inventory(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self._append_both({
            "name": "surprise", "version": "1.0", "kind": "target", "features": [],
            "declaredExpression": "MIT", "resources": [], "shareFiles": [], "depends": [],
        })
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_rejects_an_unapproved_feature(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self._rewrite_both("brotli", features=["xxhsum"])
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_rejects_a_feature_parity_break(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self._rewrite("x64", "bcdec", features=["gpl"])
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_accepts_an_approved_feature(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self._rewrite_both("brotli", features=["core"])
        inventory = _inventory_document()
        for component in inventory["components"]:
            if isinstance(component, dict) and component.get("name") == "brotli":
                component["features"] = ["core"]
        path = Path(self._tmp.name) / "feature.json"
        path.write_text(json.dumps(inventory), encoding="utf-8")
        output = Path(self._tmp.name) / "feature-prepared"
        licenses.prepare(
            Path(self._tmp.name) / "inputs", output,
            inventory_path=path, repository=self.repo, fetch=_fetch,
        )
        self.assertTrue((output / "unpackrat.spdx.json").is_file())

    def _noassertion_case(self, correction) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)

        def mutate(raw, package):
            for entry in raw["packages"]:
                if entry["SPDXID"] == "SPDXRef-port":
                    entry["licenseConcluded"] = "NOASSERTION"
            package["declaredExpression"] = "NOASSERTION"

        self._rewrite_export_both("brotli", mutate)
        inventory = _inventory_document()
        for component in inventory["components"]:
            if isinstance(component, dict) and component.get("name") == "brotli":
                component["declaredExpression"] = "NOASSERTION"
                if correction is not None:
                    component["correction"] = correction
        path = Path(self._tmp.name) / "corrected.json"
        path.write_text(json.dumps(inventory), encoding="utf-8")
        licenses.prepare(
            Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
            inventory_path=path, repository=self.repo, fetch=_fetch,
        )
        document = json.loads(
            (Path(self._tmp.name) / "prepared" / "unpackrat.spdx.json").read_text(
                encoding="utf-8"
            )
        )
        return self._packages(document)["brotli"]

    def test_prepare_applies_scoped_metadata_correction(self) -> None:
        package = self._noassertion_case(
            {"version": "1.2.0", "sha512": _sha512(BROTLI_BYTES), "license": "MIT"}
        )
        self.assertEqual(package["licenseDeclared"], "MIT")
        self.assertEqual(package["licenseConcluded"], "NOASSERTION")

    def test_prepare_rejects_noassertion_without_correction(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            self._noassertion_case(None)

    def test_prepare_rejects_correction_version_mismatch(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            self._noassertion_case({"version": "9.9", "sha512": _sha512(BROTLI_BYTES)})

    def test_prepare_rejects_correction_hash_mismatch(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            self._noassertion_case({"version": "1.2.0", "sha512": "0" * 128})

    def test_prepare_rejects_source_checksum_mismatch(self) -> None:
        def tampered(url: str) -> bytes:
            return b"tampered" if url == BROTLI_ARCHIVE else _fetch(url)

        with self.assertRaises(licenses.LicenseError):
            self._prepare(fetch=tampered)

    def test_prepare_rejects_multi_resource_checksum_mismatch(self) -> None:
        def tampered(url: str) -> bytes:
            return b"tampered" if url == LOCAL_DR2 else _fetch(url)

        with self.assertRaises(licenses.LicenseError):
            self._prepare(fetch=tampered)

    def test_prepare_rejects_component_without_source(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self._rewrite_both("brotli", resources=[])
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_rejects_tampered_share_checksum(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        manifest = Path(self._tmp.name) / "inputs" / "x86" / "installed.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        for package in document["packages"]:
            if package["name"] == "vcpkg-cmake":
                package["shareFiles"][0]["sha256"] = "0" * 64
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )


class OrtConfigMerging(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self.output = Path(self._tmp.name) / "out"
        self.output.mkdir()
        self.components = [
            {"name": "bcdec", "version": "0.985",
             "licenseChoice": {"given": "A", "choice": "B"}},
            {"name": "brotli", "version": "1.2.0", "licenseChoice": None},
        ]

    def _write(self) -> dict:
        licenses._write_ort_config(self.components, self.repo, self.output)
        return json.loads((self.output / ".ort.yml").read_text(encoding="utf-8"))

    def test_absent_base_creates_choices(self) -> None:
        document = self._write()
        self.assertEqual(
            document["license_choices"]["package_license_choices"],
            [{"package_id": "SpdxDocumentFile:vcpkg:bcdec:0.985",
              "license_choices": [{"given": "A", "choice": "B"}]}],
        )

    def test_base_without_license_choices_is_preserved(self) -> None:
        (self.repo / ".ort.yml").write_text(
            json.dumps({"excludes": {"paths": ["x"]}}), encoding="utf-8"
        )
        document = self._write()
        self.assertEqual(document["excludes"], {"paths": ["x"]})
        self.assertEqual(len(document["license_choices"]["package_license_choices"]), 1)

    def test_existing_choices_are_merged(self) -> None:
        (self.repo / ".ort.yml").write_text(
            json.dumps({"license_choices": {"package_license_choices": [{"package_id": "keep"}]}}),
            encoding="utf-8",
        )
        document = self._write()
        self.assertEqual(
            document["license_choices"]["package_license_choices"],
            [{"package_id": "keep"},
             {"package_id": "SpdxDocumentFile:vcpkg:bcdec:0.985",
              "license_choices": [{"given": "A", "choice": "B"}]}],
        )

    def test_non_list_existing_choices_are_replaced(self) -> None:
        (self.repo / ".ort.yml").write_text(
            json.dumps({"license_choices": {"package_license_choices": "bad"}}),
            encoding="utf-8",
        )
        document = self._write()
        self.assertEqual(len(document["license_choices"]["package_license_choices"]), 1)

    def test_non_mapping_section_is_reset(self) -> None:
        (self.repo / ".ort.yml").write_text(
            json.dumps({"license_choices": "bad"}), encoding="utf-8"
        )
        document = self._write()
        self.assertEqual(len(document["license_choices"]["package_license_choices"]), 1)

    def test_non_mapping_base_is_rejected(self) -> None:
        (self.repo / ".ort.yml").write_text(json.dumps(["x"]), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            self._write()


class InventoryValidation(unittest.TestCase):
    def test_rejects_wrong_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inventory.json"
            path.write_text(json.dumps({"schema": "nope"}), encoding="utf-8")
            with self.assertRaises(licenses.LicenseError):
                licenses.load_inventory(path)

    def test_rejects_missing_components(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inventory.json"
            path.write_text(
                json.dumps({"schema": "unpackrat-license-inventory/3"}), encoding="utf-8"
            )
            with self.assertRaises(licenses.LicenseError):
                licenses.load_inventory(path)

    def test_missing_file_is_reported(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.load_inventory("does-not-exist.json")

    def test_invalid_json_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inventory.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(licenses.LicenseError):
                licenses.load_inventory(path)

    def test_wrong_export_schema_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installed.json"
            path.write_text(json.dumps({"schema": "other"}), encoding="utf-8")
            with self.assertRaises(licenses.LicenseError):
                licenses.load_export(Path(tmp))


class DefaultFetch(unittest.TestCase):
    def test_default_fetch_reads_the_response(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b"payload"
        response.__enter__.return_value = response
        with mock.patch.object(licenses.urllib.request, "urlopen", return_value=response):
            self.assertEqual(licenses.default_fetch("https://example.com/x"), b"payload")


class VcpkgCollector(unittest.TestCase):
    """The vcpkg CLI is the external boundary; only its JSON is reused."""

    def test_runs_list_then_package_info_for_every_spec(self) -> None:
        listing = json.dumps(_fixture_json("vcpkg-installed-list.json"))
        info = json.dumps(_fixture_json("vcpkg-installed-info.json"))
        completed = [mock.Mock(stdout=listing), mock.Mock(stdout=info)]
        with mock.patch.object(licenses.subprocess, "run", side_effect=completed) as run:
            listing_doc, info_doc = licenses.vcpkg_collector(Path("vcpkg.exe"))(
                Path("root"), Path("repo")
            )
        self.assertEqual(listing_doc["vcpkg-cmake:x64-windows"]["version"], "2025-08-07")
        self.assertEqual(
            info_doc["results"]["bcdec:unpackrat-x64-windows-static"]["abi"],
            OFFICIAL_ABI["bcdec"],
        )
        list_call = run.call_args_list[0].args[0]
        self.assertEqual(list_call[1], "list")
        self.assertIn("--x-install-root=root", list_call)
        info_call = run.call_args_list[1].args[0]
        self.assertEqual(info_call[1], "x-package-info")
        self.assertIn("--x-transitive", info_call)
        self.assertIn("bcdec:unpackrat-x64-windows-static", info_call)

    def test_non_object_listing_queries_no_specs(self) -> None:
        completed = [mock.Mock(stdout="[]"), mock.Mock(stdout='{"results": {}}')]
        with mock.patch.object(licenses.subprocess, "run", side_effect=completed) as run:
            listing, info = licenses.vcpkg_collector("vcpkg")(Path("root"), Path("repo"))
        self.assertEqual(listing, [])
        self.assertEqual(info, {"results": {}})
        self.assertEqual(run.call_args_list[1].args[0][-1], "--x-install-root=root")

    def test_reports_a_missing_vcpkg_binary(self) -> None:
        with mock.patch.object(licenses.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(licenses.LicenseError):
                licenses._vcpkg_json(["vcpkg", "list"], Path("repo"))

    def test_reports_a_failed_vcpkg_invocation(self) -> None:
        error = subprocess.CalledProcessError(1, "vcpkg", stderr="boom")
        with mock.patch.object(licenses.subprocess, "run", side_effect=error):
            with self.assertRaises(licenses.LicenseError):
                licenses._vcpkg_json(["vcpkg", "list"], Path("repo"))

    def test_reports_non_json_output(self) -> None:
        with mock.patch.object(licenses.subprocess, "run", return_value=mock.Mock(stdout="{")):
            with self.assertRaises(licenses.LicenseError):
                licenses._vcpkg_json(["vcpkg", "list"], Path("repo"))


class OfficialCliBoundary(unittest.TestCase):
    """The authoritative installed set is the vcpkg JSON CLI, not the stale status file."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self.entries = _official_entries(licenses.target_triplet("x64"))
        root = licenses.vcpkg_root(self.repo, "x64")
        _materialize(root, self.entries)
        # A stale dpkg-style checkpoint that disagrees with the JSON CLI must be ignored.
        (root / "vcpkg").mkdir(parents=True, exist_ok=True)
        (root / "vcpkg" / "status").write_bytes((FIXTURES / "status.txt").read_bytes())
        (self.repo / "vcpkg.json").write_text('{"name": "unpackrat"}\n', encoding="utf-8")
        self.enterContext(
            mock.patch.object(licenses, "_git_tracked", return_value=("vcpkg.json",))
        )
        self.enterContext(mock.patch.object(licenses, "git_provenance", return_value=PROVENANCE))
        licenses.write_attestation(self.repo, "x64")

    def _collector(self):
        listing = _fixture_json("vcpkg-installed-list.json")
        info = _fixture_json("vcpkg-installed-info.json")
        return lambda root, repository: (listing, info)

    def test_status_checkpoint_does_not_dictate_the_export(self) -> None:
        output = licenses.export_inputs(
            self.repo, "x64", Path(self._tmp.name) / "inputs",
            collect=self._collector(),
        )
        document = json.loads((output / "installed.json").read_text(encoding="utf-8"))
        by_name = {package["name"]: package for package in document["packages"]}
        self.assertEqual(
            set(by_name), {"bcdec", "brotli", "vcpkg-cmake", "vcpkg-cmake-config"}
        )
        self.assertEqual(by_name["vcpkg-cmake"]["version"], "2025-08-07")
        self.assertEqual(by_name["vcpkg-cmake"]["kind"], "host")
        self.assertEqual(by_name["bcdec"]["abi"], OFFICIAL_ABI["bcdec"])
        self.assertEqual(by_name["bcdec"]["depends"], ["vcpkg-cmake:x64-windows"])
        self.assertEqual((output / "sbom" / "brotli.spdx.json").is_file(), True)


class CliRouting(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / "licenses").mkdir(parents=True)
        (self.repo / "LICENSE.txt").write_bytes(b"MIT\n")
        (self.repo / "licenses" / "Observer.txt").write_bytes(b"LGPL\n")
        (self.repo / "licenses" / "bcdec.txt").write_bytes(b"bcdec\n")
        self.entries = {}
        for architecture in licenses.ARCHITECTURES:
            entries = _entries(licenses.target_triplet(architecture))
            self.entries[architecture] = entries
            _materialize(licenses.vcpkg_root(self.repo, architecture), entries)
        self.inventory_path = Path(self._tmp.name) / "inventory.json"
        self.inventory_path.write_text(json.dumps(_inventory_document()), encoding="utf-8")
        (self.repo / "vcpkg.json").write_text('{"name": "unpackrat"}\n', encoding="utf-8")
        self.enterContext(
            mock.patch.object(licenses, "_git_tracked", return_value=("vcpkg.json",))
        )
        self.enterContext(mock.patch.object(licenses, "git_provenance", return_value=PROVENANCE))
        for architecture in licenses.ARCHITECTURES:
            licenses.write_attestation(self.repo, architecture)

    def _collect(self, root, repository):
        entries = self.entries[Path(root).name]
        return _listing(entries), _info(entries)

    def test_license_inputs_command_exports(self) -> None:
        output = Path(self._tmp.name) / "cli-inputs"
        with mock.patch.object(main, "_find_vcpkg", return_value=Path("vcpkg")), \
                mock.patch.object(licenses, "vcpkg_collector", return_value=self._collect), \
                mock.patch.object(licenses, "git_provenance", return_value=PROVENANCE):
            code = main.main([
                "license-inputs", "-Repository", str(self.repo),
                "-Arch", "x64", "-Output", str(output),
            ])
        self.assertEqual(code, 0)
        self.assertTrue((output / "installed.json").is_file())

    def test_license_prepare_command_writes_ort_inputs(self) -> None:
        inputs = Path(self._tmp.name) / "inputs"
        with mock.patch.object(main, "_find_vcpkg", return_value=Path("vcpkg")), \
                mock.patch.object(licenses, "vcpkg_collector", return_value=self._collect), \
                mock.patch.object(licenses, "git_provenance", return_value=PROVENANCE):
            for architecture in licenses.ARCHITECTURES:
                main.main([
                    "license-inputs", "-Repository", str(self.repo),
                    "-Arch", architecture, "-Output", str(inputs / architecture),
                ])
        output = Path(self._tmp.name) / "prepared"
        with mock.patch.object(licenses, "default_fetch", side_effect=_fetch):
            code = main.main([
                "license-prepare", "-Repository", str(self.repo),
                "-Inputs", str(inputs), "-Output", str(output),
                "-Inventory", str(self.inventory_path),
            ])
        self.assertEqual(code, 0)
        self.assertTrue((output / "unpackrat.spdx.json").is_file())
        self.assertTrue((output / ".ort.yml").is_file())

    def test_license_prepare_failure_exits_two(self) -> None:
        code = main.main([
            "license-prepare", "-Repository", str(self.repo),
            "-Inputs", str(Path(self._tmp.name) / "missing"),
            "-Output", str(Path(self._tmp.name) / "prepared"),
            "-Inventory", str(self.inventory_path),
        ])
        self.assertEqual(code, 2)


MBED_BZ2 = (
    "https://github.com/Mbed-TLS/mbedtls/releases/download/mbedtls-4.1.1/"
    "mbedtls-4.1.1.tar.bz2"
)


class SourceArtifacts(unittest.TestCase):
    """Every source artifact is content-addressed and format-preserving."""

    def test_archive_extension_matches_known_formats(self) -> None:
        self.assertEqual(licenses._archive_extension(MBED_BZ2), ".tar.bz2")
        self.assertEqual(licenses._archive_extension("https://x/a.tgz"), ".tgz")
        self.assertEqual(licenses._archive_extension("https://x/a.tar.xz"), ".tar.xz")
        self.assertEqual(licenses._archive_extension("https://x/a.zip?token=1"), ".zip")

    def test_archive_extension_ignores_raw_files(self) -> None:
        self.assertIsNone(licenses._archive_extension(LOCAL_BCDEC))
        self.assertIsNone(
            licenses._archive_extension("https://github.com/mackron/dr_libs/blob/x/dr_mp3.h")
        )

    def test_single_release_archive_is_preserved_verbatim(self) -> None:
        payload = b"mbed tls 4.1.1 release bytes\n"
        resources = [{"name": "mbedtls", "url": MBED_BZ2, "sha512": _sha512(payload)}]
        data, extension = licenses._fetch_source({"name": "mbedtls"}, resources, lambda url: payload)
        self.assertEqual(data, payload)
        self.assertEqual(extension, ".tar.bz2")

    def test_raw_header_is_bundled_into_a_tar(self) -> None:
        payload = b"bcdec header\n"
        resources = [{"name": "bcdec.h", "url": LOCAL_BCDEC, "sha512": _sha512(payload)}]
        data, extension = licenses._fetch_source({"name": "bcdec"}, resources, lambda url: payload)
        self.assertEqual(extension, ".tar")
        self.assertEqual(data, licenses.deterministic_tar({"bcdec.h": payload}))

    def _artifact(self, payload: bytes) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            component = {
                "name": "brotli", "version": "1.2.0", "shareData": {}, "shareFiles": [],
                "resources": [{"name": "brotli", "url": BROTLI_GIT, "sha512": _sha512(payload)}],
            }
            sources = licenses._write_sources([component], Path(tmp), lambda url: payload)
        return str(sources["brotli"]["artifact"])

    def test_content_address_tracks_bytes_not_the_version(self) -> None:
        first = self._artifact(b"release bytes one\n")
        second = self._artifact(b"release bytes two\n")
        self.assertNotEqual(first, second)
        self.assertEqual(first, _sha256(b"release bytes one\n") + ".tar.gz")

    def test_source_identity_variants(self) -> None:
        self.assertEqual(
            licenses._source_identity("git+https://github.com/Google/Brotli@v1.2.0"),
            "github.com/google/brotli",
        )
        self.assertEqual(
            licenses._source_identity(LOCAL_BCDEC), "github.com/iorange/bcdec"
        )
        self.assertEqual(
            licenses._source_identity("https://github.com/o/r.git"), "github.com/o/r"
        )
        self.assertEqual(
            licenses._source_identity("https://files.example.org/tools/a.tar.gz"),
            "files.example.org/tools/a.tar.gz",
        )

    def test_read_bytes_reports_a_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(licenses.LicenseError):
                licenses._read_bytes(Path(tmp) / "missing", "thing")

    def test_literal_escapes_regex_metacharacters(self) -> None:
        self.assertEqual(licenses._literal("a.b*c"), "a\\.b\\*c")

    def test_no_rule_violations_without_a_policy(self) -> None:
        self.assertEqual(licenses._rule_violations([{"resolution": None}]), [])


def _adopted_inventory(*, api_members, policy, catch2=True):
    document = _inventory_document()
    document["components"] = [
        component for component in document["components"]
        if not (isinstance(component, dict) and component.get("name") == "observer-api")
    ]
    observer = {
        "name": "observer-api", "kind": "adopted-source", "scope": "runtime",
        "vcpkgName": None, "declaredExpression": "LGPL-3.0-only", "incorporated": True,
        "members": list(api_members), "source": {"origin": "repository"},
        "notices": ["licenses/Observer.txt", "licenses/GPL-3.0.txt"],
    }
    if policy is not None:
        observer["policyResolution"] = policy
    document["components"].append(observer)
    if catch2:
        document["components"].append({
            "name": "vcpkg-catch2-recipe", "kind": "adopted-source", "scope": "build",
            "vcpkgName": None, "declaredExpression": "MIT", "incorporated": True,
            "members": [
                "build/vcpkg/ports/catch2/portfile.cmake",
                "build/vcpkg/ports/catch2/fix-install-path.patch",
            ],
            "source": {"origin": "repository"}, "notices": ["licenses/vcpkg.txt"],
        })
    document["components"].append({
        "name": "rpatool", "kind": "adopted-source", "scope": "reference",
        "vcpkgName": None, "declaredExpression": "WTFPL",
        "source": {"origin": "reference"}, "notices": ["licenses/rpatool.txt"],
    })
    return document


class AdoptedSource(_PipelineBase):
    """Repository-adopted sources become content-addressed, linked components."""

    API_BYTES = b"#ifndef UNPACKRAT_API_H\n/* LGPLv3 */\n#endif\n"
    CATCH2_PORT = b"vcpkg_from_github(...)\n"
    CATCH2_PATCH = b"--- a\n+++ b\n"

    def setUp(self) -> None:
        super().setUp()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "api.h").write_bytes(self.API_BYTES)
        port_dir = self.repo / "build" / "vcpkg" / "ports" / "catch2"
        port_dir.mkdir(parents=True)
        (port_dir / "portfile.cmake").write_bytes(self.CATCH2_PORT)
        (port_dir / "fix-install-path.patch").write_bytes(self.CATCH2_PATCH)
        (self.repo / "licenses" / "GPL-3.0.txt").write_bytes(b"GPL text\n")
        (self.repo / "licenses" / "vcpkg.txt").write_bytes(b"vcpkg MIT\n")
        (self.repo / "licenses" / "rpatool.txt").write_bytes(b"WTFPL\n")
        self.write_inventory(self.policy(approved=self.API_BYTES))

    def policy(self, *, approved: bytes, reason: str = "CANT_FIX_EXCEPTION"):
        return {
            "reason": reason,
            "license": "LGPL-3.0-only",
            "comment": "Reviewed snapshot only; changed source requires review.",
            "approvedMembers": {"src/api.h": _sha256(approved)},
        }

    def write_inventory(self, policy, *, api_members=("src/api.h",), catch2=True) -> None:
        document = _adopted_inventory(
            api_members=api_members, policy=policy, catch2=catch2
        )
        self.inventory_path.write_text(json.dumps(document), encoding="utf-8")

    def _observer_artifact(self) -> str:
        return _sha256(
            licenses.deterministic_tar({"src/api.h": self.API_BYTES})
        ) + ".tar"

    def test_incorporated_sources_reach_spdx_and_curations(self) -> None:
        output, document = self._prepare()
        packages = self._packages(document)
        artifact = self._observer_artifact()
        self.assertEqual(packages["observer-api"]["versionInfo"], artifact[: -len(".tar")])
        self.assertEqual(packages["observer-api"]["packageFileName"], artifact)
        self.assertIn("vcpkg-catch2-recipe", packages)
        self.assertNotIn("rpatool", packages)
        edges = {
            (rel["spdxElementId"], rel["relationshipType"], rel["relatedSpdxElement"])
            for rel in document["relationships"]
        }
        root = "SPDXRef-Package-unpackrat"
        self.assertIn((root, "STATIC_LINK", "SPDXRef-Package-observer-api"), edges)
        self.assertIn(
            ("SPDXRef-Package-vcpkg-catch2-recipe", "BUILD_DEPENDENCY_OF", root), edges
        )
        self.assertNotIn(
            (root, "STATIC_LINK", "SPDXRef-Package-vcpkg-catch2-recipe"), edges
        )
        curations = {
            curation["id"]: curation["curations"]
            for curation in json.loads(
                (output / "package-curations.yml").read_text(encoding="utf-8")
            )
        }
        observer = f"SpdxDocumentFile:vcpkg:observer-api:{artifact[: -len('.tar')]}"
        self.assertEqual(curations[observer]["source_code_origins"], ["ARTIFACT"])
        self.assertEqual(
            curations[observer]["source_artifact"]["hash"]["algorithm"], "SHA-512"
        )
        self.assertTrue(
            curations[observer]["source_artifact"]["url"].endswith(artifact)
        )
        self.assertTrue((output / "sources" / artifact).is_file())

    def test_observer_resolution_binds_the_artifact_derived_id(self) -> None:
        output, _ = self._prepare()
        config = json.loads((output / ".ort.yml").read_text(encoding="utf-8"))
        violations = config["resolutions"]["rule_violations"]
        self.assertEqual(len(violations), 1)
        identifier = "SpdxDocumentFile:vcpkg:observer-api:" + self._observer_artifact()[
            : -len(".tar")
        ]
        expected = (
            f"The dependency '{identifier}' is statically linked and licensed under the "
            "ScanCode 'copyleft-limited' categorized license LGPL-3\\.0-only\\."
        )
        self.assertEqual(violations[0]["message"], expected)
        self.assertEqual(violations[0]["reason"], "CANT_FIX_EXCEPTION")
        self.assertIn("requires review", violations[0]["comment"])

    def test_changed_observer_source_invalidates_the_resolution(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self.write_inventory(self.policy(approved=b"different bytes\n"))
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_missing_member_is_reported(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self.write_inventory(self.policy(approved=self.API_BYTES), api_members=("src/gone.h",))
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_escaping_member_is_rejected(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self.write_inventory(self.policy(approved=self.API_BYTES), api_members=("../secret",))
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_repository_component_without_members_is_rejected(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self.write_inventory(self.policy(approved=self.API_BYTES), api_members=())
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_incorporated_component_requires_a_policy_map_when_present(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self.write_inventory({"reason": "CANT_FIX_EXCEPTION", "license": "x", "comment": "y"})
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_pending_notice_is_rejected(self) -> None:
        document = _adopted_inventory(api_members=(), policy=None, catch2=False)
        document["components"][0]["pendingNotice"] = True
        self.inventory_path.write_text(json.dumps(document), encoding="utf-8")
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )


class PipelinePolicy(_PipelineBase):
    """Origin, scope and package-configuration behaviour of the vcpkg components."""

    def test_prepare_rejects_a_source_origin_change(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        document = _inventory_document()
        for component in document["components"]:
            if isinstance(component, dict) and component.get("name") == "brotli":
                component["source"]["identity"] = "github.com/evil/brotli"
        self.inventory_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_rejects_a_spoofed_source_host(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        spoof = "https://github.attacker.example/google/brotli/archive/v1.2.0.tar.gz"

        def mutate(raw, package):
            for entry in raw["packages"]:
                if entry["SPDXID"].startswith("SPDXRef-resource-"):
                    entry["downloadLocation"] = spoof
            package["resources"][0]["url"] = spoof
            package["resources"][0]["archiveUrl"] = None

        self._rewrite_export_both("brotli", mutate)
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_accepts_a_same_origin_version_upgrade(self) -> None:
        new_bytes = b"brotli 1.2.1 release bytes\n"
        new_git = "git+https://github.com/google/brotli@v1.2.1"
        new_archive = "https://github.com/google/brotli/archive/v1.2.1.tar.gz"
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)

        def mutate(raw, package):
            for entry in raw["packages"]:
                if entry["SPDXID"] == "SPDXRef-port":
                    entry["versionInfo"] = "1.2.1"
                elif entry["SPDXID"].startswith("SPDXRef-resource-"):
                    entry["downloadLocation"] = new_git
                    entry["checksums"] = [
                        {"algorithm": "SHA512", "checksumValue": _sha512(new_bytes)}
                    ]
            package["version"] = "1.2.1"
            package["fullVersion"] = "1.2.1"
            package["resources"][0]["url"] = new_git
            package["resources"][0]["archiveUrl"] = new_archive
            package["resources"][0]["sha512"] = _sha512(new_bytes)

        self._rewrite_export_both("brotli", mutate)

        def fetch(url: str) -> bytes:
            return new_bytes if url == new_archive else _fetch(url)

        output = Path(self._tmp.name) / "upgraded"
        licenses.prepare(
            Path(self._tmp.name) / "inputs", output,
            inventory_path=self.inventory_path, repository=self.repo, fetch=fetch,
        )
        self.assertTrue((output / "sources" / f"{_sha256(new_bytes)}.tar.gz").is_file())

    def test_prepare_marks_static_links_and_scopes(self) -> None:
        _, document = self._prepare()
        edges = {
            (rel["spdxElementId"], rel["relationshipType"], rel["relatedSpdxElement"])
            for rel in document["relationships"]
        }
        root = "SPDXRef-Package-unpackrat"
        self.assertIn((root, "STATIC_LINK", "SPDXRef-Package-bcdec"), edges)
        self.assertIn((root, "DEPENDS_ON", "SPDXRef-Package-bcdec"), edges)
        self.assertIn(("SPDXRef-Package-vcpkg-cmake", "BUILD_DEPENDENCY_OF", root), edges)
        self.assertNotIn(
            ("SPDXRef-Package-bcdec", "STATIC_LINK", "SPDXRef-Package-vcpkg-cmake"), edges
        )
        self.assertNotIn(("SPDXRef-Package-bcdec", "STATIC_LINK", root), edges)

    def test_prepare_emits_a_test_scope_for_test_libraries(self) -> None:
        document = json.loads(json.dumps(_inventory_document()))
        for component in document["components"]:
            if isinstance(component, dict) and component.get("name") == "brotli":
                component["scope"] = "test"
        self.inventory_path.write_text(json.dumps(document), encoding="utf-8")
        _, doc = self._prepare()
        edges = {
            (rel["spdxElementId"], rel["relationshipType"], rel["relatedSpdxElement"])
            for rel in doc["relationships"]
        }
        root = "SPDXRef-Package-unpackrat"
        self.assertIn(("SPDXRef-Package-brotli", "TEST_DEPENDENCY_OF", root), edges)
        self.assertIn((root, "STATIC_LINK", "SPDXRef-Package-brotli"), edges)

    def test_prepare_copies_reviewed_package_configurations(self) -> None:
        config_dir = self.repo / ".ort" / "package-configurations"
        config_dir.mkdir(parents=True)
        payload = b'id: "SpdxDocumentFile:vcpkg:bcdec:0.985"\n'
        (config_dir / "bcdec.yml").write_bytes(payload)
        output, _ = self._prepare()
        self.assertEqual((output / "package-configurations" / "bcdec.yml").read_bytes(), payload)

    def test_absent_package_configurations_directory_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            output.mkdir()
            licenses._copy_package_configurations(self.repo, output)
            self.assertFalse((output / "package-configurations").exists())

    def test_unreviewed_dependency_edge_is_rejected(self) -> None:
        output = Path(self._tmp.name) / "spdx"
        output.mkdir()
        document = {
            "rootPackage": {"name": "unpackrat", "license": "MIT"},
        }
        components = [{
            "name": "bcdec", "version": "0.985", "expression": "MIT", "scope": "runtime",
            "origin": "overlay", "staticallyLinked": True, "depends": ["ghost"],
        }]
        sources = {"bcdec": {"artifact": "a.tar.gz"}}
        provenance = {"provenance": {"sourceCommit": "a" * 40}}
        with self.assertRaises(licenses.LicenseError):
            licenses._write_spdx(document, components, sources, provenance, output)

    def test_unknown_inventory_component_kind_is_rejected(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        document = _inventory_document()
        document["components"].append({"name": "mystery", "kind": "unclassified"})
        self.inventory_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_repository_component_not_incorporated_is_ignored(self) -> None:
        component = {
            "name": "sketched", "kind": "adopted-source", "scope": "build",
            "source": {"origin": "repository"}, "incorporated": False,
        }
        self.assertEqual(licenses._adopted_components(component, self.repo), [])

    def test_transitive_static_link_is_emitted(self) -> None:
        output = Path(self._tmp.name) / "spdx"
        output.mkdir()
        document = {"rootPackage": {"name": "unpackrat", "license": "MIT"}}
        components = [
            {"name": "zydis", "version": "4.1.1", "expression": "MIT", "scope": "runtime",
             "origin": "registry", "staticallyLinked": True, "depends": ["zycore"]},
            {"name": "zycore", "version": "1.5.2", "expression": "MIT", "scope": "runtime",
             "origin": "registry", "staticallyLinked": True, "depends": []},
        ]
        sources = {"zydis": {"artifact": "z.tar.gz"}, "zycore": {"artifact": "c.tar.gz"}}
        provenance = {"provenance": {"sourceCommit": "a" * 40}}
        licenses._write_spdx(document, components, sources, provenance, output)
        edges = {
            (rel["spdxElementId"], rel["relationshipType"], rel["relatedSpdxElement"])
            for rel in json.loads(
                (output / "unpackrat.spdx.json").read_text(encoding="utf-8")
            )["relationships"]
        }
        self.assertIn(
            ("SPDXRef-Package-zydis", "STATIC_LINK", "SPDXRef-Package-zycore"), edges
        )

    def test_emitted_spdx_document_drops_the_unsupported_schema_annotation(self) -> None:
        # The pinned ORT 97 SPDX importer rejects the ``$schema`` key with an
        # UnrecognizedPropertyException, so the emitted document must carry the SPDX 2.3 graph and
        # every other field but that one unsupported annotation.
        output = Path(self._tmp.name) / "spdx"
        output.mkdir()
        document = {"rootPackage": {"name": "unpackrat", "license": "MIT"}}
        components = [
            {"name": "zydis", "version": "4.1.1", "expression": "MIT", "scope": "runtime",
             "origin": "registry", "staticallyLinked": True, "depends": []},
        ]
        sources = {"zydis": {"artifact": "z.tar.gz"}}
        provenance = {"provenance": {"sourceCommit": "a" * 40}}
        licenses._write_spdx(document, components, sources, provenance, output)
        emitted = json.loads((output / "unpackrat.spdx.json").read_text(encoding="utf-8"))
        self.assertNotIn("$schema", emitted)
        self.assertEqual(emitted["spdxVersion"], "SPDX-2.3")
        self.assertEqual(emitted["dataLicense"], "CC0-1.0")
        self.assertEqual(emitted["SPDXID"], "SPDXRef-DOCUMENT")
        self.assertEqual(emitted["name"], "unpackrat")
        self.assertIn("SPDXRef-Package-zydis", {p["SPDXID"] for p in emitted["packages"]})


class ExportIntegrity(_PipelineBase):
    """``load_export`` trusts only the raw vcpkg evidence, never the manifest summary."""

    def _prepare_now(self) -> None:
        licenses.prepare(
            Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
            inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
        )

    def test_rejects_a_tampered_raw_sbom_hash(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        manifest = Path(self._tmp.name) / "inputs" / "x86" / "installed.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        sbom = manifest.parent / document["packages"][0]["sbom"]["path"]
        sbom.write_bytes(b"{}")
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_rejects_a_reserialized_raw_sbom(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        manifest = Path(self._tmp.name) / "inputs" / "x86" / "installed.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        record = document["packages"][0]["sbom"]
        sbom = manifest.parent / record["path"]
        sbom.write_text("{not json", encoding="utf-8")
        record["sha256"] = _sha256(sbom.read_bytes())
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_rejects_facts_that_disagree_with_the_raw_sbom(self) -> None:
        # Tampering BOTH architecture manifests identically must not silently audit a different
        # source: the exported summary is re-derived from the authoritative raw SBOM.
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self._rewrite_both("brotli", declaredExpression="GPL-3.0-only")
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_rejects_a_tampered_copyright_record(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        manifest = Path(self._tmp.name) / "inputs" / "x64" / "installed.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        record = document["packages"][0]["copyright"]
        (manifest.parent / record["path"]).write_bytes(b"tampered\n")
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_rejects_an_escaping_export_path(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        manifest = Path(self._tmp.name) / "inputs" / "x86" / "installed.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["packages"][0]["sbom"]["path"] = "../escape.spdx.json"
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_rejects_a_missing_packages_array(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "installed.json"
            path.write_text(json.dumps({"schema": licenses.SCHEMA_INPUTS}), encoding="utf-8")
            with self.assertRaises(licenses.LicenseError):
                licenses.load_export(Path(tmp))

    def test_rejects_a_raw_sbom_port_name_mismatch(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)

        def mutate(raw, package):
            for entry in raw["packages"]:
                if entry["SPDXID"] == "SPDXRef-port":
                    entry["name"] = "other"

        self._rewrite_export_both("brotli", mutate)
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_rejects_a_full_version_inconsistency(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self._rewrite("x86", "brotli", fullVersion="9.9.9")
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_rejects_a_non_list_resources_record(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self._rewrite("x86", "brotli", resources="bad")
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_rejects_a_wrong_archive_url_record(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self._rewrite_both("brotli", resources=[
            {"name": "google/brotli", "url": BROTLI_GIT, "archiveUrl": None,
             "sha512": _sha512(BROTLI_BYTES)},
        ])
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_rejects_a_multi_origin_component(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)

        def mutate(raw, package):
            raw["packages"].append({
                "SPDXID": "SPDXRef-resource-1", "name": "other",
                "downloadLocation": "git+https://github.com/evil/brotli@v1",
                "checksums": [{"algorithm": "SHA512", "checksumValue": "d" * 128}],
            })
            package["resources"].append({
                "name": "other", "url": "git+https://github.com/evil/brotli@v1",
                "archiveUrl": "https://github.com/evil/brotli/archive/v1.tar.gz",
                "sha512": "d" * 128,
            })

        self._rewrite_export_both("brotli", mutate)
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_source_bytes_rejects_a_component_without_any_source(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses._source_bytes(
                {"name": "x", "kind": "vcpkg-package", "shareData": {}, "resources": []},
                _fetch,
            )

    def test_verify_share_rejects_a_checksum_mismatch(self) -> None:
        component = {"name": "helper", "shareFiles": [{"path": "a.cmake", "sha256": "0" * 64}]}
        with self.assertRaises(licenses.LicenseError):
            licenses._verify_share(component, "a.cmake", b"data")


class PortRevisionParity(_PipelineBase):
    """The full ``base#port`` identity flows into parity, correction and the published IDs."""

    def test_published_identity_carries_the_port_revision(self) -> None:
        # A genuine port-revision install: the vcpkg list boundary reports port_version 1 and the
        # raw emitted SBOM records the full ``1.2.0#1`` identity, so the export records both and
        # the published identity is the full base#port form rather than a forged manifest field.
        for architecture in licenses.ARCHITECTURES:
            entries = _entries(licenses.target_triplet(architecture))
            brotli = next(item for item in entries if item["name"] == "brotli")
            brotli["port_version"] = 1
            for package in brotli["sbom"]["packages"]:
                if package["SPDXID"] == "SPDXRef-port":
                    package["versionInfo"] = "1.2.0#1"
            _materialize(licenses.vcpkg_root(self.repo, architecture), entries)
            licenses.export_inputs(
                self.repo, architecture, Path(self._tmp.name) / "inputs" / architecture,
                collect=_collector(entries),
            )
        output = Path(self._tmp.name) / "prepared"
        licenses.prepare(
            Path(self._tmp.name) / "inputs", output,
            inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
        )
        document = json.loads((output / "unpackrat.spdx.json").read_text(encoding="utf-8"))
        versions = {package["name"]: package["versionInfo"] for package in document["packages"]}
        self.assertEqual(versions["brotli"], "1.2.0#1")
        curations = json.loads((output / "package-curations.yml").read_text(encoding="utf-8"))
        self.assertIn("SpdxDocumentFile:vcpkg:brotli:1.2.0#1", {c["id"] for c in curations})

    def test_prepare_rejects_a_port_revision_parity_break(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self._rewrite("x86", "brotli", portVersion=1, fullVersion="1.2.0#1")
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_prepare_rejects_a_malformed_port_version(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self._rewrite_both("brotli", portVersion="1")
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )

    def test_correction_binds_the_full_port_identity(self) -> None:
        reference = {
            "version": "2024-08-02", "fullVersion": "2024-08-02#1",
            "resources": [{"sha512": "digest"}],
        }
        licenses._require_correction(
            "fastlz", reference, {"version": "2024-08-02#1", "sha512": "digest"}
        )
        with self.assertRaises(licenses.LicenseError):
            licenses._require_correction(
                "fastlz", reference, {"version": "2024-08-02#2", "sha512": "digest"}
            )

    def test_prepare_rejects_a_host_helper_share_membership_break(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        manifest = Path(self._tmp.name) / "inputs" / "x64" / "installed.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        for package in document["packages"]:
            if package["name"] == "vcpkg-cmake":
                package["shareFiles"] = package["shareFiles"][:1]
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            licenses.prepare(
                Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )


class SourceIdentityHardening(unittest.TestCase):
    """Only exact official GitHub hosts describe an official upstream origin."""

    def test_official_github_hosts_map_to_owner_repo(self) -> None:
        cases = (
            ("git+https://github.com/Google/Brotli@v1.2.0", "github.com/google/brotli"),
            ("https://github.com/google/brotli/archive/v1.2.0.tar.gz", "github.com/google/brotli"),
            ("https://raw.githubusercontent.com/iOrange/bcdec/abc/bcdec.h", "github.com/iorange/bcdec"),
            (
                "https://codeload.github.com/google/brotli/tar.gz/refs/tags/v1.2.0",
                "github.com/google/brotli",
            ),
            ("https://github.com/google/brotli.git", "github.com/google/brotli"),
        )
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(licenses._source_identity(url), expected)

    def test_spoofed_hosts_never_impersonate_official_github(self) -> None:
        for url in (
            "https://github.attacker.example/google/brotli",
            "https://github.com@attacker.example/google/brotli",
            "https://github.com.evil.example/google/brotli",
            "https://github.com:8443/google/brotli",
            "https://raw.githubusercontent.com.evil.example/google/brotli",
        ):
            with self.subTest(url=url):
                self.assertNotEqual(
                    licenses._source_identity(url), "github.com/google/brotli"
                )

    def test_official_host_without_owner_repo_is_rejected(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses._source_identity("https://github.com/onlyowner")

    def test_hostless_url_is_rejected(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses._source_identity("file:///etc/passwd")

    def test_non_github_host_is_keyed_on_the_exact_host(self) -> None:
        self.assertEqual(
            licenses._source_identity("https://files.example.org/tools/a.tar.gz"),
            "files.example.org/tools/a.tar.gz",
        )


class PathContainment(unittest.TestCase):
    """Exported and inventory paths can never escape their explicit root."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        self.root.mkdir()

    def test_accepts_nested_relative_paths(self) -> None:
        target = licenses._contained_path(self.root, "sbom/bcdec.spdx.json")
        self.assertEqual(target, (self.root / "sbom" / "bcdec.spdx.json").resolve())

    def test_rejects_every_escape_form(self) -> None:
        for bad in (
            "/etc/passwd",
            "C:/outside/file",
            "C:\\outside",
            "\\\\server\\share\\file",
            "//server/share",
            "../outside",
            "a/../../b",
            "a\\b",
            "",
        ):
            with self.subTest(path=bad):
                with self.assertRaises(licenses.LicenseError):
                    licenses._contained_path(self.root, bad)

    def test_rejects_a_symlink_that_leaves_the_root(self) -> None:
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"secret\n")
        link = self.root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks are unavailable on this host")
        with self.assertRaises(licenses.LicenseError):
            licenses._contained_path(self.root, "link/secret")


class OwnSourceMembership(_PipelineBase):
    """The own-source artifact is a Git-metadata membership that fails closed on new files."""

    def _component(self, **overrides):
        base = {
            "name": "unpackrat-source", "kind": "own-source", "scope": "runtime",
            "declaredExpression": "MIT", "source": {"origin": "repository"},
            "notices": ["LICENSE.txt"], "roots": ["src"], "include": [],
            "exclude": ["*.md"],
        }
        base.update(overrides)
        return base

    def _write(self, relative: str, data: bytes = b"x\n") -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_gathers_tracked_files_under_roots(self) -> None:
        self._write("src/a.cpp")
        self._write("docs/b.md")
        with mock.patch.object(
            licenses, "_git_tracked", return_value=("src/a.cpp", "docs/b.md")
        ):
            component = licenses._own_source_component(self._component(), self.repo, set())
        self.assertEqual(sorted(component["shareData"]), ["src/a.cpp"])
        self.assertTrue(component["staticallyLinked"])

    def test_untracked_files_never_enter_the_artifact(self) -> None:
        self._write("src/tracked.cpp")
        self._write("src/untracked.cpp")
        with mock.patch.object(
            licenses, "_git_tracked", return_value=("src/tracked.cpp",)
        ):
            component = licenses._own_source_component(self._component(), self.repo, set())
        self.assertEqual(sorted(component["shareData"]), ["src/tracked.cpp"])

    def test_claimed_members_are_not_double_owned(self) -> None:
        self._write("src/api.h")
        with mock.patch.object(licenses, "_git_tracked", return_value=("src/api.h",)):
            component = licenses._own_source_component(
                self._component(), self.repo, {"src/api.h"}
            )
        self.assertEqual(component["shareData"], {})

    def test_a_new_tracked_file_outside_every_component_fails_closed(self) -> None:
        with mock.patch.object(licenses, "_git_tracked", return_value=("tools/extra.c",)):
            with self.assertRaises(licenses.LicenseError):
                licenses._own_source_component(self._component(exclude=[]), self.repo, set())

    def test_included_paths_outside_the_roots_are_scanned(self) -> None:
        self._write("build.ps1", b"param()\n")
        with mock.patch.object(licenses, "_git_tracked", return_value=("build.ps1",)):
            component = licenses._own_source_component(
                self._component(roots=["src"], include=["build.ps1"]), self.repo, set()
            )
        self.assertEqual(sorted(component["shareData"]), ["build.ps1"])

    def test_no_roots_or_includes_is_rejected(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses._own_source_component(
                self._component(roots=[], include=[]), self.repo, set()
            )

    def test_claimed_members_helper_filters_non_repository_and_unincorporated(self) -> None:
        adopted = [
            {"incorporated": True, "source": {"origin": "repository"}, "members": ["src/api.h"]},
            {"incorporated": False, "source": {"origin": "repository"}, "members": ["skipped"]},
            {"incorporated": True, "source": {"origin": "reference"}, "members": ["skipped"]},
            {"incorporated": True, "source": {"origin": "repository"}},
        ]
        self.assertEqual(licenses._claimed_members(adopted), {"src/api.h"})

    def test_matches_any_uses_documented_globs(self) -> None:
        self.assertTrue(licenses._matches_any("docs/a.md", ["*.md"]))
        self.assertFalse(licenses._matches_any("src/a.cpp", ["*.md"]))
        self.assertFalse(licenses._matches_any("src/a.cpp", []))

    def test_git_tracked_reads_the_ls_files_stream(self) -> None:
        completed = mock.Mock(stdout="src/a.cpp\x00src/b.h\x00")
        with mock.patch.object(licenses, "_git_tracked", self._real_git_tracked), \
                mock.patch.object(licenses.subprocess, "run", return_value=completed) as run:
            self.assertEqual(licenses._git_tracked(self.repo), ("src/a.cpp", "src/b.h"))
        self.assertEqual(run.call_args.args[0][:3], ["git", "-C", str(self.repo)])

    def test_prepare_scans_the_own_source_component(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self._write("src/extra.cpp", b"int x;\n")
        document = _inventory_document()
        document["components"].append({
            "name": "unpackrat-source", "kind": "own-source", "scope": "runtime",
            "declaredExpression": "MIT", "source": {"origin": "repository"},
            "notices": ["LICENSE.txt"], "roots": ["src"], "exclude": ["vcpkg.json"],
        })
        self.inventory_path.write_text(json.dumps(document), encoding="utf-8")
        with mock.patch.object(
            licenses, "_git_tracked", return_value=("vcpkg.json", "src/extra.cpp")
        ):
            output = Path(self._tmp.name) / "own"
            licenses.prepare(
                Path(self._tmp.name) / "inputs", output,
                inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
            )
        document = json.loads((output / "unpackrat.spdx.json").read_text(encoding="utf-8"))
        self.assertIn("unpackrat-source", {package["name"] for package in document["packages"]})


class ReleaseAttestation(_PipelineBase):
    """The existing restore is exported only with a successful Release build attestation."""

    def _attestation(self, architecture: str) -> Path:
        return licenses.attestation_path(self.repo, architecture)

    def _rewrite_attestation(self, architecture: str, **fields) -> None:
        path = self._attestation(architecture)
        document = json.loads(path.read_text(encoding="utf-8"))
        document.update(fields)
        path.write_text(json.dumps(document), encoding="utf-8")

    def test_export_uses_the_attested_provenance(self) -> None:
        output = self._export("x64")
        document = json.loads((output / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(document["provenance"], PROVENANCE)

    def test_export_fails_without_a_release_attestation(self) -> None:
        self._attestation("x64").unlink()
        with self.assertRaises(licenses.LicenseError):
            self._export("x64")

    def test_export_rejects_an_attestation_from_a_different_commit(self) -> None:
        self._rewrite_attestation("x64", provenance=dict(PROVENANCE, sourceCommit="f" * 40))
        with self.assertRaises(licenses.LicenseError):
            self._export("x64")

    def test_export_rejects_changed_build_inputs(self) -> None:
        # Same archive, license and features; only a reviewed build input changes, so the recorded
        # fingerprint no longer matches the checkout and the export must fail closed.
        (self.repo / "vcpkg.json").write_text('{"name": "unpackrat", "x": 1}\n', encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            self._export("x64")

    def test_export_rejects_an_attestation_from_a_foreign_run(self) -> None:
        self._rewrite_attestation("x64", provenance=dict(PROVENANCE, runId="99"))
        with self.assertRaises(licenses.LicenseError):
            self._export("x64")

    def test_load_attestation_rejects_a_wrong_schema_architecture_and_triplet(self) -> None:
        document = json.loads(self._attestation("x64").read_text(encoding="utf-8"))
        for fields in (
            {"schema": "other/1"},
            {"architecture": "x86"},
            {"triplet": "other-windows"},
        ):
            self._attestation("x64").write_text(json.dumps(dict(document, **fields)), encoding="utf-8")
            with self.subTest(fields=fields):
                with self.assertRaises(licenses.LicenseError):
                    licenses.load_attestation(self.repo, "x64")
        self._attestation("x64").write_text("[1, 2]\n", encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            licenses.load_attestation(self.repo, "x64")

    def test_a_valid_supplied_attestation_exports_identically(self) -> None:
        entries = _entries(licenses.target_triplet("x64"))
        _materialize(licenses.vcpkg_root(self.repo, "x64"), entries)
        supplied = json.loads(self._attestation("x64").read_text(encoding="utf-8"))
        output = Path(self._tmp.name) / "supplied"
        result = licenses.export_inputs(
            self.repo, "x64", output, collect=_collector(entries), attestation=supplied,
        )
        document = json.loads((result / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(document["provenance"], PROVENANCE)

    def test_export_rejects_a_supplied_attestation_that_skips_validation(self) -> None:
        entries = _entries(licenses.target_triplet("x64"))
        _materialize(licenses.vcpkg_root(self.repo, "x64"), entries)
        with self.assertRaises(licenses.LicenseError):
            licenses.export_inputs(
                self.repo, "x64", Path(self._tmp.name) / "bypass",
                collect=_collector(entries),
                attestation={"provenance": dict(PROVENANCE)},
            )


class BuildConfigurationGuard(_PipelineBase):
    """Selected build options are bound by reviewed recipe/triplet/patch content, not by URL/hash."""

    def _write_build_input(self, path: str, data: bytes) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def _approved(self, path: str, data: bytes) -> dict:
        self._write_build_input(path, data)
        with mock.patch.object(licenses, "_git_tracked", return_value=(path,)):
            return licenses.build_input_identity(self.repo)

    def test_an_unmodified_recipe_passes_review(self) -> None:
        path = "build/vcpkg/ports/x/portfile.cmake"
        approved = self._approved(path, b"set(ENABLE OFF)\n")
        with mock.patch.object(licenses, "_git_tracked", return_value=("vcpkg.json", path)):
            licenses._require_build_configuration(
                "x", {"name": "x", "buildConfiguration": dict(approved)}, self.repo
            )

    def test_a_config_only_change_fails_review(self) -> None:
        path = "build/vcpkg/ports/x/portfile.cmake"
        approved = self._approved(path, b"set(ENABLE OFF)\n")
        component = {"name": "x", "buildConfiguration": dict(approved)}
        # The archive, declared license and features are unchanged; only the recipe that selects
        # which code is compiled changes, which must force a renewed review.
        self._write_build_input(path, b"set(ENABLE ON)\n")
        with mock.patch.object(licenses, "_git_tracked", return_value=("vcpkg.json", path)):
            with self.assertRaises(licenses.LicenseError):
                licenses._require_build_configuration("x", component, self.repo)

    def test_a_missing_guarded_input_fails_review(self) -> None:
        path = "build/vcpkg/ports/x/portfile.cmake"
        approved = self._approved(path, b"set(ENABLE OFF)\n")
        with mock.patch.object(licenses, "_git_tracked", return_value=("vcpkg.json",)):
            with self.assertRaises(licenses.LicenseError):
                licenses._require_build_configuration(
                    "x", {"name": "x", "buildConfiguration": dict(approved)}, self.repo
                )

    def test_an_empty_guard_and_an_unguarded_component_are_handled(self) -> None:
        licenses._require_build_configuration("x", {"name": "x"}, self.repo)
        with self.assertRaises(licenses.LicenseError):
            licenses._require_build_configuration(
                "x", {"name": "x", "buildConfiguration": {}}, self.repo
            )

    def test_build_input_fingerprint_is_deterministic_and_needs_inputs(self) -> None:
        path = "build/vcpkg/ports/x/portfile.cmake"
        self._write_build_input(path, b"a\n")
        with mock.patch.object(licenses, "_git_tracked", return_value=(path,)):
            first = licenses.build_input_fingerprint(self.repo)
            self.assertEqual(first, licenses.build_input_fingerprint(self.repo))
        with mock.patch.object(licenses, "_git_tracked", return_value=("unrelated.txt",)):
            with self.assertRaises(licenses.LicenseError):
                licenses.build_input_fingerprint(self.repo)

    def test_write_attestation_rejects_an_unknown_architecture(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.write_attestation(self.repo, "arm64")

    def test_invalidate_attestation_rejects_an_unknown_architecture(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            licenses.invalidate_attestation(self.repo, "arm64")


class ExportShareAuthority(_PipelineBase):
    """The exported share summary is bound to the raw SBOM's own member set and bytes."""

    def _prepare_now(self) -> Path:
        return licenses.prepare(
            Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
            inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
        )

    def _helper_manifest(self, architecture: str) -> tuple[Path, dict]:
        manifest = Path(self._tmp.name) / "inputs" / architecture / "installed.json"
        return manifest, json.loads(manifest.read_text(encoding="utf-8"))

    def _helper(self, document: dict) -> dict:
        return next(package for package in document["packages"] if package["name"] == "vcpkg-cmake")

    def test_a_member_dropped_from_both_manifests_still_fails(self) -> None:
        # Deleting the helper member from *both* summaries leaves the two architectures in parity,
        # so only the raw SBOM's own membership can catch the narrowed audited set.
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
            manifest, document = self._helper_manifest(architecture)
            helper = self._helper(document)
            helper["shareFiles"] = [
                entry for entry in helper["shareFiles"] if entry["path"] != "vcpkg_cmake_build.cmake"
            ]
            manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_altered_bytes_with_a_rewritten_summary_digest_still_fails(self) -> None:
        # Rewriting the exported helper bytes *and* the manifest digest keeps the summary
        # self-consistent, but the preserved raw SBOM still disagrees, so prepare must fail.
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
            manifest, document = self._helper_manifest(architecture)
            for entry in self._helper(document)["shareFiles"]:
                if entry["path"] != "vcpkg_cmake_build.cmake":
                    continue
                target = manifest.parent / "share" / "vcpkg-cmake" / entry["path"]
                target.write_bytes(b"tampered helper bytes\n")
                entry["sha256"] = _sha256(target.read_bytes())
            manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_rejects_a_non_list_share_files_record(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        manifest, document = self._helper_manifest("x64")
        self._helper(document)["shareFiles"] = "not-a-list"
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_rejects_altered_share_bytes_with_an_unchanged_summary(self) -> None:
        # The summary still records the raw SBOM's digest, but the exported bytes no longer hash to
        # it, so the byte-level check must catch the tamper.
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        target = (
            Path(self._tmp.name) / "inputs" / "x64" / "share" / "vcpkg-cmake"
            / "vcpkg_cmake_build.cmake"
        )
        target.write_bytes(b"altered bytes keeping the original summary digest\n")
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_export_rejects_a_share_member_that_escapes_both_roots(self) -> None:
        entries = _entries(licenses.target_triplet("x64"))
        root = licenses.vcpkg_root(self.repo, "x64")
        outside = root / "escape.txt"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"outside the installed package\n")
        entries[-1]["sbom"]["files"].append({
            "fileName": "./share/vcpkg-cmake/../../../escape.txt",
            "checksums": [{"algorithm": "SHA256", "checksumValue": _sha256(outside.read_bytes())}],
        })
        output = Path(self._tmp.name) / "nested" / "export"
        _materialize(root, entries)
        with self.assertRaises(licenses.LicenseError):
            licenses.export_inputs(self.repo, "x64", output, collect=_collector(entries))
        self.assertFalse((output.parent / "escape.txt").exists())

    def test_export_rejects_every_uncontained_share_member(self) -> None:
        for index, member in enumerate((
            "../../escape.txt",
            "a/../../b.txt",
            "..\\escape.txt",
            "C:/outside.txt",
            "\\\\server\\share\\x.txt",
            "//server/share/x.txt",
        )):
            with self.subTest(member=member):
                entries = _entries(licenses.target_triplet("x64"))
                entries[-1]["sbom"]["files"].append({
                    "fileName": f"./share/vcpkg-cmake/{member}",
                    "checksums": [{"algorithm": "SHA256", "checksumValue": "0" * 64}],
                })
                _materialize(licenses.vcpkg_root(self.repo, "x64"), entries)
                with self.assertRaises(licenses.LicenseError):
                    licenses.export_inputs(
                        self.repo, "x64", Path(self._tmp.name) / f"escape-{index}",
                        collect=_collector(entries),
                    )


class PrepareBuildInputs(_PipelineBase):
    """``prepare`` binds both exports to the live checkout's build options before it fetches."""

    def _prepare_now(self) -> Path:
        return licenses.prepare(
            Path(self._tmp.name) / "inputs", Path(self._tmp.name) / "prepared",
            inventory_path=self.inventory_path, repository=self.repo, fetch=_fetch,
        )

    def test_export_records_the_current_build_inputs(self) -> None:
        output = self._export("x64")
        document = json.loads((output / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(document["buildInputs"], licenses.build_input_identity(self.repo))

    def test_prepare_rejects_a_manifest_changed_after_export(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        # The approved library versions are untouched; only the manifest that selects what the
        # restore installs changed, which must invalidate the exports.
        (self.repo / "vcpkg.json").write_text('{"name": "unpackrat", "x": 1}\n', encoding="utf-8")
        with self.assertRaises(licenses.LicenseError):
            self._prepare_now()

    def test_prepare_rejects_a_recipe_changed_after_export(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        recipe = self.repo / "build" / "vcpkg" / "ports" / "x" / "portfile.cmake"
        recipe.parent.mkdir(parents=True, exist_ok=True)
        recipe.write_bytes(b"set(ENABLE OFF)\n")
        tracked = ("vcpkg.json", "build/vcpkg/ports/x/portfile.cmake")
        with mock.patch.object(licenses, "_git_tracked", return_value=tracked):
            with self.assertRaises(licenses.LicenseError):
                self._prepare_now()


class OwnSourceCoverage(_PipelineBase):
    """Executable test code is scanned; only the deliberate poison/data fixtures are excluded."""

    REPOSITORY = Path(__file__).resolve().parents[2]

    def _component(self) -> dict:
        document = json.loads(
            (self.REPOSITORY / "build" / "license-inventory.json").read_text(encoding="utf-8")
        )
        return next(item for item in document["components"] if item.get("kind") == "own-source")

    def test_a_new_tracked_helper_under_build_tests_reaches_the_artifact(self) -> None:
        target = self.repo / "build" / "tests" / "new_helper.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"# released under GPL-3.0-or-later\n")
        with mock.patch.object(
            licenses, "_git_tracked", return_value=("build/tests/new_helper.py",)
        ):
            gathered = licenses._own_source_component(self._component(), self.repo, set())
        self.assertIn("build/tests/new_helper.py", gathered["shareData"])

    def test_deliberate_poison_fixtures_stay_excluded(self) -> None:
        fixture = "build/tests/fixtures/ort-smoke/forbidden-gpl/payload/extra_parser.c"
        target = self.repo / fixture
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"/* SPDX-License-Identifier: GPL-3.0-only */\n")
        with mock.patch.object(licenses, "_git_tracked", return_value=(fixture,)):
            gathered = licenses._own_source_component(self._component(), self.repo, set())
        self.assertEqual(gathered["shareData"], {})


if __name__ == "__main__":
    unittest.main()

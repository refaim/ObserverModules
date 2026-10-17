"""Strict content contracts for the retained third-party notice texts.

These tests pin the observable attribution/packaging contract that the 2026-10-16
license audit correction establishes. They assert exact notice wording that was
verified against the selected upstream sources under ``out/license-audit/`` and never
mirror an internal constant:

* ``licenses/vcpkg.txt`` carries Microsoft's full MIT grant for the copied
  microsoft/vcpkg Catch2 overlay recipe and patch (source/build attribution only);
* ``licenses/libspng.txt`` keeps the BSD-2-Clause grant and adds the exact
  libpng-2.0 author/grant block from ``spng/spng.c``;
* ``licenses/zstd.txt`` keeps the root BSD-3-Clause and GPLv2 alternatives and adds
  the historical BSD-2-Clause / Yann Collet notices from the selected legacy sources;
* ``licenses/dr_libs.txt`` (and its synced port ``copyright``) records the 2023
  David Reid date, keeps both license alternatives and records the embedded
  minimp3 CC0 provenance without inventing an attribution condition;
* ``licenses/serde-pickle.txt`` keeps the selected MIT grant and records only the
  conservative Georg Brandl ``src/de.rs`` upstream reference, leaving the relation
  between our parser and that project unresolved instead of asserting a history;
* ``README.md`` keeps the grant for our own source and the release ZIP
  ``LICENSES.txt`` statement while omitting implementation detail and component
  tables; the actual retained notices and inventory are checked separately.
"""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
LICENSES = ROOT / "licenses"
README = ROOT / "README.md"

# The current required notice set covers shipped dependencies, adopted source,
# and release/build tooling. Historical IX build-tooling attribution was removed
# with its obsolete source and build references.
# A regression guard for "remove no retained grants"; new notices may be added.
RETAINED_NOTICES = {
    "bcdec.txt", "brotli.txt", "Catch2.txt", "dr_libs.txt", "fastlz.txt", "godot.txt",
    "GPL-3.0.txt", "json.txt", "libogg.txt", "libspng.txt", "mbedcrypto.txt",
    "Observer.txt", "qoa.txt", "rgssad.txt", "rpatool.txt", "serde-pickle.txt",
    "xxHash.txt", "zlib-ng.txt", "zstd.txt", "zycore.txt", "zydis.txt",
    "vcpkg.txt",
}


def notice(name: str) -> str:
    return (LICENSES / name).read_text(encoding="utf-8")


class RetainedNoticeTests(unittest.TestCase):
    def test_required_current_notices_are_retained(self) -> None:
        present = {path.name for path in LICENSES.glob("*.txt")}
        self.assertEqual(RETAINED_NOTICES - present, set())

    def test_root_project_grants_are_unchanged(self) -> None:
        license_text = (ROOT / "LICENSE.txt").read_text(encoding="utf-8")
        self.assertTrue(license_text.startswith("MIT License"))
        self.assertIn("Copyright (c) 2019-2026 Roman Kharitonov", license_text)
        self.assertIn("Permission is hereby granted, free of charge", license_text)
        observer = notice("Observer.txt")
        self.assertIn("GNU LESSER GENERAL PUBLIC LICENSE", observer)
        gpl = notice("GPL-3.0.txt")
        self.assertIn("GNU GENERAL PUBLIC LICENSE", gpl)
        self.assertIn("Version 3, 29 June 2007", gpl)


class VcpkgSourceNoticeTests(unittest.TestCase):
    """The copied microsoft/vcpkg Catch2 overlay needs the upstream MIT grant."""

    def test_vcpkg_notice_reproduces_the_microsoft_mit_grant(self) -> None:
        text = notice("vcpkg.txt")
        self.assertIn("microsoft/vcpkg", text)
        self.assertIn("MIT License", text)
        self.assertIn("Copyright (c) Microsoft Corporation", text)
        self.assertIn("Permission is hereby granted, free of charge", text)
        self.assertIn(
            "The above copyright notice and this permission notice shall be included in all copies",
            text,
        )
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND', text)

    def test_vcpkg_notice_describes_the_adapted_recipe_without_local_paths(self) -> None:
        # Review correction: the overlay recipe is an adaptation of the official
        # recipe (Catch2 3.16.0 -> 3.16.1 plus the matching SHA512), the notice
        # must state that adaptation truthfully instead of claiming the bytes are
        # identical to upstream, and must not point at the local, unpublished
        # out/ audit tree.
        text = notice("vcpkg.txt")
        self.assertIn("adapted", text)
        self.assertIn("3.16.0", text)
        self.assertIn("3.16.1", text)
        self.assertNotIn("byte-identical", text)
        self.assertNotIn("out/license-audit", text)
        # The stable upstream baseline stays referenced.
        self.assertIn("20ceb8adbf0504d4b0d5e4058a32f9d4fc896391", text)
        self.assertIn("Permission is hereby granted, free of charge", text)


class LibSpngNoticeTests(unittest.TestCase):
    """libspng ships the BSD-2-Clause grant AND the embedded libpng-2.0 grant."""

    def test_libspng_keeps_bsd2_and_adds_the_libpng2_grant(self) -> None:
        text = notice("libspng.txt")
        self.assertIn("BSD 2-Clause License", text)
        self.assertIn("Copyright (c) 2018-2023, Randy <randy408@protonmail.com>", text)
        self.assertIn("PNG Reference Library License version 2", text)
        self.assertIn("Copyright (c) 1995-2019 The PNG Reference Library Authors.", text)
        self.assertIn("Copyright (c) 2018-2019 Cosmin Truta.", text)
        self.assertIn("Copyright (c) 2000-2002, 2004, 2006-2018 Glenn Randers-Pehrson.", text)
        self.assertIn("Copyright (c) 1996-1997 Andreas Dilger.", text)
        self.assertIn("Copyright (c) 1995-1996 Guy Eric Schalnat, Group 42, Inc.", text)
        self.assertIn("This Copyright notice may not be removed or altered from any", text)
        self.assertIn("Altered source versions must be plainly marked as such", text)


class ZstdLegacyNoticeTests(unittest.TestCase):
    """The selected legacy sources carry historical BSD-2-Clause grants."""

    def test_zstd_keeps_the_root_alternatives(self) -> None:
        text = notice("zstd.txt")
        self.assertIn("ZSTD is dual licensed under BSD and GPLv2.", text)
        self.assertIn("For Zstandard software", text)
        self.assertIn("Neither the name Facebook, nor Meta, nor the names of its contributors", text)
        self.assertIn("GNU GENERAL PUBLIC LICENSE", text)
        self.assertIn("Version 2, June 1991", text)

    def test_zstd_adds_the_historical_legacy_bsd2_notices(self) -> None:
        text = notice("zstd.txt")
        self.assertIn("BSD 2-Clause License (https://opensource.org/licenses/bsd-license.php)", text)
        # zstd_v01.c, v02.c and v03.c: the "zstd - standard compression library" grant.
        self.assertIn("Copyright (C) 2014-2015, Yann Collet.", text)
        # zstd_v02/03/06/07: the embedded mem.h low-level memory access notice.
        self.assertIn("low-level memory access routines", text)
        self.assertIn("Copyright (C) 2013-2015, Yann Collet.", text)
        # zstd_v04.c: a distinct buffered/decompression block and its exact wording.
        self.assertIn("zstd - decompression module fo v0.4 legacy format", text)
        self.assertIn("Buffered version of Zstd compression library", text)
        # zstd_v05/06/07: the 2013-2016 and 2015-2016 portions.
        self.assertIn("Copyright (C) 2013-2016, Yann Collet.", text)
        self.assertIn("Copyright (C) 2015-2016, Yann Collet.", text)
        # The historical notices must be labelled as legacy sources, not one XXH32 block.
        self.assertNotIn("XXH32", text)


class DrLibsNoticeTests(unittest.TestCase):
    """dr_libs records the 2023 date, both alternatives and the minimp3 provenance."""

    def test_dr_libs_notice_matches_the_selected_headers(self) -> None:
        text = notice("dr_libs.txt")
        self.assertIn("Copyright 2023 David Reid", text)
        self.assertNotIn("Copyright 2020 David Reid", text)
        self.assertIn("ALTERNATIVE 1 - Public Domain (www.unlicense.org)", text)
        self.assertIn("ALTERNATIVE 2 - MIT No Attribution", text)
        self.assertIn("lieff/minimp3", text)
        self.assertIn("creativecommons.org/publicdomain/zero/1.0", text)
        self.assertIn("dedicated all copyright", text)
        # No invented attribution condition: the notice keeps the actual CC0 wording.
        self.assertNotIn("must attribute", text.lower())
        self.assertNotIn("attribution is required", text.lower())

    def test_drlibs_port_copyright_is_synced_with_the_packaged_notice(self) -> None:
        port = ROOT / "build" / "vcpkg" / "ports" / "drlibs" / "copyright"
        self.assertEqual(port.read_bytes(), (LICENSES / "dr_libs.txt").read_bytes())


class SerdePickleNoticeTests(unittest.TestCase):
    """serde-pickle keeps the selected MIT grant and a conservative attribution note."""

    def test_serde_pickle_keeps_mit_and_records_the_georg_brandl_reference(self) -> None:
        text = notice("serde-pickle.txt")
        self.assertIn("Copyright (c) 2014 The Rust Project Developers", text)
        self.assertIn("Permission is hereby granted, free of charge", text)
        self.assertIn("Georg Brandl", text)
        self.assertIn("src/de.rs", text)
        # Conservative wording: a reference, not a claimed copied implementation.
        self.assertIn("reference", text.lower())
        self.assertNotIn("unpackrat is licensed", text.lower())
        self.assertNotIn("apache-only", text.lower())

    def test_serde_pickle_records_only_the_conservative_attribution(self) -> None:
        # Review correction: the notice must not claim a developmental history for
        # our parser (no first-party-from-documented-protocol story and no copying
        # denial). It records only the upstream MIT OR Apache grant and the exact,
        # contiguous Georg Brandl src/de.rs reference, and leaves the relation
        # between our parser and that project unresolved.
        text = notice("serde-pickle.txt")
        self.assertIn("MIT OR Apache-2.0", text)
        self.assertIn('"Copyright (c) 2015-2024 Georg Brandl"', text)
        self.assertIn("src/de.rs", text)
        self.assertIn("unresolved", text)
        self.assertNotIn("first-party", text)
        self.assertNotIn("documented Pickle protocol", text)
        self.assertNotIn("written from", text)
        self.assertNotIn("informed by", text)
        self.assertNotIn("not a claim that", text)
        self.assertNotIn("does not relicense", text)


class ReadmeLicenseSectionTests(unittest.TestCase):
    """The README states the minimal own-source grant and defers notice inventory."""

    def test_readme_drops_the_hand_maintained_component_tables(self) -> None:
        readme = README.read_text(encoding="utf-8")
        self.assertNotIn("| Project | License | Modules |", readme)
        self.assertNotIn("### Third-party components", readme)
        self.assertNotIn("### Sources of inspiration", readme)

    def test_readme_keeps_the_root_grant_for_own_source(self) -> None:
        readme = README.read_text(encoding="utf-8")
        self.assertIn("[MIT License](LICENSE.txt)", readme)
        self.assertIn("unpackrat's own source", readme)
        self.assertNotIn("One upstream file is an explicit exception", readme)

    def test_readme_omits_the_audit_implementation_paragraph(self) -> None:
        # The README omits audit implementation details, documented instead in
        # ``docs/build-system.md``.
        readme = README.read_text(encoding="utf-8")
        self.assertNotIn("Every retained upstream notice lives in", readme)
        self.assertNotIn("build/license-inventory.json", readme)
        self.assertNotIn("OSS Review Toolkit", readme)
        self.assertNotIn("publishes a consolidated", readme)
        self.assertNotIn("](licenses)", readme)
        docs = (ROOT / "docs" / "build-system.md").read_text(encoding="utf-8")
        self.assertIn("build/license-inventory.json", docs)
        self.assertIn("OSS Review Toolkit", docs)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

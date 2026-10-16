"""The verified source artifact must scan every real grant file in the tree.

The reviewed own-source component used to drop whole filename extensions
(``*.md``, ``*.txt``, ``*.json``, ``*.yml``) from the verified source.  Because
the matcher runs ``fnmatch`` on the full repository-relative path, that silently
removed source-tree grants and build scripts: a new
``src/<vendor>/LICENSE.txt``, ``CMakeLists.txt`` or ``LICENSE.md`` never reached
the scanned artifact and only the root metadata survived.

These tests pin the repaired membership against the real reviewed inventory and
a controlled tracked-file list.  A temporary repository supplies the bytes and
the recorded smoke fixtures supply the copyleft payloads (never inlined here),
so a source grant, the project root grant and a retained CMake build script must
reach both ``shareData`` and the deterministic source archive.  The repair stays
data only: recognised repository-root metadata and documented in-tree
reference/fixture data keep their reviewed scope, while any new unrecognised
path still fails accounting until it is reviewed.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import licenses  # noqa: E402


REPOSITORY = Path(__file__).resolve().parents[2]
INVENTORY = REPOSITORY / "build" / "license-inventory.json"
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"


def _load_grant_payload_paths() -> dict[str, str]:
    """Read the two reused payload paths from their owned fixture data."""
    data = json.loads((FIXTURE_ROOT / "license_source_membership" / "payloads.json").read_text(encoding="utf-8"))
    return {name: data["paths"][name] for name in ("primary", "secondary")}


# The copyleft payload bytes are reused from the recorded smoke fixtures: this test file is
# itself scanned source, so no path or license declaration is written literally into it. The
# two reused payload paths are test inputs and live in owned fixture data (payloads.json).
_GRANT_PAYLOAD_PATHS = _load_grant_payload_paths()
VENDOR_GRANT_FIXTURE = FIXTURE_ROOT / _GRANT_PAYLOAD_PATHS["primary"]
SECOND_GRANT_FIXTURE = FIXTURE_ROOT / _GRANT_PAYLOAD_PATHS["secondary"]


def _own_source_component() -> dict:
    document = json.loads(INVENTORY.read_text(encoding="utf-8"))
    return next(item for item in document["components"] if item.get("kind") == "own-source")


class OwnSourceMembershipData(unittest.TestCase):
    """The inventory data itself must not carry a blanket extension exclusion."""

    def setUp(self) -> None:
        self.component = _own_source_component()

    def test_no_blanket_extension_exclusion_remains(self) -> None:
        exclude = self.component["exclude"]
        for blanket in ("*.md", "*.txt", "*.json", "*.yml"):
            with self.subTest(pattern=blanket):
                self.assertNotIn(blanket, exclude)

    def test_source_grants_and_build_scripts_are_not_excluded(self) -> None:
        exclude = self.component["exclude"]
        for path in (
            "src/new_vendor/LICENSE.txt",
            "src/new_vendor/LICENSE.md",
            "src/new_vendor/LICENSE.json",
            "src/new_vendor/LICENSE.yml",
            "src/new_vendor/CMakeLists.txt",
            "src/modules/godot/ChangeLog.en.txt",
            "build/ubsan-ignorelist.txt",
        ):
            with self.subTest(path=path):
                self.assertFalse(licenses._matches_any(path, exclude))

    def test_the_root_grant_is_included_and_prefix_exclusions_are_preserved(self) -> None:
        self.assertIn("LICENSE.txt", self.component["include"])
        for preserved in (".github/*", ".ort/*", "docs/*", "licenses/*", "build/tests/fixtures/*"):
            with self.subTest(pattern=preserved):
                self.assertIn(preserved, self.component["exclude"])


class OwnSourceArtifactMembership(unittest.TestCase):
    """A controlled tracked-file list drives membership from the real inventory."""

    def test_the_reused_grant_fixtures_are_named_by_owned_fixture_data(self) -> None:
        # The two reused payload paths are test *inputs*, so they live in owned fixture data
        # instead of being restated as literals in this scanned test source. The fixture data
        # is the single source of truth and must resolve to real files under the fixture root.
        path_map = _load_grant_payload_paths()
        self.assertEqual(set(path_map), {"primary", "secondary"})
        resolved = {name: (FIXTURE_ROOT / relative) for name, relative in path_map.items()}
        self.assertEqual(len(set(resolved.values())), len(resolved))
        for name, path in resolved.items():
            with self.subTest(fixture=name):
                self.assertTrue(path.is_file())

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self._temporary.name)
        self.component = _own_source_component()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _write(self, relative: str, data: bytes) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _gather(self, members: dict[str, bytes], tracked: tuple[str, ...] | None = None):
        for relative, data in members.items():
            self._write(relative, data)
        order = tuple(sorted(members)) if tracked is None else tracked
        with mock.patch.object(licenses, "_git_tracked", return_value=order):
            return licenses._own_source_component(self.component, self.repo, set())

    def _grant_members(self) -> dict[str, bytes]:
        vendor_grant = VENDOR_GRANT_FIXTURE.read_bytes()
        second_grant = SECOND_GRANT_FIXTURE.read_bytes()
        return {
            "src/new_vendor/plain.cpp": b"int plain_source_symbol;\n",
            "src/new_vendor/CMakeLists.txt": (
                b"add_library(new_vendor STATIC plain.cpp)\n"
            ),
            "src/new_vendor/LICENSE.txt": vendor_grant,
            "src/new_vendor/LICENSE.md": second_grant,
            "src/new_vendor/LICENSE.json": second_grant,
            "src/new_vendor/LICENSE.yml": vendor_grant,
            "LICENSE.txt": (REPOSITORY / "LICENSE.txt").read_bytes(),
        }

    def test_new_source_grants_and_build_script_reach_the_artifact(self) -> None:
        members = self._grant_members()
        component = self._gather(members)
        self.assertEqual(set(component["shareData"]), set(members))
        for relative, data in members.items():
            with self.subTest(path=relative):
                self.assertEqual(component["shareData"][relative], data)

    def test_the_source_archive_contains_the_source_grants(self) -> None:
        members = self._grant_members()
        component = self._gather(members)
        archive = licenses.deterministic_tar(component["shareData"])
        self.assertEqual(archive, licenses.deterministic_tar(members))
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
            self.assertEqual(sorted(handle.getnames()), sorted(members))

    def test_reviewed_metadata_and_fixtures_stay_accounted_not_scanned(self) -> None:
        metadata = {
            "README.md": b"# project\n",
            "AGENTS.md": b"# agents\n",
            ".ort.yml": b"---\n",
            "codecov.yml": b"---\n",
            "vcpkg.json": b"{}\n",
            "vcpkg-configuration.json": b"{}\n",
            "build/license-inventory.json": b"{}\n",
            "build/vcpkg/ports/README.md": b"# ports\n",
            "build/vcpkg/ports/zlib/vcpkg.json": b"{}\n",
            "src/tests/data/godot/README.md": b"# fixture\n",
            "src/tests/data/renpy/README.md": b"# fixture\n",
            "src/tests/data/godot/converted-size-manifest.json": b"{}\n",
            "src/tests/data/godot/texture-media-repro.txt": b"repro notes\n",
        }
        component = self._gather(metadata)
        self.assertEqual(component["shareData"], {})

    def test_a_new_unrecognised_root_file_still_fails_closed(self) -> None:
        self._write("HISTORY.md", b"# history\n")
        with mock.patch.object(licenses, "_git_tracked", return_value=("HISTORY.md",)):
            with self.assertRaises(licenses.LicenseError):
                licenses._own_source_component(self.component, self.repo, set())


if __name__ == "__main__":
    unittest.main()

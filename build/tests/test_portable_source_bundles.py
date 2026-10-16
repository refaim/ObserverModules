"""Behavioral tests for the portable, deterministic project-generated source bundles.

The real Windows and Linux gzip streams differ for the same logical members even
though the decompressed tar bytes are identical, so a gzip-wrapped bundle is not a
portable content address.  ``deterministic_tar`` must therefore emit an
*uncompressed* POSIX tar: only then does the content hash match across platforms.

These tests pin the target contract before the production change.  They assert that
``deterministic_tar`` returns a strict uncompressed TAR and that the three
project-generated bundle routes (adopted-source, verified share data and raw
resources) advertise the ``.tar`` extension, while a single downloaded upstream
archive keeps its exact opaque bytes and its own extension.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
import sys
import tarfile
import unittest


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import licenses  # noqa: E402


GZIP_MAGIC = b"\x1f\x8b"


def _sha512(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def _no_fetch(url: str) -> bytes:
    raise AssertionError(f"unexpected fetch of {url}")


class PortableSourceBundles(unittest.TestCase):
    """The project-generated bundles must be strict uncompressed TARs."""

    def _assert_strict_uncompressed_tar(self, data: bytes, names: list[str]) -> None:
        self.assertFalse(
            data.startswith(GZIP_MAGIC),
            "a project-generated bundle must be an uncompressed TAR, not a gzip stream",
        )
        # mode "r:" is strict uncompressed tar; a gzip-wrapped stream raises ReadError.
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            infos = archive.getmembers()
            self.assertEqual([info.name for info in infos], names)
            for info in infos:
                self.assertTrue(info.isfile())
                self.assertEqual(info.uid, 0)
                self.assertEqual(info.gid, 0)
                self.assertEqual(info.uname, "")
                self.assertEqual(info.gname, "")
                self.assertEqual(info.mtime, 0)

    def test_deterministic_tar_is_a_strict_uncompressed_tar(self) -> None:
        members = {"b.txt": b"beta\n", "a.txt": b"alpha\n"}
        data = licenses.deterministic_tar(members)
        self._assert_strict_uncompressed_tar(data, ["a.txt", "b.txt"])
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            payloads = {
                info.name: archive.extractfile(info).read()
                for info in archive.getmembers()
            }
        self.assertEqual(payloads, members)

    def test_source_bytes_adopted_source_is_a_strict_uncompressed_tar(self) -> None:
        component = {
            "name": "vendor-lib",
            "kind": "adopted-source",
            "shareData": {"vendor-lib/header.h": b"int x;\n"},
        }
        data, extension = licenses._source_bytes(component, _no_fetch)
        self.assertEqual(extension, ".tar")
        self._assert_strict_uncompressed_tar(data, ["vendor-lib/header.h"])

    def test_source_bytes_verified_share_data_is_a_strict_uncompressed_tar(self) -> None:
        payload = b"helper body\n"
        component = {
            "name": "helper",
            "kind": "vcpkg-package",
            "shareData": {"tool.cmake": payload},
            "shareFiles": [
                {"path": "tool.cmake", "sha256": hashlib.sha256(payload).hexdigest()}
            ],
        }
        data, extension = licenses._source_bytes(component, _no_fetch)
        self.assertEqual(extension, ".tar")
        self._assert_strict_uncompressed_tar(data, ["helper/tool.cmake"])

    def test_fetch_source_raw_header_is_a_strict_uncompressed_tar(self) -> None:
        payload = b"# vendor header\n"
        url = "https://raw.githubusercontent.com/vendor/lib/abc123/vendor.h"
        resources = [{"name": "vendor.h", "url": url, "sha512": _sha512(payload)}]
        data, extension = licenses._fetch_source(
            {"name": "vendor"}, resources, lambda _url: payload
        )
        self.assertEqual(extension, ".tar")
        self._assert_strict_uncompressed_tar(data, ["vendor.h"])

    def test_fetch_source_single_upstream_archive_is_preserved_verbatim(self) -> None:
        payload = GZIP_MAGIC + b"\x08\x00 upstream tar.gz bytes\n"
        url = "https://github.com/vendor/lib/archive/v1.tar.gz"
        resources = [{"name": "lib", "url": url, "sha512": _sha512(payload)}]
        data, extension = licenses._fetch_source(
            {"name": "lib"}, resources, lambda _url: payload
        )
        self.assertEqual(data, payload)
        self.assertEqual(extension, ".tar.gz")


if __name__ == "__main__":
    unittest.main()

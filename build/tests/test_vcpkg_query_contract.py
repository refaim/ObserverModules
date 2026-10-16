"""Regression contract for the vcpkg JSON query boundary.

The pinned tool built from ``microsoft/vcpkg-tool`` at
``98d7cb0cf1f4686a3e43aa5672b6230c1d56bce8`` ships an experimental
``x-package-info`` command whose handler writes the complete ``results`` JSON to
stdout and then returns from ``command_package_info_and_exit`` without an exit
success.  The process therefore exits 1 on a fully successful query, with
*nothing* on stderr and a parseable body.

The license collector must accept exactly that success shape for this one query
while staying fail-closed on every other failure mode: an unrelated non-zero
exit, any stderr diagnostic, an unparseable body, a missing binary, and any
malformed or incomplete payload downstream.  The plain ``vcpkg list`` query
keeps its strict exit-0 contract.

No license text is inlined here: the payloads are reused verbatim from the
recorded ``fixtures/vcpkg-json`` documents so this test file stays clean source.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import licenses  # noqa: E402


JSON_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "vcpkg-json"


def _fixture_text(name: str) -> str:
    return (JSON_FIXTURES / name).read_text(encoding="utf-8")


class InstalledInfoExitContract(unittest.TestCase):
    """``x-package-info`` exits 1 on success; the collector accepts only that."""

    def _query(self, **run_kwargs):
        with mock.patch.object(licenses.subprocess, "run", **run_kwargs):
            return licenses._vcpkg_installed_info_json(
                ["vcpkg", "x-package-info", "--x-installed", "--x-json"],
                Path("repo"),
            )

    def test_accepts_the_complete_body_of_a_successful_exit_one(self) -> None:
        payload = _fixture_text("vcpkg-installed-info.json")
        error = subprocess.CalledProcessError(1, "vcpkg", output=payload, stderr="")
        document = self._query(side_effect=error)
        self.assertEqual(document, json.loads(payload))

    def test_accepts_an_ordinary_zero_exit(self) -> None:
        payload = _fixture_text("vcpkg-installed-info.json")
        document = self._query(return_value=mock.Mock(stdout=payload))
        self.assertEqual(document, json.loads(payload))

    def test_rejects_an_unrelated_nonzero_exit(self) -> None:
        error = subprocess.CalledProcessError(
            2, "vcpkg", output=_fixture_text("vcpkg-installed-info.json"), stderr=""
        )
        with self.assertRaises(licenses.LicenseError):
            self._query(side_effect=error)

    def test_rejects_exit_one_with_a_stderr_diagnostic(self) -> None:
        error = subprocess.CalledProcessError(
            1, "vcpkg", output=_fixture_text("vcpkg-installed-info.json"), stderr="boom"
        )
        with self.assertRaises(licenses.LicenseError):
            self._query(side_effect=error)

    def test_rejects_exit_one_with_a_malformed_body(self) -> None:
        error = subprocess.CalledProcessError(1, "vcpkg", output="{", stderr="")
        with self.assertRaises(licenses.LicenseError):
            self._query(side_effect=error)

    def test_rejects_a_malformed_body_on_an_ordinary_exit(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            self._query(return_value=mock.Mock(stdout="{"))

    def test_reports_a_missing_vcpkg_binary(self) -> None:
        with self.assertRaises(licenses.LicenseError):
            self._query(side_effect=FileNotFoundError())

    def test_list_query_stays_fail_closed_on_a_nonzero_exit(self) -> None:
        error = subprocess.CalledProcessError(1, "vcpkg", stderr="")
        with mock.patch.object(licenses.subprocess, "run", side_effect=error):
            with self.assertRaises(licenses.LicenseError):
                licenses._vcpkg_json(["vcpkg", "list", "--x-json"], Path("repo"))


class CollectorExitContract(unittest.TestCase):
    """The collector threads the accepted exit-1 body into the strict join."""

    def _collect(self, info_side_effect):
        listing = _fixture_text("vcpkg-installed-list.json")
        side_effect = [mock.Mock(stdout=listing), info_side_effect]
        with mock.patch.object(licenses.subprocess, "run", side_effect=side_effect) as run:
            return licenses.vcpkg_collector("vcpkg")(Path("root"), Path("repo")), run

    def test_joins_the_exit_one_info_into_the_installed_set(self) -> None:
        info = _fixture_text("vcpkg-installed-info.json")
        error = subprocess.CalledProcessError(1, "vcpkg", output=info, stderr="")
        (listing_doc, info_doc), run = self._collect(error)
        packages = licenses.parse_installed(listing_doc, info_doc)
        self.assertEqual(
            {package.name for package in packages},
            {"bcdec", "brotli", "vcpkg-cmake", "vcpkg-cmake-config"},
        )
        self.assertEqual(run.call_args_list[1].args[0][1], "x-package-info")

    def test_fails_closed_when_the_exit_one_body_is_incomplete(self) -> None:
        info = _fixture_text("vcpkg-installed-info.json")
        error = subprocess.CalledProcessError(1, "vcpkg", output=info, stderr="boom")
        with self.assertRaises(licenses.LicenseError):
            self._collect(error)

    def test_fails_closed_on_a_missing_abi_after_an_accepted_exit_one(self) -> None:
        info = json.loads(_fixture_text("vcpkg-installed-info.json"))
        del info["results"]["bcdec:unpackrat-x64-windows-static"]["abi"]
        error = subprocess.CalledProcessError(1, "vcpkg", output=json.dumps(info), stderr="")
        (listing_doc, info_doc), _ = self._collect(error)
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed(listing_doc, info_doc)

    def test_fails_closed_on_an_extra_spec_after_an_accepted_exit_one(self) -> None:
        info = json.loads(_fixture_text("vcpkg-installed-info.json"))
        info["results"]["ghost:x64-windows"] = {"triplet": "x64-windows", "abi": "a" * 64}
        error = subprocess.CalledProcessError(1, "vcpkg", output=json.dumps(info), stderr="")
        (listing_doc, info_doc), _ = self._collect(error)
        with self.assertRaises(licenses.LicenseError):
            licenses.parse_installed(listing_doc, info_doc)


if __name__ == "__main__":
    unittest.main()

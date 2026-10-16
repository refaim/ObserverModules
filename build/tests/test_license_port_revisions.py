"""Regression contract for port-revisioned installed packages in the license export.

The pinned vcpkg-tool records the installed port revision inside the port's SBOM
``versionInfo`` as the full ``base#port`` identity; a real restore reports
``1.3.2#1`` for zlib 1.3.2 port 1 and ``3.12.0#2`` for nlohmann-json 3.12.0 port 2.
``vcpkg list --x-json`` instead reports the bare ``version`` plus a separate
``port_version``.  The export must therefore bind the authoritative raw SBOM to the
full identity while recording the canonical base, and reloading the export must
reject any record whose base or revision the raw SBOM contradicts.

The installed-set helpers and recorded JSON fixtures of ``test_license_pipeline``
supply the adapter filesystem and the fake vcpkg boundary; no license text is
inlined here.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


BUILD_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(BUILD_ROOT))

import licenses  # noqa: E402
import test_license_pipeline as pipeline  # noqa: E402


REVISION_PACKAGE = "brotli"


class _PortRevisionBase(pipeline._PipelineBase):
    """A synthetic installed set in which one recorded package carries a port revision."""

    def _revision_entries(self, architecture: str, *, port: int = 1, sbom_port: int | None = None):
        """Return the installed set, its canonical base and its full identity.

        ``port`` is the revision the ``vcpkg list`` boundary reports; ``sbom_port`` is the
        revision the raw SBOM records (defaulting to the same one), so a test can make the
        two disagree or drop the SBOM's revision entirely.
        """

        entries = pipeline._entries(licenses.target_triplet(architecture))
        entry = next(item for item in entries if item["name"] == REVISION_PACKAGE)
        base = str(entry["version"])
        full = base if port == 0 else f"{base}#{port}"
        recorded_port = port if sbom_port is None else sbom_port
        entry["port_version"] = port
        for package in entry["sbom"]["packages"]:
            if package["SPDXID"] == "SPDXRef-port":
                package["versionInfo"] = base if recorded_port == 0 else f"{base}#{recorded_port}"
        return entries, base, full

    def _export_tree(self, architecture: str, entries) -> Path:
        pipeline._materialize(licenses.vcpkg_root(self.repo, architecture), entries)
        return licenses.export_inputs(
            self.repo, architecture, self._inputs() / architecture,
            collect=pipeline._collector(entries),
        )

    def _inputs(self) -> Path:
        return Path(self._tmp.name) / "inputs"

    def _manifest_package(self, architecture: str) -> dict:
        document = json.loads(
            (self._inputs() / architecture / "installed.json").read_text(encoding="utf-8")
        )
        return next(item for item in document["packages"] if item["name"] == REVISION_PACKAGE)

    def _export_revision_tree(self, architecture: str, **kwargs) -> tuple[str, str]:
        entries, base, full = self._revision_entries(architecture, **kwargs)
        self._export_tree(architecture, entries)
        return base, full

    def _prepare_spdx(self) -> dict:
        output = Path(self._tmp.name) / "prepared"
        licenses.prepare(
            self._inputs(), output, inventory_path=self.inventory_path,
            repository=self.repo, fetch=pipeline._fetch,
        )
        return json.loads((output / "unpackrat.spdx.json").read_text(encoding="utf-8"))

    def _prepared_curations(self) -> list:
        text = (Path(self._tmp.name) / "prepared" / "package-curations.yml").read_text(
            encoding="utf-8"
        )
        return json.loads(text)


class PortRevisionExport(_PortRevisionBase):
    """A genuine ``base#port`` install exports with both identities and reloads cleanly."""

    def test_export_records_the_base_and_binds_the_full_identity(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            base, full = self._export_revision_tree(architecture)
            record = self._manifest_package(architecture)
            self.assertEqual(record["version"], base)
            self.assertEqual(record["portVersion"], 1)
            self.assertEqual(record["fullVersion"], full)
            self.assertEqual(full, f"{base}#1")

    def test_exported_revision_reloads_through_load_export(self) -> None:
        base, full = self._export_revision_tree(licenses.ARCHITECTURES[0])
        self._export_revision_tree(licenses.ARCHITECTURES[1])
        document = licenses.load_export(self._inputs() / licenses.ARCHITECTURES[0])
        record = next(item for item in document["packages"] if item["name"] == REVISION_PACKAGE)
        self.assertEqual(record["version"], base)
        self.assertEqual(record["portVersion"], 1)
        self.assertEqual(record["fullVersion"], full)

    def test_prepare_publishes_the_port_revision(self) -> None:
        base, full = self._export_revision_tree(licenses.ARCHITECTURES[0])
        self._export_revision_tree(licenses.ARCHITECTURES[1])
        spdx = self._prepare_spdx()
        versions = {package["name"]: package["versionInfo"] for package in spdx["packages"]}
        self.assertEqual(versions[REVISION_PACKAGE], full)
        self.assertEqual(base, full.removesuffix("#1"))
        curations = self._prepared_curations()
        self.assertIn(
            f"SpdxDocumentFile:vcpkg:{REVISION_PACKAGE}:{full}",
            {entry["id"] for entry in curations},
        )


class PortRevisionExportRejection(_PortRevisionBase):
    """The export binds the raw SBOM to the full identity before anything is recorded."""

    def test_export_rejects_a_sbom_revision_that_differs_from_the_listing(self) -> None:
        entries, base, _ = self._revision_entries(licenses.ARCHITECTURES[0], port=1, sbom_port=9)
        with self.assertRaises(licenses.LicenseError):
            self._export_tree(licenses.ARCHITECTURES[0], entries)

    def test_export_rejects_a_listed_revision_the_sbom_omits(self) -> None:
        entries, base, _ = self._revision_entries(licenses.ARCHITECTURES[0], port=1, sbom_port=0)
        with self.assertRaises(licenses.LicenseError):
            self._export_tree(licenses.ARCHITECTURES[0], entries)


class PortRevisionReloadRejection(_PortRevisionBase):
    """A reloaded record may neither drop nor invent the revision the raw SBOM names."""

    def _reload_both(self, **fields) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export_revision_tree(architecture)
        self._rewrite_both(REVISION_PACKAGE, **fields)

    def test_reload_rejects_a_dropped_revision(self) -> None:
        base, _ = self._revision_entries(licenses.ARCHITECTURES[0])[1:]
        self._reload_both(portVersion=0, fullVersion=base)
        with self.assertRaises(licenses.LicenseError):
            self._prepare_spdx()

    def test_reload_rejects_an_invented_revision(self) -> None:
        base, _ = self._revision_entries(licenses.ARCHITECTURES[0])[1:]
        self._reload_both(portVersion=9, fullVersion=f"{base}#9")
        with self.assertRaises(licenses.LicenseError):
            self._prepare_spdx()

    def test_reload_rejects_a_base_that_absorbs_the_revision(self) -> None:
        base, full = self._revision_entries(licenses.ARCHITECTURES[0])[1:]
        self._reload_both(version=full, portVersion=0, fullVersion=full)
        with self.assertRaises(licenses.LicenseError):
            self._prepare_spdx()


class PortRevisionBareForgeryRejection(_PortRevisionBase):
    """A genuine bare-identity export cannot be reloaded after both manifests invent a revision."""

    def test_reload_rejects_an_invented_revision_over_a_bare_sbom(self) -> None:
        _, base, _ = self._revision_entries(licenses.ARCHITECTURES[0], port=0, sbom_port=0)
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        self._rewrite_both(REVISION_PACKAGE, portVersion=9, fullVersion=f"{base}#9")
        with self.assertRaises(licenses.LicenseError):
            self._prepare_spdx()


class PortZeroUnchanged(_PortRevisionBase):
    """A port-version-zero install keeps the exact bare identity and still prepares."""

    def test_zero_revision_keeps_the_bare_identity(self) -> None:
        for architecture in licenses.ARCHITECTURES:
            self._export(architecture)
        record = self._manifest_package(licenses.ARCHITECTURES[0])
        self.assertEqual(record["portVersion"], 0)
        self.assertEqual(record["fullVersion"], record["version"])
        spdx = self._prepare_spdx()
        versions = {package["name"]: package["versionInfo"] for package in spdx["packages"]}
        self.assertEqual(versions[REVISION_PACKAGE], record["version"])


if __name__ == "__main__":
    unittest.main()

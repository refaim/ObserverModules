from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from jinja2 import UndefinedError


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.render import TemplateRenderer  # noqa: E402


class VcpkgTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = TemplateRenderer(BUILD_ROOT / "templates")
        self.variables = {
            "name": "restore-vcpkg-x64",
            "pool": "slot",
            "inputs": [],
            "pwsh": "pwsh.exe",
            "vcpkg": r"C:\tools\vcpkg.exe",
            "repository": r"C:\repo with O'Brien",
            "triplet": "observer-x64-windows-static",
        }

    def test_manifest_restore_targets_node_output_and_requires_include_directory(self) -> None:
        recipe = json.loads(self.renderer.render("vcpkg.ps1", self.variables))
        script = recipe["script"]["data"]

        self.assertIn("Invoke-Checked 'C:\\tools\\vcpkg.exe' @(", script)
        self.assertIn("\n    'install'\n", script)
        self.assertIn('\n    "--x-install-root=$outDir"\n', script)
        self.assertIn("\n    '--triplet'\n    'observer-x64-windows-static'\n", script)
        self.assertIn("'--x-manifest-root=C:\\repo with O''Brien'", script)
        self.assertIn("'--overlay-triplets=C:\\repo with O''Brien\\build\\vcpkg\\triplets'", script)
        self.assertIn(
            "Test-Path -LiteralPath (Join-Path $outDir "
            "'observer-x64-windows-static\\include') -PathType Container",
            script,
        )
        self.assertIn("throw 'vcpkg restore did not produce the include directory'", script)

    def test_triplet_is_required(self) -> None:
        del self.variables["triplet"]

        with self.assertRaisesRegex(UndefinedError, "triplet.*undefined"):
            self.renderer.render("vcpkg.ps1", self.variables)


if __name__ == "__main__":
    unittest.main()

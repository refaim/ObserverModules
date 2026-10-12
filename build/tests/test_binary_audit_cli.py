"""Standalone worker tests for the ``core.binary_audit`` CLI dispatch.

These assertions were preserved from ``test_audit_graph.py`` when the retired
``graphs.audit`` composition was dropped.  They drive the real ``pe`` and
``binskim`` CLI paths, so the literal and dispatched-run behaviour stays covered.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.binary_audit import AuditError, main as binary_audit_main  # noqa: E402

class BinaryAuditCliTests(unittest.TestCase):
    def test_binary_audit_cli_dispatches_pe_binskim_and_literal_binskim_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            headers, dependents, exports = root / "headers.txt", root / "dependents.txt", root / "exports.txt"
            headers.write_text(" 8664 machine (x64)\n", encoding="utf-8")
            dependents.write_text(" KERNEL32.dll\n", encoding="utf-8")
            exports.write_text(
                " 1 0 0001 LoadSubModule\n 2 1 0002 UnloadSubModule\n", encoding="utf-8"
            )
            self.assertEqual(
                0,
                binary_audit_main(("pe", "x64", str(headers), str(dependents), str(exports))),
            )
            report = root / "approved.sarif"
            report.write_text(json.dumps({"runs": []}), encoding="utf-8")
            self.assertEqual(0, binary_audit_main(("binskim", str(report))))

            output = root / "out"
            output.mkdir()
            tool, binary = root / "BinSkim.exe", root / "renpy.so"
            tool.touch()
            binary.touch()

            def complete(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                Path(argv[argv.index("--output") + 1]).write_text('{"runs":[]}', encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0)

            with mock.patch.dict(os.environ, {"UNPACKRAT_OUT_DIR": str(output)}, clear=False):
                with mock.patch("core.binary_audit.subprocess.run", side_effect=complete) as invoked:
                    self.assertEqual(0, binary_audit_main(("run-binskim", str(tool), str(binary))))
            argv = invoked.call_args.args[0]
            self.assertEqual(argv[:3], [str(tool), "analyze", str(binary)])
            self.assertIn("--disable-telemetry", argv)
            self.assertEqual(output / "binskim.sarif", Path(argv[argv.index("--output") + 1]))

            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(AuditError, "UNPACKRAT_OUT_DIR"):
                    binary_audit_main(("run-binskim", str(tool), str(binary)))
            empty_output = root / "empty-output"
            empty_output.mkdir()
            with mock.patch.dict(os.environ, {"UNPACKRAT_OUT_DIR": str(empty_output)}, clear=False):
                with mock.patch("core.binary_audit.subprocess.run"):
                    with self.assertRaisesRegex(AuditError, "did not produce"):
                        binary_audit_main(("run-binskim", str(tool), str(binary)))


if __name__ == "__main__":
    unittest.main(verbosity=2)

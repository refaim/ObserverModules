from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.binary_audit import (  # noqa: E402
    AuditError,
    main as binary_audit_main,
    require_clean_binskim,
    require_release_pe,
)


class BinaryAuditTests(unittest.TestCase):
    def test_release_pe_requires_machine_static_dependencies_and_exact_exports(self) -> None:
        require_release_pe(
            "x64",
            " 8664 machine (x64)\n",
            "  KERNEL32.dll\n  api-ms-win-core-file-l1-1-0.dll\n",
            "  1 0 0001 LoadSubModule\n  2 1 0002 UnloadSubModule\n",
        )

        failures = (
            ("x86", " 8664 machine (x64)\n", " KERNEL32.dll\n", " 1 0 1 LoadSubModule\n 2 1 2 UnloadSubModule\n", "machine"),
            ("x64", " 8664 machine (x64)\n", " VCRUNTIME140.dll\n", " 1 0 1 LoadSubModule\n 2 1 2 UnloadSubModule\n", "dependencies"),
            ("x64", " 8664 machine (x64)\n", " KERNEL32.dll\n", " 1 0 1 LoadSubModule\n 2 1 2 Surprise\n", "exports"),
        )
        for architecture, headers, dependents, exports, message in failures:
            with self.subTest(message=message), self.assertRaisesRegex(AuditError, message):
                require_release_pe(architecture, headers, dependents, exports)

        with self.assertRaisesRegex(AuditError, "unsupported.*riscv64") as raised:
            require_release_pe("riscv64", "", "", "")
        self.assertIsInstance(raised.exception.__cause__, KeyError)
        with self.assertRaisesRegex(AuditError, "unsupported.*arm64"):
            require_release_pe(
                "arm64",
                " AA64 machine (ARM64)\n",
                " KERNEL32.dll\n",
                " 1 0 1 LoadSubModule\n 2 1 2 UnloadSubModule\n",
            )

    def test_release_pe_executable_accepts_a_self_contained_exe_without_exports(self) -> None:
        # The actual leak-probe.exe is an x64 /MT EXE whose dumpbin export table is empty.
        require_release_pe(
            "x64",
            " 8664 machine (x64)\n",
            "  KERNEL32.dll\n",
            "  Summary\n\n        4A000 .text\n",
            executable=True,
        )

    def test_release_pe_executable_still_rejects_any_export(self) -> None:
        for exports in (
            "  1 0 0001 LoadSubModule\n  2 1 0002 UnloadSubModule\n",
            "  1 0 0001 Renpy_ExtraExport\n",
        ):
            with self.subTest(exports=exports), self.assertRaisesRegex(AuditError, "unexpected exports"):
                require_release_pe(
                    "x64", " 8664 machine (x64)\n", "  KERNEL32.dll\n", exports,
                    executable=True,
                )

    def test_release_pe_default_module_contract_still_requires_exports(self) -> None:
        with self.assertRaisesRegex(AuditError, "unexpected exports"):
            require_release_pe("x64", " 8664 machine (x64)\n", "  KERNEL32.dll\n", "")

    def test_cli_pe_executable_flag_selects_the_executable_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            headers = root / "headers.txt"
            dependents = root / "dependents.txt"
            exports = root / "exports.txt"
            headers.write_text(" 8664 machine (x64)\n", encoding="utf-8")
            dependents.write_text("  KERNEL32.dll\n", encoding="utf-8")
            exports.write_text("  Summary\n\n        4A000 .text\n", encoding="utf-8")
            self.assertEqual(
                0,
                binary_audit_main((
                    "pe", "--executable", "x64",
                    str(headers), str(dependents), str(exports),
                )),
            )
            with self.assertRaisesRegex(AuditError, "unexpected exports"):
                binary_audit_main(("pe", "x64", str(headers), str(dependents), str(exports)))

    def test_binskim_allows_only_documented_warning(self) -> None:
        approved = {
            "runs": [{
                "tool": {"driver": {"rules": [
                    {"id": "BA2027", "defaultConfiguration": {"level": "warning"}}
                ]}},
                "results": [{"ruleId": "BA2027"}],
            }]
        }
        require_clean_binskim(approved)

        for result in (
            {"ruleId": "BA2001", "level": "warning"},
            {"ruleId": "BA2027", "level": "error"},
        ):
            document = {
                "runs": [{
                    "tool": {"driver": {"rules": []}},
                    "results": [result],
                }]
            }
            with self.subTest(result=result), self.assertRaisesRegex(AuditError, result["ruleId"]):
                require_clean_binskim(document)


if __name__ == "__main__":
    unittest.main(verbosity=2)

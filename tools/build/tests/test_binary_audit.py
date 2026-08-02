from __future__ import annotations

import sys
import unittest
from pathlib import Path


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.binary_audit import AuditError, require_clean_binskim, require_release_pe  # noqa: E402


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

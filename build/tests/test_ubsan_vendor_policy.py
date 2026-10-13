"""Keep the reviewed SDK exception narrower than first-party UBSan checks."""

from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MSBUILD = "{http://schemas.microsoft.com/developer/msbuild/2003}"


class UbsanVendorPolicyTests(unittest.TestCase):
    def test_only_ubsan_uses_the_exact_vendor_list_and_keeps_fatal_undefined_checks(self) -> None:
        project = ET.parse(REPOSITORY_ROOT / "build/unpackrat-project.props").getroot()
        options = project.findall(f".//{MSBUILD}ClCompile/{MSBUILD}AdditionalOptions")
        scoped = [node for node in options if "sanitize-ignorelist" in (node.text or "")]
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0].get("Condition"), "'$(Configuration)' == 'UBSan'")
        self.assertEqual(
            scoped[0].text,
            '/clang:-fsanitize=undefined /clang:-fno-sanitize-recover=all '
            '/clang:-fsanitize-ignorelist="$(RepositoryRoot)build\\ubsan-ignorelist.txt" '
            '%(AdditionalOptions)',
        )
        for node in options:
            if node.get("Condition") != "'$(Configuration)' == 'UBSan'":
                self.assertNotIn("sanitize-ignorelist", node.text or "")

    def test_vendor_list_exempts_only_alignment_in_the_exact_sdk_function(self) -> None:
        source = REPOSITORY_ROOT / "build/ubsan-ignorelist.txt"
        self.assertTrue(source.is_file(), "the UBSan exception must be explicit and reviewable")
        entries = [
            line.strip() for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(entries, ["[alignment]", "fun:wmemcmp"])

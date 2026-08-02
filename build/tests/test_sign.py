from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.sign import content_uid  # noqa: E402


class ContentUidTests(unittest.TestCase):
    def fields(self) -> dict[str, object]:
        return {
            "recipe": '{"script":"build"}\n',
            "inputs": {
                "src/archive.cpp": b"archive bytes\x00",
                "src/archive.h": b"header bytes\n",
            },
            "dependencies": {
                "vcpkg": "0123456789abcdef0123456789abcdef",
                "headers": "fedcba9876543210fedcba9876543210",
            },
            "toolchain": {"msvc": "19.44", "sdk": "10.0.26100.0"},
            "config": {"arch": "x64", "flags": ["/MT", "/W4"]},
        }

    def uid(self, **changes: object) -> str:
        fields = self.fields()
        fields.update(changes)
        return content_uid(**fields)  # type: ignore[arg-type]

    def test_uid_is_lowercase_md5(self) -> None:
        uid = self.uid()

        self.assertRegex(uid, re.compile(r"^[0-9a-f]{32}$"))

    def test_mapping_order_does_not_change_uid(self) -> None:
        fields = self.fields()

        self.assertEqual(
            content_uid(**fields),  # type: ignore[arg-type]
            content_uid(
                recipe=fields["recipe"],  # type: ignore[arg-type]
                inputs=dict(reversed(list(fields["inputs"].items()))),  # type: ignore[union-attr]
                dependencies=dict(
                    reversed(list(fields["dependencies"].items()))  # type: ignore[union-attr]
                ),
                toolchain={"sdk": "10.0.26100.0", "msvc": "19.44"},
                config={"flags": ["/MT", "/W4"], "arch": "x64"},
            ),
        )

    def test_every_signed_field_changes_uid(self) -> None:
        original = self.uid()
        changes = [
            {"recipe": '{"script":"test"}\n'},
            {"inputs": {"src/other.cpp": b"archive bytes\x00", "src/archive.h": b"header bytes\n"}},
            {"inputs": {"src/archive.cpp": b"changed\x00", "src/archive.h": b"header bytes\n"}},
            {"dependencies": {"other": "0123456789abcdef0123456789abcdef", "headers": "fedcba9876543210fedcba9876543210"}},
            {"dependencies": {"vcpkg": "11111111111111111111111111111111", "headers": "fedcba9876543210fedcba9876543210"}},
            {"toolchain": {"compiler": "19.44", "sdk": "10.0.26100.0"}},
            {"toolchain": {"msvc": "19.45", "sdk": "10.0.26100.0"}},
            {"config": {"architecture": "x64", "flags": ["/MT", "/W4"]}},
            {"config": {"arch": "ARM64", "flags": ["/MT", "/W4"]}},
            {"config": {"arch": "x64", "flags": ["/W4", "/MT"]}},
        ]

        for change in changes:
            with self.subTest(change=change):
                self.assertNotEqual(original, self.uid(**change))

    def test_binary_inputs_are_framed_without_concatenation_ambiguity(self) -> None:
        common = {
            "recipe": b"recipe",
            "dependencies": {},
            "toolchain": {},
            "config": {},
        }

        self.assertNotEqual(
            content_uid(inputs={"a": b"bc"}, **common),
            content_uid(inputs={"ab": b"c"}, **common),
        )

    def test_recipe_text_is_signed_as_exact_utf8_bytes(self) -> None:
        fields = self.fields()
        text_uid = content_uid(**fields)  # type: ignore[arg-type]
        fields["recipe"] = str(fields["recipe"]).encode("utf-8")

        self.assertEqual(text_uid, content_uid(**fields))  # type: ignore[arg-type]

    def test_noncanonical_inputs_and_dependencies_are_rejected(self) -> None:
        valid = self.fields()
        invalid_fields = [
            {"recipe": object()},
            {"inputs": {1: b"bytes"}},
            {"inputs": {"source": "text"}},
            {"dependencies": {1: "0123456789abcdef0123456789abcdef"}},
            {"dependencies": {"dep": 1}},
            {"dependencies": {"dep": "not-an-md5"}},
        ]

        for change in invalid_fields:
            fields = dict(valid)
            fields.update(change)
            with self.subTest(change=change), self.assertRaises((TypeError, ValueError)):
                content_uid(**fields)  # type: ignore[arg-type]

    def test_identity_is_json_and_rejects_ambiguous_values(self) -> None:
        valid = self.fields()
        valid["toolchain"] = {
            "enabled": True,
            "generation": 1,
            "ratio": 0.5,
            "optional": None,
            "tuple": ("a", "b"),
        }
        self.assertRegex(content_uid(**valid), re.compile(r"^[0-9a-f]{32}$"))  # type: ignore[arg-type]

        for toolchain in (
            {1: "value"},
            {"nested": {1: "ambiguous"}},
            {"invalid": object()},
            {"nan": float("nan")},
        ):
            fields = self.fields()
            fields["toolchain"] = toolchain
            with self.subTest(toolchain=toolchain), self.assertRaises((TypeError, ValueError)):
                content_uid(**fields)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

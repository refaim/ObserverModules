from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = BUILD_ROOT.parent
sys.path.insert(0, str(BUILD_ROOT))

from core.module_version import (  # noqa: E402
    MODULES,
    VERSION_PROPS,
    VERSION_RESOURCE,
    ModuleVersionError,
    binary_version,
    content_identity,
    main as module_version_main,
    parse_version,
    project_version_inputs,
    read_version,
    require_binary_version,
    shared_content_inputs,
    version_path,
)
from core.package import MODULES as PACKAGE_MODULES  # noqa: E402


SYSTEM_BINARY = Path("C:/Windows/System32/kernel32.dll")


class ModuleVersionFileTests(unittest.TestCase):
    def test_every_shipped_module_continues_its_published_lineage(self) -> None:
        self.assertEqual(MODULES, PACKAGE_MODULES)
        self.assertEqual(
            {module: read_version(REPOSITORY, module) for module in MODULES},
            {"renpy": "3.1.0", "rpgmaker": "1.1.0", "zanzarah": "2.1.0"},
        )
        for module in MODULES:
            with self.subTest(module=module):
                lines = (REPOSITORY / version_path(module)).read_text(encoding="ascii").splitlines()
                self.assertEqual(len(lines), 1)

    def test_module_sources_report_the_injected_version_instead_of_literals(self) -> None:
        for module in MODULES:
            with self.subTest(module=module):
                source = (REPOSITORY / f"src/modules/{module}/{module}.cpp").read_text(
                    encoding="utf-8"
                )
                body = source[source.index("version_info get_version_info"):]
                body = body[: body.index("\n    }")]
                self.assertIn("UNPACKRAT_MODULE_VERSION_MAJOR,", body)
                self.assertIn("UNPACKRAT_MODULE_VERSION_MINOR,", body)
                self.assertIn("build/unpackrat-module-version.props", source)
                # No hardcoded major/minor component may survive beside the injected macros.
                self.assertEqual([], re.findall(r"^\s+\d+,\s*$", body, re.MULTILINE))

    def test_version_inputs_follow_every_project_that_compiles_a_module(self) -> None:
        self.assertEqual(version_path("renpy"), "src/modules/renpy/VERSION")
        self.assertEqual((VERSION_PROPS, VERSION_RESOURCE), (
            "build/unpackrat-module-version.props", "src/modules/version.rc",
        ))
        # A shipped DLL also carries the shared resource; its fuzz target only recompiles the
        # same translation units, so it signs the VERSION file alone.
        self.assertEqual(
            project_version_inputs("zanzarah"),
            (VERSION_RESOURCE, "src/modules/zanzarah/VERSION"),
        )
        self.assertEqual(
            project_version_inputs("fuzz-renpy"), ("src/modules/renpy/VERSION",)
        )
        for project in ("tests", "leak-probe", "fuzz-pickle", "fuzz-", "renpy-fuzz"):
            with self.subTest(project=project):
                self.assertEqual(project_version_inputs(project), ())
        for name in (VERSION_PROPS, VERSION_RESOURCE):
            with self.subTest(name=name):
                self.assertTrue((REPOSITORY / name).is_file())
        with self.assertRaisesRegex(ModuleVersionError, "unknown module"):
            version_path("pickle")

    def test_shared_content_inputs_cover_the_version_and_dependency_plumbing(self) -> None:
        names = shared_content_inputs(REPOSITORY)

        self.assertEqual(names, tuple(sorted(set(names))))
        self.assertIn(VERSION_PROPS, names)
        self.assertIn(VERSION_RESOURCE, names)
        self.assertIn("vcpkg.json", names)
        self.assertIn("build/vcpkg/triplets/unpackrat-x64-windows-static.cmake", names)
        self.assertNotIn("vcpkg-configuration.json", names)
        for name in names:
            with self.subTest(name=name):
                self.assertTrue((REPOSITORY / name).is_file())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (VERSION_PROPS, VERSION_RESOURCE, "vcpkg.json",
                         "vcpkg-configuration.json", "build/vcpkg/ports/zlib/portfile.cmake"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x\n", encoding="utf-8")
            self.assertEqual(
                shared_content_inputs(root),
                (
                    VERSION_PROPS,
                    "build/vcpkg/ports/zlib/portfile.cmake",
                    VERSION_RESOURCE,
                    "vcpkg-configuration.json",
                    "vcpkg.json",
                ),
            )

    def test_versions_are_validated_as_bounded_three_component_semver(self) -> None:
        self.assertEqual(parse_version("3.1.0"), (3, 1, 0))
        self.assertEqual(parse_version("0.0.0"), (0, 0, 0))
        for invalid in ("3.1", "3.1.0.0", "03.1.0", "3.1.0-rc1", "", "v3.1.0", "3.1.x"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ModuleVersionError, "must be X.Y.Z"
            ):
                parse_version(invalid)
        with self.assertRaisesRegex(ModuleVersionError, "exceeds 65535"):
            parse_version("99999.0.0")

    def test_reading_a_missing_or_malformed_version_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ModuleVersionError, "unreadable"):
                read_version(root, "renpy")
            path = root / version_path("renpy")
            path.parent.mkdir(parents=True)
            path.write_text("  2.4.6\n\n", encoding="utf-8")
            self.assertEqual(read_version(root, "renpy"), "2.4.6")
            path.write_text("2.4\n", encoding="utf-8")
            with self.assertRaisesRegex(ModuleVersionError, "must be X.Y.Z"):
                read_version(root, "renpy")


class ContentIdentityTests(unittest.TestCase):
    def test_identity_covers_exact_bytes_and_names_regardless_of_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, content in (("a.cpp", "one\n"), ("b/c.h", "two\n")):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            first = content_identity(root, ("a.cpp", "b/c.h"))
            self.assertEqual(first, content_identity(root, ("b/c.h", "a.cpp", "a.cpp")))
            expected = hashlib.sha256()
            for name in ("a.cpp", "b/c.h"):
                digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
                expected.update(f"{name}\0{digest}\n".encode("utf-8"))
            self.assertEqual(first, expected.hexdigest())

            (root / "a.cpp").write_text("changed\n", encoding="utf-8")
            self.assertNotEqual(first, content_identity(root, ("a.cpp", "b/c.h")))
            (root / "renamed.cpp").write_text("changed\n", encoding="utf-8")
            self.assertNotEqual(
                content_identity(root, ("a.cpp", "b/c.h")),
                content_identity(root, ("renamed.cpp", "b/c.h")),
            )

    def test_identity_rejects_an_empty_set_and_the_version_file_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ModuleVersionError, "at least one input"):
                content_identity(root, ())
            with self.assertRaisesRegex(ModuleVersionError, "must exclude"):
                content_identity(root, ("src/modules/renpy/VERSION",))


class BinaryVersionTests(unittest.TestCase):
    def test_a_real_version_resource_is_read_as_two_numeric_quads(self) -> None:
        file_version, product_version = binary_version(SYSTEM_BINARY)

        self.assertEqual(len(file_version), 4)
        self.assertEqual(len(product_version), 4)
        self.assertTrue(all(0 <= item <= 0xFFFF for item in file_version))
        self.assertGreaterEqual(file_version[0], 10)

    def test_a_binary_without_a_version_resource_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "renpy.so"
            binary.write_bytes(b"not a portable executable")
            with self.assertRaisesRegex(ModuleVersionError, "no version resource"):
                binary_version(binary)
            with self.assertRaisesRegex(ModuleVersionError, "no version resource"):
                require_binary_version(binary, "3.1.0")

    def test_required_version_must_match_both_resource_quads_exactly(self) -> None:
        with self.assertRaisesRegex(ModuleVersionError, "version resource is"):
            require_binary_version(SYSTEM_BINARY, "3.1.0")

        with mock.patch(
            "core.module_version.binary_version",
            return_value=((3, 1, 0, 0), (3, 1, 0, 0)),
        ):
            self.assertIsNone(require_binary_version(SYSTEM_BINARY, "3.1.0"))
        for actual in (((3, 1, 1, 0), (3, 1, 0, 0)), ((3, 1, 0, 0), (3, 0, 0, 0))):
            with (
                self.subTest(actual=actual),
                mock.patch("core.module_version.binary_version", return_value=actual),
                self.assertRaisesRegex(ModuleVersionError, "version resource is"),
            ):
                require_binary_version(SYSTEM_BINARY, "3.1.0")


class ModuleVersionCommandTests(unittest.TestCase):
    def test_verify_command_gates_one_binary_and_requires_a_subcommand(self) -> None:
        with mock.patch(
            "core.module_version.binary_version",
            return_value=((1, 1, 0, 0), (1, 1, 0, 0)),
        ):
            self.assertEqual(0, module_version_main(("verify", "1.1.0", str(SYSTEM_BINARY))))


        with self.assertRaises(SystemExit):
            module_version_main(())


if __name__ == "__main__":
    unittest.main(verbosity=2)

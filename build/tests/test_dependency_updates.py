"""Updated dependencies retain static packaging and the Mbed TLS 4.1 LTS contract."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
PORTS = REPOSITORY / "build" / "vcpkg" / "ports"


class DependencyUpdateTests(unittest.TestCase):
    def test_overlay_patches_survive_a_fresh_git_checkout_without_crlf(self) -> None:
        patches = sorted(PORTS.rglob("*.patch"))
        self.assertGreaterEqual(len(patches), 2)
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / ".gitattributes").write_bytes((REPOSITORY / ".gitattributes").read_bytes())
            for patch in patches:
                copied = repository / patch.relative_to(REPOSITORY)
                copied.parent.mkdir(parents=True, exist_ok=True)
                copied.write_bytes(patch.read_bytes())
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "-c", "core.autocrlf=false", "add", "."],
                           cwd=repository, check=True, capture_output=True)
            checkout = repository / "checkout"
            subprocess.run(["git", "-c", "core.autocrlf=false", "checkout-index", "--all",
                            "--prefix=" + checkout.as_posix() + "/"],
                           cwd=repository, check=True, capture_output=True)
            paths = [patch.relative_to(REPOSITORY).as_posix() for patch in patches]
            attributes = subprocess.run(["git", "check-attr", "eol", "--", *paths],
                                        cwd=repository, check=True, capture_output=True, text=True)
            for relative in paths:
                with self.subTest(patch=relative):
                    checked_patch = checkout / relative
                    self.assertNotIn(b"\r", checked_patch.read_bytes())
                    self.assertIn(f"{relative}: eol: lf", attributes.stdout)
                    # Git's parser rejects blank hunk context after CRLF conversion.
                    subprocess.run(["git", "apply", "--numstat", str(checked_patch)],
                                   cwd=repository, check=True, capture_output=True)

    def test_manifest_selects_the_updated_dependencies_and_registry(self) -> None:
        manifest = json.loads((REPOSITORY / "vcpkg.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["builtin-baseline"], "20ceb8adbf0504d4b0d5e4058a32f9d4fc896391")
        dependencies = {dependency["name"]: dependency for dependency in manifest["dependencies"]}
        for name, version in (("catch2", "3.16.1"), ("xxhash", "0.8.4"), ("mbedtls", "4.1.1")):
            with self.subTest(dependency=name):
                self.assertEqual(dependencies[name]["version>="], version)
        configuration = json.loads((REPOSITORY / "vcpkg-configuration.json").read_text(encoding="utf-8"))
        self.assertIn("build/vcpkg/ports", configuration["overlay-ports"])

    def test_catch2_overlay_keeps_static_linkage_and_manual_main_selection(self) -> None:
        port = PORTS / "catch2"
        metadata = json.loads((port / "vcpkg.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["version-semver"], "3.16.1")
        self.assertEqual(metadata["name"], "catch2")
        self.assertEqual(metadata["dependencies"], [
            {"name": "vcpkg-cmake", "host": True},
            {"name": "vcpkg-cmake-config", "host": True},
        ])
        recipe = (port / "portfile.cmake").read_text(encoding="utf-8")
        self.assertRegex(recipe, r"\bREPO\s+catchorg/Catch2\b")
        self.assertRegex(recipe, r'\bREF\s+"?v\$\{VERSION\}"?')
        self.assertIn(
            "a6b0f1d1bd9178382f06de9773b31e3efdcdfb20fc3b65b23d9675e0838da6f64c417a75e062bda918dd28781e5d11a78e7130cef2394e676c7bc2da9e49106b",
            recipe,
        )
        self.assertIn("vcpkg_check_linkage(ONLY_STATIC_LIBRARY)", recipe)
        self.assertIn("fix-install-path.patch", recipe)
        patch = (port / "fix-install-path.patch").read_text(encoding="utf-8")
        self.assertIn("-      Catch2WithMain", patch)
        self.assertIn("${CMAKE_INSTALL_LIBDIR}/manual-link", patch)

    def test_mbedtls_overlay_uses_the_complete_41_lts_source_release(self) -> None:
        port = PORTS / "mbedtls"
        metadata = json.loads((port / "vcpkg.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["name"], "mbedtls")
        self.assertEqual(metadata["version"], "4.1.1")
        self.assertNotIn("port-version", metadata)
        recipe = (port / "portfile.cmake").read_text(encoding="utf-8")
        self.assertIn("vcpkg_download_distfile(ARCHIVE", recipe)
        self.assertIn("vcpkg_extract_source_archive", recipe)
        self.assertIn("https://github.com/Mbed-TLS/mbedtls/releases/download/mbedtls-${VERSION}/mbedtls-${VERSION}.tar.bz2", recipe)
        self.assertNotIn("vcpkg_from_github", recipe)
        self.assertNotIn("empty placeholder", recipe)
        self.assertIn(
            "1b82100511e9da298480a17e1093546eb26fe10fe6d391c8310e034fc57cd066d5837ab487f8ee06a669f6b29a20522901b74eb83b18ac6410aeb12d57ab2d12",
            recipe,
        )
        self.assertIn("vcpkg_check_linkage(ONLY_STATIC_LIBRARY)", recipe)
        self.assertIn('-DENABLE_PROGRAMS=OFF', recipe)
        self.assertIn('-DENABLE_TESTING=OFF', recipe)
        self.assertIn('-DUSE_STATIC_MBEDTLS_LIBRARY=ON', recipe)
        self.assertIn('-DUSE_SHARED_MBEDTLS_LIBRARY=OFF', recipe)
        # The MSVC runtime follows the triplet instead of a fixed literal; the
        # exact ON/OFF mapping is proven by test_mbedtls_crt.py.
        self.assertIn('VCPKG_CRT_LINKAGE', recipe)
        self.assertIn('-DMSVC_STATIC_RUNTIME=${', recipe)
        self.assertNotRegex(recipe, r'-DMSVC_STATIC_RUNTIME=(ON|OFF)\b')
        self.assertIn('-DGEN_FILES=OFF', recipe)
        self.assertIn('CONFIG_PATH "lib/cmake/MbedTLS"', recipe)
        self.assertIn('CONFIG_PATH "lib/cmake/TF-PSA-Crypto"', recipe)
        self.assertIn('vcpkg_cmake_config_fixup(DO_NOT_DELETE_PARENT_CONFIG_PATH CONFIG_PATH "lib/cmake/MbedTLS")', recipe)
        self.assertIn('static-library-name.patch', recipe)
        alias_patch = (port / 'static-library-name.patch').read_text(encoding="utf-8")
        self.assertIn('${CMAKE_STATIC_LIBRARY_PREFIX}mbedcrypto${CMAKE_STATIC_LIBRARY_SUFFIX}', alias_patch)
        self.assertNotIn("enable-pthread.patch", recipe)
        self.assertNotIn("features", metadata)
        self.assertTrue((port / "usage").is_file())

    def test_psa_adapter_uses_public_headers_and_installs_its_tests(self) -> None:
        project = (REPOSITORY / "build/projects/tests.vcxproj").read_text(encoding="utf-8")
        self.assertIn(r"src\tests\unit\crypto_codec.cpp", project)
        self.assertIn(r"<ObjectFileName>$(IntDir)crypto_codec_unit.obj</ObjectFileName>", project)
        adapter = (REPOSITORY / "src" / "core" / "codecs" / "crypto_codec.cpp").read_text(encoding="utf-8")
        for header in ("psa/crypto.h", "mbedtls/platform_util.h"):
            with self.subTest(header=header):
                self.assertIn(f"#include <{header}>", adapter)
        self.assertNotIn("#include <mbedtls/aes.h>", adapter)
        self.assertNotIn("#include <mbedtls/md5.h>", adapter)

"""The mbedtls overlay derives the MSVC runtime from ``VCPKG_CRT_LINKAGE``.

The real port is evaluated with ``cmake -P`` against fixture stubs for the vcpkg
helpers, so the captured ``vcpkg_cmake_configure`` arguments are exactly what a
dynamic-CRT and a static-CRT triplet would produce.  The
port must map ``static`` to ``-DMSVC_STATIC_RUNTIME=ON`` and ``dynamic`` to
``-DMSVC_STATIC_RUNTIME=OFF`` while keeping the static library selection intact.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
PORTFILE = REPOSITORY / "build" / "vcpkg" / "ports" / "mbedtls" / "portfile.cmake"

# Fixture stubs replace the vcpkg helpers the port imports; everything else runs
# as real CMake so the configure arguments are captured as authored.
DRIVER = """\
function(vcpkg_check_linkage)
endfunction()

function(vcpkg_download_distfile _variable)
    set(${_variable} "fixture-archive" PARENT_SCOPE)
endfunction()

function(vcpkg_extract_source_archive _variable)
    set(${_variable} "fixture-source" PARENT_SCOPE)
endfunction()

function(vcpkg_cmake_configure)
    set(capture "")
    foreach(argument IN LISTS ARGN)
        string(APPEND capture "${argument}\\n")
    endforeach()
    file(WRITE "${CONFIGURE_CAPTURE}" "${capture}")
endfunction()

macro(vcpkg_cmake_install)
endmacro()
macro(vcpkg_copy_pdbs)
endmacro()
macro(vcpkg_fixup_pkgconfig)
endmacro()
macro(vcpkg_cmake_config_fixup)
endmacro()
macro(vcpkg_install_copyright)
endmacro()

include("${PORTFILE}")
"""

CRT_MAPPINGS = (
    ("static", "-DMSVC_STATIC_RUNTIME=ON"),
    ("dynamic", "-DMSVC_STATIC_RUNTIME=OFF"),
)

STATIC_LIBRARY_FLAGS = (
    "-DUSE_STATIC_MBEDTLS_LIBRARY=ON",
    "-DUSE_SHARED_MBEDTLS_LIBRARY=OFF",
)


class MbedtlsCrtTests(unittest.TestCase):
    def _capture_options(self, root: Path, crt: str) -> tuple[list[str], Path]:
        script = root / f"driver-{crt}.cmake"
        capture = root / f"options-{crt}.txt"
        packages = root / f"packages-{crt}"
        script.write_text(DRIVER, encoding="utf-8")
        subprocess.run(
            [
                "cmake",
                f"-DPORTFILE={PORTFILE.as_posix()}",
                f"-DCONFIGURE_CAPTURE={capture.as_posix()}",
                f"-DCURRENT_PACKAGES_DIR={packages.as_posix()}",
                "-DPORT=mbedtls",
                f"-DVCPKG_CRT_LINKAGE={crt}",
                "-P",
                str(script),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return capture.read_text(encoding="utf-8").splitlines(), packages

    def test_port_maps_vcpkg_crt_linkage_onto_the_msvc_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for crt, expected in CRT_MAPPINGS:
                with self.subTest(crt=crt):
                    options, packages = self._capture_options(root, crt)
                    self.assertIn(expected, options)
                    self.assertNotIn(f"-DMSVC_STATIC_RUNTIME={'OFF' if crt == 'static' else 'ON'}", options)
                    for flag in STATIC_LIBRARY_FLAGS:
                        self.assertIn(flag, options)
                    # The debug staging tree is removed only under the controlled
                    # packages directory supplied by this test.
                    self.assertFalse((packages / "debug" / "include").exists())
                    self.assertTrue((packages / "share" / "mbedtls" / "usage").is_file())


if __name__ == "__main__":
    unittest.main()

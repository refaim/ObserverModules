"""UBSan's official Windows runtime and its dependencies use the DLL CRT together."""

from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


ROOT = Path(__file__).resolve().parents[2]
NS = {"m": "http://schemas.microsoft.com/developer/msbuild/2003"}


class UbsanCrtTests(unittest.TestCase):
    def test_release_keeps_static_crt_and_instrumentation_matches_its_runtime(self):
        project = ET.parse(ROOT / "build/unpackrat-project.props").getroot()
        runtimes = {node.get("Condition"): node.text for node in project.findall(".//m:ClCompile/m:RuntimeLibrary", NS)}
        self.assertEqual(runtimes, {
            "'$(Configuration)' == 'Release'": "MultiThreaded",
            "'$(Configuration)' == 'ASan' Or '$(Configuration)' == 'Fuzz'": "MultiThreadedDLL",
            "'$(Configuration)' == 'UBSan'": "MultiThreadedDLL",
            "'$(Configuration)' != 'Release' And '$(Configuration)' != 'ASan' And '$(Configuration)' != 'Fuzz' And '$(Configuration)' != 'UBSan'": "MultiThreadedDebug",
        })

    def test_msbuild_selects_an_isolated_ubsan_triplet_and_install_root(self):
        project = ET.parse(ROOT / "build/unpackrat-project.props").getroot()
        for name, expected in (
            ("VcpkgTriplet", "unpackrat-$(PlatformMoniker)-windows-static-ubsan"),
            ("VcpkgInstalledDir", "$(ArtifactsRoot)vcpkg_installed\\$(PlatformMoniker)-ubsan\\"),
        ):
            with self.subTest(property=name):
                nodes = project.findall(f".//m:{name}", NS)
                by_condition = {node.get("Condition"): node.text for node in nodes}
                self.assertEqual(by_condition["'$(Configuration)' == 'UBSan'"], expected)
                ordinary = next(node for node in nodes if "!=" in node.get("Condition"))
                self.assertIn("'$(Configuration)' != 'UBSan'", ordinary.get("Condition"))

    def test_ubsan_dependencies_are_static_libraries_with_dynamic_crt(self):
        triplets = ROOT / "build/vcpkg/triplets"
        text = (triplets / "unpackrat-x64-windows-static-ubsan.cmake").read_text()
        self.assertIn('include("${CMAKE_CURRENT_LIST_DIR}/unpackrat-x64-windows-static.cmake")', text)
        self.assertIn("set(VCPKG_CRT_LINKAGE dynamic)", text)
        self.assertIn("set(VCPKG_BUILD_TYPE release)", text)
        base = (triplets / "unpackrat-x64-windows-static.cmake").read_text()
        for setting in ("set(VCPKG_LIBRARY_LINKAGE static)",
                        "set(VCPKG_TARGET_ARCHITECTURE x64)", "set(VCPKG_PLATFORM_TOOLSET v145)",
                        "set(VCPKG_PLATFORM_TOOLSET_VERSION 14.51)", "set(VCPKG_CMAKE_SYSTEM_VERSION 10.0.26100.0)"):
            self.assertIn(setting, base)
        for other, crt in (("unpackrat-x64-windows-static", "static"), ("unpackrat-x86-windows-static", "static"),
                           ("unpackrat-x64-windows-static-asan", "dynamic"), ("unpackrat-x86-windows-static-asan", "dynamic")):
            self.assertIn(f"set(VCPKG_CRT_LINKAGE {crt})", (triplets / f"{other}.cmake").read_text())

    def test_driver_keeps_all_dependency_variants_separate(self):
        for configuration, suffix in (("Release", ""), ("Debug", ""), ("Coverage", ""),
                                      ("ASan", "-asan"), ("Fuzz", "-asan"), ("UBSan", "-ubsan")):
            with self.subTest(configuration=configuration):
                self.assertEqual(main.triplet("x64", configuration), f"unpackrat-x64-windows-static{suffix}")
                self.assertEqual(main.restore_root(ROOT, "x64", configuration),
                                 ROOT / "out/native/vcpkg_installed" / f"x64{suffix}")

    def test_restore_uses_the_ubsan_install_and_scratch_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(main, "_vs_installation", return_value=root / "vs"), \
                 mock.patch.object(main, "_msvc_toolset_version", return_value="14.51.36231"), \
                 mock.patch.object(main, "run") as run:
                main._restore(root, "x64", "UBSan", root / "vcpkg.exe")
            command = run.call_args.args[0]
            self.assertIn(f"--x-install-root={root / 'out/native/vcpkg_installed/x64-ubsan'}", command)
            self.assertIn(f"--x-buildtrees-root={root / 'out/native/vcpkg_scratch/x64-ubsan/buildtrees'}", command)
            self.assertIn(f"--x-packages-root={root / 'out/native/vcpkg_scratch/x64-ubsan/packages'}", command)
            self.assertEqual(command[command.index("--triplet") + 1], "unpackrat-x64-windows-static-ubsan")

    def test_ci_binary_cache_separates_the_ubsan_variant(self):
        workflow = (ROOT / ".github/workflows/ci-check.yml").read_text()
        keys = re.findall(r"^\s+key: (vcpkg-.+)$", workflow, re.MULTILINE)
        self.assertEqual(len(keys), 2)
        for key in keys:
            self.assertIn("inputs.stage == 'ubsan' && 'ubsan'", key)


if __name__ == "__main__":
    unittest.main()

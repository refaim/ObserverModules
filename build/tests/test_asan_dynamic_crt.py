"""Unloadable ASan modules share a DLL CRT; standalone Fuzz keeps the static CRT.

Every Fuzz target is a standalone executable that is never unloaded, so it must keep the
static CRT its supplied libFuzzer archive is built with, matching shipping Release. ASan
still links the shared CRT so its interceptors stay valid after an instrumented DLL
unloads. Its dependencies are isolated from the static-CRT fuzz tree.
"""

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


ROOT = Path(__file__).resolve().parents[2]


class SanitizerCrtTests(unittest.TestCase):
    def test_real_msbuild_selects_matching_crt_metadata_for_every_configuration(self):
        msbuild = main._find_msbuild()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "metadata.proj"
            project.write_text(
                '<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">'
                f'<Import Project="{ROOT / "build/unpackrat-project.props"}" />'
                '<ItemGroup><ClCompile Include="probe.cpp" /></ItemGroup></Project>', encoding="utf-8")
            for platform in ("Win32", "x64"):
                for configuration, expected in (("Release", "MultiThreaded"), ("Debug", "MultiThreadedDebug"),
                                                ("Coverage", "MultiThreadedDebug"), ("UBSan", "MultiThreadedDLL"),
                                                ("ASan", "MultiThreadedDLL"), ("Fuzz", "MultiThreaded")):
                    with self.subTest(platform=platform, configuration=configuration):
                        result = subprocess.run([str(msbuild), str(project), "-nologo", "-getItem:ClCompile",
                                                 f"-p:Platform={platform}", f"-p:Configuration={configuration}"],
                                                capture_output=True, text=True, encoding="utf-8", check=True)
                        item = json.loads(result.stdout)["Items"]["ClCompile"][0]
                        self.assertEqual(item["RuntimeLibrary"], expected)

    def test_real_cmake_evaluates_every_instrumented_triplet_with_matching_crt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            flags_by_variant = {}
            for architecture in ("x86", "x64"):
                # ASan dependencies use the DLL CRT; the standalone Fuzz dependencies the
                # supplied static libFuzzer archive requires keep the static CRT. Both stay
                # static libraries carrying the identical address-instrumentation flags.
                for variant, crt in (("asan", "dynamic"), ("fuzz", "static")):
                    with self.subTest(architecture=architecture, variant=variant):
                        script = root / f"{architecture}-{variant}.cmake"
                        output = root / f"{architecture}-{variant}.txt"
                        triplet = ROOT / f"build/vcpkg/triplets/unpackrat-{architecture}-windows-static-{variant}.cmake"
                        script.write_text('set(PORT fixture)\n'
                                          f'include("{triplet.as_posix()}")\n'
                                          f'file(WRITE "{output.as_posix()}" "${{VCPKG_CRT_LINKAGE}}|'
                                          '${VCPKG_LIBRARY_LINKAGE}|${VCPKG_BUILD_TYPE}|${VCPKG_C_FLAGS}|${VCPKG_CXX_FLAGS}")\n',
                                          encoding="utf-8")
                        subprocess.run(["cmake", "-P", str(script)], capture_output=True, text=True, check=True)
                        settings = output.read_text(encoding="utf-8").split("|")
                        self.assertEqual(settings[:3], [crt, "static", "release"])
                        for flags in settings[3:]:
                            self.assertIn("/fsanitize=address", flags)
                            self.assertIn("/Qspectre", flags)
                            self.assertIn("/W4", flags)
                        flags_by_variant.setdefault(architecture, {})[variant] = settings[3:]
                self.assertEqual(flags_by_variant[architecture]["fuzz"],
                                 flags_by_variant[architecture]["asan"])


if __name__ == "__main__":
    unittest.main()

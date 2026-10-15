"""Run the source port with synthetic source and the real vcpkg process helper.

These fixtures exercise recipe behavior, not native compilation or patch contents.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PORT = Path(__file__).resolve().parents[1] / "vcpkg/ports/yy-thunks"
SCRIPTS = Path(os.environ.get("VCPKG_ROOT", os.environ.get("VCPKG_INSTALLATION_ROOT", Path.home() / "scoop/apps/vcpkg/current"))) / "scripts/cmake"
SOURCE_SHA = "b949f296a412dfbf901060d4903d3b1dcc1b9afdad61b7eefbee7b86b77e58690fd17827e75b2337a3535fc6fe306976d6714058be1d427963c3358c29d273aa"


class YyThunksSourcePortTests(unittest.TestCase):
    def _run_port(self, root, *, arch="x86", tool=True, code=0, output=True, stale=False):
        cmake = shutil.which("cmake")
        self.assertIsNotNone(cmake, "Existing CMake is required")
        source = root / "source ü space"
        (source / "src").mkdir(parents=True)
        (source / "LICENSE").write_text("fixture MIT notice", encoding="utf-8")
        tools = root / "existing tools"
        tools.mkdir()
        if tool:
            (tools / "LibMaker.exe").write_bytes(b"fixture discovery only; never executed")
        obj = source / "objs" / arch / "YY_Thunks_for_WinXP.obj"
        obj.parent.mkdir(parents=True)
        if stale:
            obj.write_text("stale object", encoding="utf-8")
        (source / "objs" / "unselected.obj").write_text("must not install", encoding="utf-8")
        body = (
            '@echo off\n'
            'if not "%YY_THUNKS_HARDENED%"=="1" exit /b 41\n'
            'if not "%YY_THUNKS_OBJECT_TARGET%"=="WinXP" exit /b 42\n'
            f'if not "%Platform%"=="{arch}" exit /b 43\n'
            f'if exist "%~dp0..\\objs\\{arch}\\YY_Thunks_for_WinXP.obj" exit /b 44\n'
            '>"%~dp0..\\invoked.txt" echo %YY_THUNKS_HARDENED%^|%YY_THUNKS_OBJECT_TARGET%^|%Platform%\n'
        )
        if output:
            body += f'>"%~dp0..\\objs\\{arch}\\YY_Thunks_for_WinXP.obj" echo fresh-{arch}\n'
        body += f'exit /b {code}\n'
        (source / "src/Build.cmd").write_text(body, encoding="utf-8")
        trees = root / "buildtrees"
        trees.mkdir()
        script = root / "fixture.cmake"
        script.write_text(
            f'list(APPEND CMAKE_MODULE_PATH "{SCRIPTS.as_posix()}")\n'
            'set(Z_VCPKG_EXECUTE_PROCESS_NAME execute_process)\n'
            'include(vcpkg_execute_required_process)\n'
            'include(vcpkg_execute_in_download_mode)\n'
            'include(vcpkg_list)\n'
            'include(z_vcpkg_function_arguments)\n'
            'include(z_vcpkg_prettify_command_line)\n'
            'include(z_vcpkg_forward_output_variable)\n'
            'set(PORT yy-thunks)\n'
            f'set(VCPKG_TARGET_ARCHITECTURE "{arch}")\n'
            f'set(CURRENT_BUILDTREES_DIR "{trees.as_posix()}")\n'
            f'set(CURRENT_PACKAGES_DIR "{(root / "package").as_posix()}")\n'
            f'set(Z_VCPKG_ERROR_LOG_COLLECTION_FILE "{(root / "errors.txt").as_posix()}")\n'
            'function(vcpkg_from_github)\n'
            ' cmake_parse_arguments(PARSE_ARGV 0 arg "" "OUT_SOURCE_PATH;REPO;REF;SHA512" "PATCHES")\n'
            ' if(NOT arg_REPO STREQUAL "Chuyu-Team/YY-Thunks" OR NOT arg_REF STREQUAL "b41fb80b8d316581eec0959600aeb0e7339d0e5e")\n'
            '  message(FATAL_ERROR "Wrong source identity")\n endif()\n'
            f' if(NOT arg_SHA512 STREQUAL "{SOURCE_SHA}" OR NOT arg_PATCHES STREQUAL "ordinal-fallback.patch;hardened-build.patch")\n'
            '  message(FATAL_ERROR "Wrong source digest or patches")\n endif()\n'
            f' set(${{arg_OUT_SOURCE_PATH}} "{source.as_posix()}" PARENT_SCOPE)\n'
            'endfunction()\n'
            'function(vcpkg_install_copyright)\n'
            ' cmake_parse_arguments(PARSE_ARGV 0 arg "" "" "FILE_LIST")\n'
            ' file(INSTALL ${arg_FILE_LIST} DESTINATION "${CURRENT_PACKAGES_DIR}/share/${PORT}" RENAME copyright)\n'
            'endfunction()\n'
            f'include("{(PORT / "portfile.cmake").as_posix()}")\n'
            'if(NOT VCPKG_POLICY_EMPTY_INCLUDE_FOLDER STREQUAL "enabled")\n'
            ' message(FATAL_ERROR "Empty include policy missing")\nendif()\n',
            encoding="utf-8",
        )
        env = {**os.environ, "PATH": str(tools) + os.pathsep + str(Path(os.environ["SystemRoot"]) / "System32")}
        return subprocess.run([cmake, "-P", str(script)], env=env, capture_output=True, text=True, check=False)

    def test_builds_and_installs_only_the_selected_architecture(self):
        for arch in ("x86", "x64"):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                result = self._run_port(root, arch=arch, stale=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                installed = root / "package/share/yy-thunks"
                self.assertEqual((installed / f"objs/{arch}/YY_Thunks_for_WinXP.obj").read_text().strip(), f"fresh-{arch}")
                self.assertEqual((root / "source ü space/invoked.txt").read_text().strip(), f"1|WinXP|{arch}")
                self.assertEqual((installed / "copyright").read_text(), "fixture MIT notice")
                self.assertEqual(sorted(p.relative_to(installed).as_posix() for p in installed.rglob("*") if p.is_file()), ["copyright", f"objs/{arch}/YY_Thunks_for_WinXP.obj"])

    def test_missing_existing_libmaker_is_a_prerequisite_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._run_port(root, tool=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("LibMaker", result.stderr)
            self.assertFalse((root / "source ü space/invoked.txt").exists())

    def test_any_build_failure_rejects_even_an_emitted_object(self):
        for code in (7, -1073741819):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                result = self._run_port(root, code=code)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Command failed:", result.stderr)
                self.assertIn("Error code:", result.stderr)
                # CMake names the Windows exception status, preserving the failure.
                expected_status = "Access violation" if code < 0 else str(code)
                self.assertIn(f"Error code: {expected_status}", result.stderr)
                self.assertTrue((root / "source ü space/invoked.txt").exists())
                self.assertFalse((root / "package/share/yy-thunks").exists())

    def test_success_without_new_output_rejects_stale_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._run_port(root, output=False, stale=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("did not produce", result.stderr)
            self.assertFalse((root / "package/share/yy-thunks").exists())

    def test_unsupported_architecture_is_rejected_before_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._run_port(root, arch="arm64")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("x86 and x64", result.stderr)
            self.assertFalse((root / "source ü space/invoked.txt").exists())

    def test_metadata_changes_the_binary_package_revision(self):
        metadata = json.loads((PORT / "vcpkg.json").read_text())
        self.assertEqual(metadata["version"], "1.2.3")
        self.assertEqual(metadata.get("port-version"), 1)
        self.assertEqual(metadata["supports"], "windows & (x86 | x64)")
        self.assertEqual(metadata["description"], "YY-Thunks Windows XP WinAPI compatibility object built from pinned source")

    def test_all_triplets_track_existing_libmaker_contents_without_provisioning(self):
        cmake = shutil.which("cmake")
        self.assertIsNotNone(cmake)
        triplets = sorted((PORT.parents[1] / "triplets").glob("unpackrat-*-windows-static*.cmake"))
        self.assertEqual({path.name for path in triplets}, {
            "unpackrat-x86-windows-static.cmake", "unpackrat-x64-windows-static.cmake",
            "unpackrat-x86-windows-static-asan.cmake", "unpackrat-x64-windows-static-asan.cmake",
        })
        for triplet in triplets:
            with self.subTest(triplet=triplet.name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                tool = root / "LibMaker.exe"
                script = root / "track.cmake"
                report = root / "tracked.txt"
                script.write_text(
                    'set(PORT yy-thunks)\n'
                    'set(VCPKG_HASH_ADDITIONAL_FILES existing-entry)\n'
                    f'include("{triplet.as_posix()}")\n'
                    'set(tool_hash missing)\n'
                    'list(LENGTH VCPKG_HASH_ADDITIONAL_FILES entries)\n'
                    'if(entries GREATER 1)\n'
                    ' list(GET VCPKG_HASH_ADDITIONAL_FILES 1 tool)\n'
                    ' file(SHA256 "${tool}" tool_hash)\n'
                    'endif()\n'
                    f'file(WRITE "{report.as_posix()}" "${{VCPKG_HASH_ADDITIONAL_FILES}}\n${{tool_hash}}")\n',
                    encoding="utf-8",
                )
                env = {**os.environ, "PATH": str(root)}
                hashes = []
                for content in (None, b"tool version one", b"tool version two"):
                    if content is not None:
                        tool.write_bytes(content)
                    result = subprocess.run([cmake, "-P", str(script)], env=env, capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    tracked, digest = report.read_text().splitlines()
                    if content is None:
                        self.assertEqual((tracked, digest), ("existing-entry", "missing"))
                    else:
                        self.assertEqual(tracked, f"existing-entry;{tool.as_posix()}")
                        hashes.append(digest)
                self.assertNotEqual(hashes[0], hashes[1])

    def test_other_ports_do_not_depend_on_libmaker(self):
        cmake = shutil.which("cmake")
        self.assertIsNotNone(cmake)
        for triplet in sorted((PORT.parents[1] / "triplets").glob("unpackrat-*-windows-static*.cmake")):
            with self.subTest(triplet=triplet.name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                tool = root / "LibMaker.exe"
                script = root / "other-port.cmake"
                report = root / "tracked.txt"
                script.write_text(
                    'set(PORT zlib)\n'
                    'set(VCPKG_HASH_ADDITIONAL_FILES existing-entry)\n'
                    f'include("{triplet.as_posix()}")\n'
                    f'file(WRITE "{report.as_posix()}" "${{VCPKG_HASH_ADDITIONAL_FILES}}")\n',
                    encoding="utf-8",
                )
                for content in (b"tool version one", b"tool version two"):
                    tool.write_bytes(content)
                    result = subprocess.run([cmake, "-P", str(script)], env={**os.environ, "PATH": str(root)}, capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(report.read_text(), "existing-entry")


if __name__ == "__main__":
    unittest.main()

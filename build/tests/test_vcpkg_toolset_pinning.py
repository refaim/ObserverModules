"""vcpkg builds every dependency with the same v145 14.51 toolset.

Every triplet (x86/x64, standard, ASan and the standalone Fuzz variant) must pin
``VCPKG_PLATFORM_TOOLSET`` to ``v145`` and ``VCPKG_PLATFORM_TOOLSET_VERSION`` to ``14.51``
so a dependency compiled for the shipped binaries never mixes a second toolset, while
shipping Release retains the ``/MT`` self-contained contract. ASan uses the DLL CRT to
permit module unload; the never-unloaded standalone Fuzz executables keep the static CRT.

``_restore`` must additionally point vcpkg at the same selected VS installation the
driver builds with - through the child environment only, never by mutating the host
environment - so two installed Visual Studios cannot make vcpkg use the older one.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


TRIPLETS = (
    ("unpackrat-x86-windows-static", "static"),
    ("unpackrat-x64-windows-static", "static"),
    ("unpackrat-x86-windows-static-asan", "dynamic"),
    ("unpackrat-x64-windows-static-asan", "dynamic"),
    ("unpackrat-x86-windows-static-fuzz", "static"),
    ("unpackrat-x64-windows-static-fuzz", "static"),
)


def evaluate_triplet(name: str) -> dict[str, str]:
    """Return a triplet's effective variables after CMake follows its includes.

    The Fuzz variants layer the static CRT override on top of the included ASan triplet, so
    the effective values - not the literal text of each file - are what the dependency build
    identity depends on. The pinned version is evaluated with an unset environment override so
    the assertion reads the committed pin rather than the developer's machine environment.
    """
    triplet = main.REPOSITORY / "build" / "vcpkg" / "triplets" / f"{name}.cmake"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        script = root / "evaluate.cmake"
        output = root / "effective.txt"
        script.write_text(
            "set(PORT fixture)\n"
            f'include("{triplet.as_posix()}")\n'
            f'file(WRITE "{output.as_posix()}" "${{VCPKG_PLATFORM_TOOLSET}}|'
            "${VCPKG_PLATFORM_TOOLSET_VERSION}|${VCPKG_CMAKE_SYSTEM_VERSION}|"
            '${VCPKG_CRT_LINKAGE}|${VCPKG_LIBRARY_LINKAGE}")\n',
            encoding="utf-8",
        )
        environment = {
            key: value for key, value in os.environ.items()
            if key != "VCPKG_PLATFORM_TOOLSET_VERSION"
        }
        subprocess.run(["cmake", "-P", str(script)], capture_output=True, text=True, check=True,
                       env=environment)
        toolset, version, sdk, crt, library = output.read_text(encoding="utf-8").split("|")
    return {"toolset": toolset, "version": version, "sdk": sdk, "crt": crt, "library": library}


class VcpkgTripletPinTests(unittest.TestCase):
    def test_every_triplet_pins_the_v145_toolset_and_matching_crt(self) -> None:
        for name, crt in TRIPLETS:
            with self.subTest(triplet=name):
                effective = evaluate_triplet(name)
                self.assertEqual(effective["toolset"], "v145")
                self.assertEqual(effective["version"], "14.51")
                self.assertEqual(effective["crt"], crt)
                self.assertEqual(effective["library"], "static")

    def test_every_triplet_targets_the_retained_windows_sdk_family(self) -> None:
        # The dependencies must be built against the same SDK family the shipped binaries
        # target, so a newly installed 10.0.28000 SDK never changes the restored artifacts.
        for name, _ in TRIPLETS:
            with self.subTest(triplet=name):
                self.assertEqual(evaluate_triplet(name)["sdk"], "10.0.26100.0")


class RestoreEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.vcpkg = self.root / "vcpkg.exe"
        self.installation = self.root / "vs"

    def test_restore_pins_the_selected_installation_in_the_child_environment_only(self) -> None:
        original = dict(os.environ)
        with mock.patch.object(main, "_vs_installation", return_value=self.installation), \
             mock.patch.object(main, "_msvc_toolset_version", return_value="14.51.36231"), \
             mock.patch.object(main, "run") as invoked:
            main._restore(self.repository, "x64", "Debug", self.vcpkg)

        env = invoked.call_args.kwargs["env"]
        self.assertEqual(env["VCPKG_VISUAL_STUDIO_PATH"], os.fspath(self.installation))
        self.assertEqual(env["VCPKG_PLATFORM_TOOLSET_VERSION"], "14.51.36231")
        self.assertEqual(env["VCPKG_FORCE_SYSTEM_BINARIES"], "1")
        self.assertIn("PATH", env["VCPKG_KEEP_ENV_VARS"].split(";"))
        # The pin travels through the child environment; the host environment is untouched.
        self.assertEqual(dict(os.environ), original)
        self.assertNotIn("VCPKG_VISUAL_STUDIO_PATH", os.environ)

    def test_restore_preserves_system_tool_path_and_existing_kept_variables(self) -> None:
        for inherited, expected in (("", "PATH"), ("PROJECT_TOOLS", "PROJECT_TOOLS;PATH"),
                                    ("PATH;PROJECT_TOOLS", "PATH;PROJECT_TOOLS"),
                                    ("Path;PROJECT_TOOLS", "Path;PROJECT_TOOLS")):
            with self.subTest(inherited=inherited), mock.patch.dict(
                    os.environ, {"VCPKG_KEEP_ENV_VARS": inherited}), \
                 mock.patch.object(main, "_vs_installation", return_value=self.installation), \
                 mock.patch.object(main, "_msvc_toolset_version", return_value="14.51.36231"), \
                 mock.patch.object(main, "run") as invoked:
                main._restore(self.repository, "x64", "Debug", self.vcpkg)
                self.assertEqual(invoked.call_args.kwargs["env"]["VCPKG_KEEP_ENV_VARS"], expected)
                self.assertEqual(os.environ["VCPKG_KEEP_ENV_VARS"], inherited)


if __name__ == "__main__":
    unittest.main()

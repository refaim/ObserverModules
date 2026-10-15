"""vcpkg builds every dependency with the same v145 14.51 toolset and the static CRT.

All four triplets (x86/x64, standard and ASan) must pin ``VCPKG_PLATFORM_TOOLSET`` to
``v145`` and ``VCPKG_PLATFORM_TOOLSET_VERSION`` to ``14.51`` so a dependency compiled
for the shipped binaries never mixes a second toolset, while the static CRT linkage
keeps the ``/MT`` self-contained contract.

``_restore`` must additionally point vcpkg at the same selected VS installation the
driver builds with - through the child environment only, never by mutating the host
environment - so two installed Visual Studios cannot make vcpkg use the older one.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


TRIPLETS = (
    "unpackrat-x86-windows-static",
    "unpackrat-x64-windows-static",
    "unpackrat-x86-windows-static-asan",
    "unpackrat-x64-windows-static-asan",
)


class VcpkgTripletPinTests(unittest.TestCase):
    def test_every_triplet_pins_the_v145_toolset_and_the_static_crt(self) -> None:
        for name in TRIPLETS:
            with self.subTest(triplet=name):
                text = (main.REPOSITORY / "build" / "vcpkg" / "triplets" / f"{name}.cmake").read_text(
                    encoding="utf-8"
                )
                self.assertIn("set(VCPKG_PLATFORM_TOOLSET v145)", text)
                self.assertIn("set(VCPKG_PLATFORM_TOOLSET_VERSION 14.51)", text)
                self.assertIn("set(VCPKG_CRT_LINKAGE static)", text)
                self.assertIn("set(VCPKG_LIBRARY_LINKAGE static)", text)

    def test_every_triplet_targets_the_retained_windows_sdk_family(self) -> None:
        # The dependencies must be built against the same SDK family the shipped binaries
        # target, so a newly installed 10.0.28000 SDK never changes the restored artifacts.
        for name in TRIPLETS:
            with self.subTest(triplet=name):
                text = (main.REPOSITORY / "build" / "vcpkg" / "triplets" / f"{name}.cmake").read_text(
                    encoding="utf-8"
                )
                self.assertIn("set(VCPKG_CMAKE_SYSTEM_VERSION 10.0.26100.0)", text)


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

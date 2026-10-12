from __future__ import annotations

import unittest

from core.host import (
    detect_host_architecture,
    require_runnable,
    runnable_architectures,
    verify_route,
)


class HostTests(unittest.TestCase):
    def test_common_machine_names_are_canonicalized(self) -> None:
        for machine, expected in (
            ("AMD64", "x64"),
            ("x86_64", "x64"),
            ("ARM64", "arm64"),
            ("aarch64", "arm64"),
            ("x86", "x86"),
            ("i686", "x86"),
        ):
            with self.subTest(machine=machine):
                self.assertEqual(detect_host_architecture(machine), expected)

    def test_unknown_machine_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsupported Windows host architecture"):
            detect_host_architecture("mips64")

    def test_runnable_matrix_matches_windows_emulation_contract(self) -> None:
        requested = ("x86", "x64")
        self.assertEqual(runnable_architectures(requested, "x86"), ("x86",))
        self.assertEqual(runnable_architectures(requested, "x64"), ("x86", "x64"))
        # An ARM64 Windows host may execute emulated x86/x64 targets, but ARM64 itself is
        # not a supported target and can never be requested.
        self.assertEqual(runnable_architectures(requested, "arm64"), ("x86", "x64"))

    def test_arm64_is_not_an_accepted_requested_target(self) -> None:
        for requested in (("arm64",), ("x86", "arm64")):
            with self.subTest(requested=requested), self.assertRaisesRegex(
                ValueError, "unsupported requested architecture"
            ):
                runnable_architectures(requested, "x64")
        with self.assertRaisesRegex(ValueError, "unsupported requested architecture"):
            verify_route(("arm64",), "arm64")

    def test_invalid_requested_and_host_architectures_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported requested architecture"):
            runnable_architectures(("sparc",), "x64")
        with self.assertRaisesRegex(ValueError, "unsupported host architecture"):
            runnable_architectures(("x64",), "sparc")

    def test_test_command_contract_rejects_nonrunnable_requests(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "cannot run x64 tests on x86 host"):
            require_runnable(("x86", "x64"), "x86")
        with self.assertRaisesRegex(ValueError, "unsupported requested architecture"):
            require_runnable(("arm64",), "x64")

        self.assertEqual(require_runnable(("x86", "x64"), "x64"), ("x86", "x64"))

    def test_verify_route_selects_host_capable_specialists(self) -> None:
        route = verify_route(("x86", "x64"), "x64")

        self.assertEqual(route.runnable, ("x86", "x64"))
        self.assertEqual(route.coverage, ("x64",))
        self.assertEqual(route.asan, ("x86", "x64"))
        self.assertEqual(route.ubsan, ("x64",))
        self.assertEqual(route.fuzz, ("x86", "x64"))
        self.assertTrue(route.run_x64_specialists)
        self.assertEqual(route.deferred, ())

    def test_verify_route_defers_nonrunnable_specialists_explicitly(self) -> None:
        route = verify_route(("x64",), "x86")

        self.assertEqual(route.runnable, ())
        self.assertEqual(route.coverage, ())
        self.assertEqual(route.asan, ())
        self.assertEqual(route.ubsan, ())
        self.assertEqual(route.fuzz, ())
        self.assertFalse(route.run_x64_specialists)
        self.assertEqual(
            [item.gate for item in route.deferred],
            ["tests", "package-runtime", "coverage", "asan", "ubsan", "leaks", "fuzz"],
        )

    def test_verify_route_runs_the_x86_replay_gate_without_the_x64_specialists(self) -> None:
        route = verify_route(("x86",), "x86")

        self.assertEqual(route.runnable, ("x86",))
        self.assertEqual(route.coverage, ())
        self.assertEqual(route.asan, ("x86",))
        self.assertEqual(route.ubsan, ())
        self.assertEqual(route.fuzz, ("x86",))
        self.assertFalse(route.run_x64_specialists)
        self.assertEqual(route.deferred, ())


if __name__ == "__main__":
    unittest.main(verbosity=2)

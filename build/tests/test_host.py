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
        requested = ("x86", "x64", "arm64")
        self.assertEqual(runnable_architectures(requested, "x86"), ("x86",))
        self.assertEqual(runnable_architectures(requested, "x64"), ("x86", "x64"))
        self.assertEqual(runnable_architectures(requested, "arm64"), requested)

    def test_invalid_requested_and_host_architectures_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported requested architecture"):
            runnable_architectures(("sparc",), "x64")
        with self.assertRaisesRegex(ValueError, "unsupported host architecture"):
            runnable_architectures(("x64",), "sparc")

    def test_test_command_contract_rejects_nonrunnable_requests(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "cannot run arm64 tests on x64 host"):
            require_runnable(("x86", "arm64"), "x64")

        self.assertEqual(require_runnable(("x86", "x64"), "x64"), ("x86", "x64"))

    def test_verify_route_selects_host_capable_specialists(self) -> None:
        route = verify_route(("x86", "x64", "arm64"), "x64")

        self.assertEqual(route.runnable, ("x86", "x64"))
        self.assertEqual(route.coverage, ("x64",))
        self.assertEqual(route.asan, ("x86", "x64"))
        self.assertEqual(route.ubsan, ("x64",))
        self.assertTrue(route.run_x64_specialists)
        self.assertEqual(
            [(item.gate, item.architecture) for item in route.deferred],
            [("tests", "arm64"), ("package-runtime", "arm64")],
        )
        self.assertTrue(all(item.reason for item in route.deferred))

    def test_verify_route_defers_nonrunnable_specialists_explicitly(self) -> None:
        route = verify_route(("x64",), "x86")

        self.assertEqual(route.runnable, ())
        self.assertEqual(route.coverage, ())
        self.assertEqual(route.asan, ())
        self.assertEqual(route.ubsan, ())
        self.assertFalse(route.run_x64_specialists)
        self.assertEqual(
            [item.gate for item in route.deferred],
            ["tests", "package-runtime", "coverage", "asan", "ubsan", "leaks", "fuzz"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

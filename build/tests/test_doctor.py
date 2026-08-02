from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import runpy
from types import SimpleNamespace
import sys
import unittest
from unittest import mock


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import core.doctor as doctor  # noqa: E402


TOOLCHAIN = SimpleNamespace(identity=(
    ("msbuild_version", "17.14"), ("vc_tools_version", "14.44"),
    ("clang_tidy_version", "19.1"), ("windows_sdk_version", "10.0"),
))
SOURCE = SimpleNamespace(identity=(
    ("pwsh_version", "7.5"), ("clang_format_version", "19.1"),
    ("cppcheck_version", "2.18"), ("psscriptanalyzer_version", "1.24"),
))
QUALITY = object()
RUNTIMES = object()


class DoctorTests(unittest.TestCase):
    def patches(self) -> tuple[mock._patch, ...]:
        return (
            mock.patch.object(doctor, "discover_msvc_toolchain", return_value=TOOLCHAIN),
            mock.patch.object(doctor, "discover_source_tools", return_value=SOURCE),
            mock.patch.object(doctor, "discover_quality_tools", return_value=QUALITY),
            mock.patch.object(doctor, "resolve_sanitizer_runtimes", return_value=RUNTIMES),
        )

    def test_complete_report_is_deterministic_and_main_renders_plain_tsv(self) -> None:
        with self.patches()[0] as msvc, self.patches()[1] as source, \
             self.patches()[2] as quality, self.patches()[3] as runtimes:
            report = doctor.doctor_report()
            self.assertEqual(report, doctor.doctor_report())
        self.assertEqual(
            report,
            (
                doctor.Probe("python", "OK", "3.14.6"),
                doctor.Probe("msvc", "OK", "MSBuild=17.14, MSVC=14.44, LLVM=19.1, SDK=10.0"),
                doctor.Probe("source-tools", "OK", "PowerShell=7.5, clang-format=19.1, Cppcheck=2.18, PSScriptAnalyzer=1.24"),
                doctor.Probe("quality-tools", "OK", "clang-cl, clang-scan-deps, llvm-cov, llvm-profdata, dumpbin, BinSkim, UMDH"),
                doctor.Probe("sanitizer-runtimes", "OK", "ASan x86/x64, UBSan x64"),
            ),
        )
        self.assertEqual(msvc.call_count, 2)
        self.assertEqual(source.call_args, mock.call(TOOLCHAIN))
        self.assertEqual(quality.call_args, mock.call(TOOLCHAIN))
        self.assertEqual(runtimes.call_args, mock.call(TOOLCHAIN))

        output = StringIO()
        with mock.patch.object(doctor, "doctor_report", return_value=report), redirect_stdout(output):
            self.assertEqual(doctor.main(()), 0)
        self.assertEqual(
            output.getvalue(),
            "probe\tstatus\tdetail\n" + "".join(
                f"{item.name}\t{item.status}\t{item.detail}\n" for item in report
            ),
        )

    def test_failures_are_concise_independent_and_make_main_fail(self) -> None:
        failing_quality = RuntimeError("quality\n  missing")
        with (
            mock.patch.object(doctor, "discover_msvc_toolchain", return_value=TOOLCHAIN),
            mock.patch.object(doctor, "discover_source_tools", return_value=SOURCE),
            mock.patch.object(doctor, "discover_quality_tools", side_effect=failing_quality),
            mock.patch.object(doctor, "resolve_sanitizer_runtimes", return_value=RUNTIMES),
        ):
            report = doctor.doctor_report()
        self.assertEqual([item.status for item in report], ["OK", "OK", "OK", "MISSING", "OK"])
        self.assertEqual(report[3].detail, "quality missing")
        output = StringIO()
        with mock.patch.object(doctor, "doctor_report", return_value=report), redirect_stdout(output):
            self.assertEqual(doctor.main(()), 1)
        self.assertIn("quality-tools\tMISSING\tquality missing\n", output.getvalue())

    def test_missing_python_and_msvc_still_report_every_probe(self) -> None:
        missing = RuntimeError()
        with (
            mock.patch.object(doctor.sys, "version_info", (3, 14, 5)),
            mock.patch.object(doctor, "discover_msvc_toolchain", side_effect=missing),
            mock.patch.object(doctor, "discover_source_tools") as source,
            mock.patch.object(doctor, "discover_quality_tools") as quality,
            mock.patch.object(doctor, "resolve_sanitizer_runtimes") as runtimes,
        ):
            report = doctor.doctor_report()
        self.assertEqual([item.status for item in report], ["MISSING"] * 5)
        self.assertIn("requires ==3.14.6, running 3.14.5", report[0].detail)
        self.assertEqual(report[1].detail, "RuntimeError")
        self.assertTrue(all(item.detail == "MSVC toolchain unavailable" for item in report[2:]))
        source.assert_not_called()
        quality.assert_not_called()
        runtimes.assert_not_called()

    def test_module_entry_point_exits_with_main_result(self) -> None:
        patches = (
            mock.patch("core.toolchain.discover_msvc_toolchain", return_value=TOOLCHAIN),
            mock.patch("core.source_tools.discover_source_tools", return_value=SOURCE),
            mock.patch("core.quality_tools.discover_quality_tools", return_value=QUALITY),
            mock.patch("core.quality_tools.resolve_sanitizer_runtimes", return_value=RUNTIMES),
            mock.patch.object(sys, "argv", ["doctor.py"]),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             redirect_stdout(StringIO()), self.assertRaises(SystemExit) as raised:
            runpy.run_path(str(BUILD_ROOT / "core/doctor.py"), run_name="__main__")
        self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()

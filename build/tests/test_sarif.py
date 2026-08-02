from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

from core.sarif import (  # noqa: E402
    SarifError,
    SarifFindingsError,
    clang_tidy_to_sarif,
    merge_sarif,
    normalize_msvc,
    require_clean,
)
from core import sarif  # noqa: E402


def write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


class SarifTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_clang_tidy_conversion_is_confined_deduplicated_and_deterministic(self) -> None:
        repository = self.root / "repo"
        source = repository / "src" / "unit.cpp"
        other_source = repository / "src" / "other.cpp"
        outside = self.root / "repo-sibling" / "outside.cpp"
        for path in (source, other_source, outside):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

        logs = self.root / "objects"
        first = logs / "z" / "z.ClangTidy.log"
        second = logs / "a" / "a.ClangTidy.log"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        duplicate = f"{source}(9,3): warning: duplicate message [modernize-use-nullptr] [renpy.vcxproj]"
        first.write_text(
            "\n".join(
                (
                    duplicate,
                    f"{outside}(1,1): error: outside [outside-check] [renpy.vcxproj]",
                    f"{source}(1,1): warning: no rule suffix",
                    f"{source}(2,5): error: second message [-*, bugprone-sizeof-expression] [renpy.vcxproj]",
                )
            ),
            encoding="utf-8",
        )
        second.write_text(
            "\n".join(
                (
                    f"{other_source}(4,2): warning: first message [alpha-check] [renpy.vcxproj]",
                    duplicate,
                    f"{source}(3,1): warning: disabled only [-*] [renpy.vcxproj]",
                    "ordinary compiler output",
                )
            ),
            encoding="utf-8",
        )

        output = self.root / "reports" / "tidy.sarif"
        repeat = self.root / "reports" / "repeat.sarif"
        automation_id = "clang-tidy/x64/renpy/pickle/"
        clang_tidy_to_sarif(repository, logs, output, automation_id)
        clang_tidy_to_sarif(repository, logs, repeat, automation_id)

        self.assertEqual(output.read_bytes(), repeat.read_bytes())
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual("2.1.0", document["version"])
        self.assertEqual(automation_id, document["runs"][0]["automationDetails"]["id"])
        driver = document["runs"][0]["tool"]["driver"]
        self.assertEqual(
            ["alpha-check", "bugprone-sizeof-expression", "modernize-use-nullptr"],
            [rule["id"] for rule in driver["rules"]],
        )
        results = document["runs"][0]["results"]
        self.assertEqual(3, len(results))
        self.assertEqual(
            [
                ("src/other.cpp", 4, "alpha-check", "warning", "first message"),
                ("src/unit.cpp", 2, "bugprone-sizeof-expression", "error", "second message"),
                ("src/unit.cpp", 9, "modernize-use-nullptr", "warning", "duplicate message"),
            ],
            [
                (
                    result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
                    result["locations"][0]["physicalLocation"]["region"]["startLine"],
                    result["ruleId"],
                    result["level"],
                    result["message"]["text"],
                )
                for result in results
            ],
        )

    def test_clang_tidy_missing_log_tree_produces_an_empty_run(self) -> None:
        repository = self.root / "repo"
        repository.mkdir()
        output = self.root / "empty.sarif"

        clang_tidy_to_sarif(repository, self.root / "missing", output, "tidy/exact/")

        run = json.loads(output.read_text(encoding="utf-8"))["runs"][0]
        self.assertEqual([], run["results"])
        self.assertEqual([], run["tool"]["driver"]["rules"])

    def test_msvc_normalization_retains_document_and_assigns_stable_run_ids(self) -> None:
        source = self.root / "raw.sarif"
        output = self.root / "normalized.sarif"
        original = {
            "version": "2.1.0",
            "$schema": "original-schema",
            "inlineExternalProperties": [{"guid": "kept"}],
            "runs": [
                {"automationDetails": {"id": "unstable", "description": {"text": "kept"}}, "results": []},
                {"automationDetails": "invalid but replaceable", "properties": {"kept": True}},
            ],
        }
        write_json(source, original)

        normalize_msvc(source, output, "msvc-analyze/x64/renpy/pickle/")

        normalized = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual("original-schema", normalized["$schema"])
        self.assertEqual(original["inlineExternalProperties"], normalized["inlineExternalProperties"])
        self.assertEqual(
            [
                "msvc-analyze/x64/renpy/pickle/run-1/",
                "msvc-analyze/x64/renpy/pickle/run-2/",
            ],
            [run["automationDetails"]["id"] for run in normalized["runs"]],
        )
        self.assertEqual({"text": "kept"}, normalized["runs"][0]["automationDetails"]["description"])
        self.assertEqual({"kept": True}, normalized["runs"][1]["properties"])

        single_source = self.root / "single.sarif"
        single_output = self.root / "single-normalized.sarif"
        write_json(single_source, {"version": "2.1.0", "runs": [{"results": []}]})
        normalize_msvc(single_source, single_output, "caller-supplied-exact-id")
        self.assertEqual(
            "caller-supplied-exact-id",
            json.loads(single_output.read_text(encoding="utf-8"))["runs"][0]["automationDetails"]["id"],
        )

    def test_normalization_rejects_non_21_documents_or_invalid_runs(self) -> None:
        source = self.root / "raw.sarif"
        output = self.root / "normalized.sarif"
        for document in (
            {"version": "2.0.0", "runs": [{}]},
            {"version": "2.1.0", "runs": []},
            {"version": "2.1.0", "runs": ["not an object"]},
        ):
            with self.subTest(document=document):
                write_json(source, document)
                with self.assertRaises(SarifError):
                    normalize_msvc(source, output, "id")

    def test_merge_sorts_runs_by_identity_and_is_deterministic(self) -> None:
        first = self.root / "first.sarif"
        second = self.root / "second.sarif"
        write_json(first, {"version": "2.1.0", "runs": [{"automationDetails": {"id": "z/"}, "value": 2}]})
        write_json(
            second,
            {
                "version": "2.1.0",
                "runs": [
                    {"automationDetails": {"id": "m/"}, "value": 1},
                    {"automationDetails": {"id": "a/"}, "value": 0},
                ],
            },
        )
        output = self.root / "merged.sarif"
        reverse = self.root / "merged-reverse.sarif"

        merge_sarif((first, second), output)
        merge_sarif((second, first), reverse)

        self.assertEqual(output.read_bytes(), reverse.read_bytes())
        merged = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(["a/", "m/", "z/"], [run["automationDetails"]["id"] for run in merged["runs"]])
        self.assertEqual([0, 1, 2], [run["value"] for run in merged["runs"]])

    def test_merge_rejects_missing_or_duplicate_identity_and_no_inputs(self) -> None:
        output = self.root / "merged.sarif"
        missing = self.root / "missing-id.sarif"
        duplicate = self.root / "duplicate.sarif"
        write_json(missing, {"version": "2.1.0", "runs": [{"automationDetails": {}}]})
        write_json(
            duplicate,
            {
                "version": "2.1.0",
                "runs": [
                    {"automationDetails": {"id": "same/"}},
                    {"automationDetails": {"id": "same/"}},
                ],
            },
        )

        for inputs in ((), (missing,), (duplicate,)):
            with self.subTest(inputs=inputs), self.assertRaises(SarifError):
                merge_sarif(inputs, output)

    def test_gate_ignores_non_findings_and_rejects_warnings_and_errors(self) -> None:
        clean = self.root / "clean.sarif"
        warning = self.root / "warning.sarif"
        write_json(
            clean,
            {
                "version": "2.1.0",
                "runs": [
                    {
                        "automationDetails": {"id": "clean/"},
                        "results": [{"level": "note"}, {"level": "none"}],
                    }
                ],
            },
        )
        write_json(
            warning,
            {
                "version": "2.1.0",
                "runs": [
                    {
                        "automationDetails": {"id": "findings/"},
                        "results": [{"level": "warning"}, {"level": "error"}, {"level": "note"}],
                    }
                ],
            },
        )

        self.assertIsNone(require_clean((clean,)))
        with self.assertRaisesRegex(SarifFindingsError, "2 warning/error finding"):
            require_clean((clean, warning))

    def test_cli_dispatches_every_command_into_observer_output_directory(self) -> None:
        output = self.root / "out"
        output.mkdir()
        source = self.root / "raw.sarif"
        logs = self.root / "logs"
        repository = self.root / "repo"
        environment = {"OBSERVER_OUT_DIR": str(output)}

        with mock.patch.dict("os.environ", environment, clear=True):
            with mock.patch.object(sarif, "normalize_msvc") as operation:
                self.assertEqual(
                    0,
                    sarif.main(("normalize-msvc", str(source), "msvc/id/", "--output-name", "renpy.sarif")),
                )
                operation.assert_called_once_with(source, output / "renpy.sarif", "msvc/id/")

            with mock.patch.object(sarif, "clang_tidy_to_sarif") as operation:
                self.assertEqual(
                    0,
                    sarif.main(("convert-tidy", str(repository), str(logs), "tidy/id/")),
                )
                operation.assert_called_once_with(repository, logs, output / "renpy.sarif", "tidy/id/")

            other = self.root / "other.sarif"
            with mock.patch.object(sarif, "merge_sarif") as operation:
                self.assertEqual(0, sarif.main(("merge", str(source), str(other))))
                operation.assert_called_once_with([source, other], output / "analysis.sarif")

            with mock.patch.object(sarif, "require_clean") as operation:
                self.assertEqual(0, sarif.main(("gate", str(source))))
                operation.assert_called_once_with((source,))
                self.assertEqual([], list(output.iterdir()))

    def test_cli_requires_existing_output_directory_and_confines_output_name(self) -> None:
        source = self.root / "raw.sarif"
        stderr = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                sarif.main(("gate", str(source)))
        self.assertIn("OBSERVER_OUT_DIR", stderr.getvalue())

        missing = self.root / "missing"
        stderr = io.StringIO()
        with mock.patch.dict("os.environ", {"OBSERVER_OUT_DIR": str(missing)}, clear=True), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                sarif.main(("gate", str(source)))
        self.assertIn("existing directory", stderr.getvalue())

        output = self.root / "out"
        output.mkdir()
        stderr = io.StringIO()
        with mock.patch.dict("os.environ", {"OBSERVER_OUT_DIR": str(output)}, clear=True), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                sarif.main(("normalize-msvc", str(source), "id", "--output-name", "../escape.sarif"))
        self.assertIn("confined", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

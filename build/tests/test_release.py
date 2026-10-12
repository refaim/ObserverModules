from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import stat
import tempfile
import unittest
from unittest import mock
import zipfile


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

import core.release as release_module  # noqa: E402
from core.module_version import MODULES  # noqa: E402
from core.release import (  # noqa: E402
    ARCHITECTURES,
    ReleaseError,
    evaluate,
    expected_archives,
    load_manifest,
    main as release_main,
    merge_manifests,
    parse_tag,
    recorded_previous_tag,
    resolve_previous,
    select_module,
    stable_release_tags,
    validate_tag,
    verify_assets,
)


VERSIONS = {"renpy": "3.1.0", "rpgmaker": "1.1.0", "zanzarah": "2.1.0"}
CHANGELOGS = {"en": "ChangeLog.en.txt", "ru": "ChangeLog.ru.txt"}


def identity(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def asset_bytes(name: str) -> bytes:
    return f"{name} payload".encode("utf-8")


def asset_sha(name: str) -> str:
    return hashlib.sha256(asset_bytes(name)).hexdigest()


def module_archive(module: str, version: str, architecture: str) -> str:
    return f"{module}-{version}-{architecture}.zip"


def symbol_archive(module: str, version: str) -> str:
    return f"{module}-{version}-pdb.zip"


def bundle_payload(module: str, architecture: str) -> bytes:
    return f"{module}-{architecture}-pdb".encode()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_bundle(path: Path, entries: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as bundle:
        for name, payload in entries.items():
            bundle.writestr(name, payload)
    return path


def manifest(architecture: str = "x64", **overrides: tuple[str, str]) -> dict[str, object]:
    modules = []
    archives = []
    for module in MODULES:
        version, content = overrides.get(module, (VERSIONS[module], identity(module)))
        modules.append({"content": content, "module": module, "version": version})
        name = module_archive(module, version, architecture)
        archives.append({"name": name, "sha256": asset_sha(name)})
    return {"archives": sorted(archives, key=lambda entry: entry["name"]), "modules": modules}


def write(root: Path, name: str, document: object) -> Path:
    path = root / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def changelog_entry(module: str, version: str, language: str) -> str:
    """A minimal but valid human ChangeLog whose top entry names ``version``."""

    header = f"{'Version' if language == 'en' else '\u0412\u0435\u0440\u0441\u0438\u044f'} {version}"
    return f"{header}\n{'-' * len(header)}\n * {module} {version} release.\n"


def make_repository(root: Path, versions: dict[str, str] | None = None) -> Path:
    for module, version in (versions or VERSIONS).items():
        version_path = root / "src" / "modules" / module / "VERSION"
        version_path.parent.mkdir(parents=True, exist_ok=True)
        version_path.write_text(f"{version}\n", encoding="utf-8")
        for language, name in CHANGELOGS.items():
            (version_path.parent / name).write_text(
                changelog_entry(module, version, language), encoding="utf-8"
            )
    return root


def write_assets(directory: Path, names: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(asset_bytes(name))


def release_record(tag: str, *, draft: bool = False, prerelease: bool = False) -> str:
    """One line of the ``gh api`` JSON that the workflow feeds to the ``previous`` command."""

    return json.dumps({"tag": tag, "draft": draft, "prerelease": prerelease})


def release_report(module: str, version: str, *, previous: object) -> dict[str, object]:
    """One module release report, as the immutable ``release.json`` of a publication."""

    return {
        "bootstrap": previous is None,
        "modules": [
            {
                "content": identity(module),
                "module": module,
                "previous_version": previous,
                "status": "bootstrap" if previous is None else "released",
                "version": version,
            }
        ],
        "note": "",
        "title": f"{module} {version}",
    }


class MergeTests(unittest.TestCase):
    def test_architecture_manifests_union_archives_and_agree_on_modules(self) -> None:
        merged = merge_manifests((manifest("x64"), manifest("arm64"), manifest("x86")))

        self.assertEqual(merged["modules"], manifest()["modules"])
        self.assertEqual(len(merged["archives"]), 9)
        self.assertEqual(
            [entry["name"] for entry in merged["archives"]],
            sorted(entry["name"] for entry in merged["archives"]),
        )

    def test_disagreeing_or_colliding_release_manifests_are_rejected(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "disagree about renpy"):
            merge_manifests((manifest("x64"), manifest("arm64", renpy=("3.2.0", identity("renpy")))))
        with self.assertRaisesRegex(ReleaseError, "duplicate release archive"):
            merge_manifests((manifest("x64"), manifest("x64")))
        with self.assertRaisesRegex(ReleaseError, "at least one release manifest"):
            merge_manifests(())

    def test_merging_selected_only_and_all_module_manifests_is_rejected(self) -> None:
        selected = {
            "archives": [{"name": module_archive("renpy", "3.1.0", "x64"),
                          "sha256": asset_sha(module_archive("renpy", "3.1.0", "x64"))}],
            "modules": [{"content": identity("renpy"), "module": "renpy", "version": "3.1.0"}],
        }
        with self.assertRaisesRegex(ReleaseError, "describe different module sets"):
            merge_manifests((selected, manifest("x64")), expected=None)


class ManifestValidationTests(unittest.TestCase):
    def test_only_a_complete_well_formed_manifest_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(load_manifest(write(root, "good.json", manifest())), manifest())

            partial = manifest()
            partial["modules"] = partial["modules"][:2]
            invalid = [
                ([], "must be a JSON object"),
                ({"modules": manifest()["modules"]}, "must be a JSON object"),
                (partial, "expected module set"),
                (
                    {
                        **manifest(),
                        "modules": [
                            {"content": identity("x"), "module": "renpy", "version": "1.0.0"}
                        ] * 3,
                    },
                    "expected module set",
                ),
                (
                    {
                        **manifest(),
                        "modules": [
                            *manifest()["modules"],
                            {"content": identity("x"), "module": "pickle", "version": "1.0.0"},
                        ],
                    },
                    "expected module set",
                ),
                ({**manifest(), "modules": ["renpy"]}, "module record must be an object"),
                ({**manifest(), "modules": "renpy"}, "must be a JSON object"),
                (
                    manifest(renpy=("3.1.0", "not-a-digest")),
                    "module content identity",
                ),
                (manifest(renpy=("3.1", identity("renpy"))), "must be X.Y.Z"),
                ({**manifest(), "archives": [{"name": "a.zip"}]}, "archive record"),
                (
                    {**manifest(), "archives": [{"name": "../escape.zip", "sha256": identity("a")}]},
                    "plain zip file name",
                ),
                (
                    {**manifest(), "archives": [{"name": "sub/dir.zip", "sha256": identity("a")}]},
                    "plain zip file name",
                ),
                (
                    {
                        **manifest(),
                        "archives": [
                            {"name": "a.zip", "sha256": identity("a")},
                            {"name": "a.zip", "sha256": identity("a")},
                        ],
                    },
                    "duplicate release archive",
                ),
            ]
            for index, (document, message) in enumerate(invalid):
                with self.subTest(index=index), self.assertRaisesRegex(ReleaseError, message):
                    load_manifest(write(root, f"bad-{index}.json", document))

            broken = root / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseError, "unreadable release manifest"):
                load_manifest(broken)
            with self.assertRaisesRegex(ReleaseError, "unreadable release manifest"):
                load_manifest(root / "absent.json")


class GateTests(unittest.TestCase):
    def statuses(self, current: dict[str, object], previous: dict[str, object] | None) -> dict[str, str]:
        report = evaluate(current, previous)
        return {record["module"]: record["status"] for record in report["modules"]}

    def test_a_first_release_without_a_previous_manifest_is_recorded_as_bootstrap(self) -> None:
        report = evaluate(manifest(), None)

        self.assertTrue(report["bootstrap"])
        self.assertEqual(
            self.statuses(manifest(), None), {module: "bootstrap" for module in MODULES}
        )
        self.assertEqual(report["title"], "renpy 3.1.0, rpgmaker 1.1.0, zanzarah 2.1.0")
        self.assertEqual([record["previous_version"] for record in report["modules"]], [None] * 3)
        self.assertIn("no previous release manifest", report["note"])

    def test_unchanged_and_correctly_bumped_modules_pass_the_gate(self) -> None:
        previous = manifest()
        current = manifest(renpy=("3.2.0", identity("renpy-next")))

        report = evaluate(current, previous)

        self.assertFalse(report["bootstrap"])
        self.assertEqual(
            {record["module"]: record["status"] for record in report["modules"]},
            {"renpy": "released", "rpgmaker": "unchanged", "zanzarah": "unchanged"},
        )
        self.assertEqual(report["modules"][0]["previous_version"], "3.1.0")
        self.assertEqual(report["title"], "renpy 3.2.0, rpgmaker 1.1.0, zanzarah 2.1.0")
        self.assertEqual(report["note"], "")

    def test_a_previous_release_may_outlive_a_retired_module(self) -> None:
        previous = manifest()
        previous["modules"] = [
            *previous["modules"],
            {"content": identity("pickle"), "module": "pickle", "version": "1.0.0"},
        ]

        self.assertEqual(
            self.statuses(manifest(), previous),
            {module: "unchanged" for module in MODULES},
        )
        with self.assertRaisesRegex(ReleaseError, "expected module set"):
            evaluate(previous, None)

    def test_a_module_absent_from_the_previous_release_is_new(self) -> None:
        previous = manifest()
        previous["modules"] = [
            record for record in previous["modules"] if record["module"] != "zanzarah"
        ]

        self.assertEqual(
            self.statuses(manifest(), previous),
            {"renpy": "unchanged", "rpgmaker": "unchanged", "zanzarah": "new"},
        )

    def test_changed_content_without_a_version_bump_fails_the_release(self) -> None:
        previous = manifest()
        current = manifest(rpgmaker=("1.1.0", identity("rpgmaker-next")))

        with self.assertRaisesRegex(ReleaseError, "rpgmaker content changed without a version bump"):
            evaluate(current, previous)

    def test_an_unchanged_module_may_not_be_republished_under_a_new_version(self) -> None:
        previous = manifest()
        current = manifest(zanzarah=("2.2.0", identity("zanzarah")))

        with self.assertRaisesRegex(ReleaseError, "zanzarah version bumped without a content change"):
            evaluate(current, previous)

    def test_a_version_may_never_move_backwards(self) -> None:
        previous = manifest()
        current = manifest(renpy=("3.0.9", identity("renpy-next")))

        with self.assertRaisesRegex(ReleaseError, "renpy version 3.0.9 is older than 3.1.0"):
            evaluate(current, previous)


class ReleaseCommandTests(unittest.TestCase):
    def run_gate(self, root: Path, name: str, *arguments: str) -> Path:
        output = root / name
        output.mkdir()
        self.assertEqual(0, release_main(("gate", "--output", str(output), *arguments)))
        return output

    def test_gate_publishes_the_merged_manifest_and_its_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = [
                str(write(root, f"packages-{architecture}.json", manifest(architecture)))
                for architecture in ("x64", "arm64")
            ]
            previous = str(write(root, "previous.json", manifest("x64")))

            bootstrap = self.run_gate(root, "bootstrap", *current)
            self.assertTrue(json.loads((bootstrap / "release.json").read_text())["bootstrap"])
            merged = json.loads((bootstrap / "packages.json").read_text(encoding="utf-8"))
            self.assertEqual(len(merged["archives"]), 6)
            self.assertEqual(merged["modules"], manifest()["modules"])
            notes = (bootstrap / "notes.md").read_text(encoding="utf-8")
            self.assertIn("| renpy | 3.1.0 | bootstrap |", notes)
            self.assertIn("no previous release manifest", notes)

            compared = self.run_gate(root, "compared", "--previous", previous, *current)
            report = json.loads((compared / "release.json").read_text(encoding="utf-8"))
            self.assertFalse(report["bootstrap"])
            self.assertEqual(
                {record["status"] for record in report["modules"]}, {"unchanged"}
            )
            notes = (compared / "notes.md").read_text(encoding="utf-8")
            self.assertIn("| zanzarah | 2.1.0 | unchanged |", notes)
            self.assertNotIn("no previous release manifest", notes)

            regressed = str(write(root, "regressed.json", manifest("x64", renpy=("3.9.0", identity("old")))))
            output = root / "failed"
            output.mkdir()
            with self.assertRaisesRegex(ReleaseError, "older than"):
                release_main(("gate", "--output", str(output), "--previous", regressed, *current))
            self.assertFalse((output / "release.json").exists())

            with self.assertRaisesRegex(ReleaseError, "output directory"):
                release_main(("gate", "--output", str(root / "absent"), *current))

    def test_gate_command_writes_the_merged_release_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = str(write(root, "packages-x64.json", manifest("x64")))
            output = root / "entrypoint"
            output.mkdir()
            self.assertEqual(0, release_main(("gate", "--output", str(output), current)))
            self.assertTrue((output / "release.json").is_file())

        with self.assertRaises(SystemExit):
            release_main(())


class TagTests(unittest.TestCase):
    def test_canonical_module_tags_map_to_their_declared_version(self) -> None:
        for module, version in VERSIONS.items():
            with self.subTest(module=module), tempfile.TemporaryDirectory() as temporary:
                repository = make_repository(Path(temporary))
                self.assertEqual(parse_tag(f"{module}/v{version}"), (module, version))
                self.assertEqual(validate_tag(repository, f"{module}/v{version}"), (module, version))

    def test_unknown_malformed_and_mismatched_tags_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = make_repository(Path(temporary))
            invalid = [
                ("renpy", "release tag must be"),
                ("renpy/v3.1", "version must be X.Y.Z"),
                ("renpy/v3.1.0/v2", "version must be X.Y.Z"),
                ("unknown/v1.0.0", "unknown release module"),
            ]
            for tag, message in invalid:
                with self.subTest(tag=tag), self.assertRaisesRegex(ReleaseError, message):
                    parse_tag(tag)
            with self.assertRaisesRegex(ReleaseError, "does not match"):
                validate_tag(repository, "renpy/v9.9.9")

    def test_a_missing_version_file_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ReleaseError, "unreadable"):
                validate_tag(Path(temporary), "renpy/v3.1.0")

    def test_either_language_change_log_disagreeing_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = make_repository(Path(temporary))
            for index, name in enumerate(CHANGELOGS.values()):
                changelog = repository / "src/modules/renpy" / name
                original = changelog.read_bytes()
                changelog.unlink()
                with self.subTest(missing=name), self.assertRaisesRegex(ReleaseError, "ChangeLog"):
                    validate_tag(repository, "renpy/v3.1.0")
                changelog.write_bytes(b"Version 3.0.0\n-------------\n * old\n")
                with self.subTest(stale=name), self.assertRaisesRegex(ReleaseError, "ChangeLog"):
                    validate_tag(repository, "renpy/v3.1.0")
                changelog.write_bytes(original)

    def test_tag_command_writes_the_selected_module_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = make_repository(root)
            output = root / "github-output.txt"
            output.write_text("", encoding="utf-8")

            self.assertEqual(
                0,
                release_main(
                    (
                        "tag",
                        "--repository",
                        str(repository),
                        "--tag",
                        "renpy/v3.1.0",
                        "--github-output",
                        str(output),
                    )
                ),
            )
            self.assertEqual(output.read_text(encoding="utf-8"), "module=renpy\nversion=3.1.0\n")

            self.assertEqual(
                0,
                release_main(("tag", "--repository", str(repository), "--tag", "rpgmaker/v1.1.0")),
            )


class PreviousReleaseTests(unittest.TestCase):
    def test_only_published_stable_release_records_feed_the_lineage(self) -> None:
        lines = [
            release_record("renpy/v3.2.0"),
            release_record("renpy/v3.3.0", draft=True),
            release_record("renpy/v3.4.0", prerelease=True),
            release_record("renpy/v3.5.0", draft=True, prerelease=True),
            "",
            "   ",
            "not json",
            '["renpy/v9.9.9"]',
            '{"draft": false, "prerelease": false}',
            '{"tag": 123, "draft": false, "prerelease": false}',
            release_record("renpy/v3.3.1"),
        ]

        self.assertEqual(stable_release_tags(lines), ["renpy/v3.2.0", "renpy/v3.3.1"])

    def test_a_new_release_compares_against_the_latest_published_head(self) -> None:
        self.assertEqual(
            resolve_previous("renpy/v3.1.0", ["renpy/v3.2.0", "renpy/v3.0.0"]),
            "renpy/v3.2.0",
        )

    def test_a_new_release_with_only_lower_history_resolves_its_predecessor(self) -> None:
        self.assertEqual(resolve_previous("renpy/v3.1.0", ["renpy/v3.0.0"]), "renpy/v3.0.0")

    def test_no_published_same_module_release_is_a_genuine_bootstrap(self) -> None:
        for releases in ([], ["rpgmaker/v1.0.0"], ["renpy/v3.1.0"]):
            with self.subTest(releases=releases):
                self.assertIsNone(resolve_previous("renpy/v3.1.0", releases))

    def test_unrelated_and_unparseable_release_tags_are_ignored(self) -> None:
        releases = ["nightly", "renpy/3.0.0", "renpy/v3.0.0"]

        self.assertEqual(resolve_previous("renpy/v3.1.0", releases), "renpy/v3.0.0")

    def test_a_new_publication_command_writes_the_resolved_plain_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases = root / "releases.jsonl"
            releases.write_text(
                "\n".join(
                    [
                        release_record("renpy/v3.0.9", draft=True),
                        release_record("renpy/v3.0.8", prerelease=True),
                        release_record("renpy/v3.0.0"),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "previous.txt"

            self.assertEqual(
                0,
                release_main(
                    (
                        "previous",
                        "--tag",
                        "renpy/v3.1.0",
                        "--releases",
                        str(releases),
                        "--output",
                        str(output),
                    )
                ),
            )

            self.assertEqual(output.read_text(encoding="utf-8"), "renpy/v3.0.0\n")

    def test_a_new_publication_command_writes_an_empty_file_for_a_first_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases = root / "releases.jsonl"
            releases.write_text("\n", encoding="utf-8")
            output = root / "previous.txt"

            self.assertEqual(
                0,
                release_main(
                    (
                        "previous",
                        "--tag",
                        "renpy/v3.1.0",
                        "--releases",
                        str(releases),
                        "--output",
                        str(output),
                    )
                ),
            )

            self.assertEqual(output.read_text(encoding="utf-8"), "\n")

    def test_a_rerun_command_uses_the_recorded_predecessor_ignoring_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = write(
                root, "release.json", release_report("renpy", "3.1.0", previous="3.0.5")
            )
            releases = root / "releases.jsonl"
            releases.write_text(
                "\n".join(
                    [
                        release_record("renpy/v3.2.0"),
                        release_record("renpy/v3.1.0"),
                        release_record("renpy/v3.0.0"),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "previous.txt"

            self.assertEqual(
                0,
                release_main(
                    (
                        "previous",
                        "--tag",
                        "renpy/v3.1.0",
                        "--current-exists",
                        "--current-report",
                        str(report),
                        "--releases",
                        str(releases),
                        "--output",
                        str(output),
                    )
                ),
            )

            self.assertEqual(output.read_text(encoding="utf-8"), "renpy/v3.0.5\n")

    def test_a_bootstrap_rerun_ignores_a_later_inserted_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = write(
                root, "release.json", release_report("renpy", "3.1.0", previous=None)
            )
            releases = root / "releases.jsonl"
            releases.write_text(release_record("renpy/v3.0.5") + "\n", encoding="utf-8")
            output = root / "previous.txt"

            self.assertEqual(
                0,
                release_main(
                    (
                        "previous",
                        "--tag",
                        "renpy/v3.1.0",
                        "--current-exists",
                        "--current-report",
                        str(report),
                        "--releases",
                        str(releases),
                        "--output",
                        str(output),
                    )
                ),
            )

            self.assertEqual(output.read_text(encoding="utf-8"), "\n")

    def test_the_previous_command_requires_a_recorded_report_for_a_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "previous.txt"
            with self.assertRaisesRegex(ReleaseError, "requires --current-report"):
                release_main(
                    (
                        "previous",
                        "--tag",
                        "renpy/v3.1.0",
                        "--current-exists",
                        "--output",
                        str(output),
                    )
                )

    def test_the_previous_command_requires_releases_for_a_new_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "previous.txt"
            with self.assertRaisesRegex(ReleaseError, "requires --releases"):
                release_main(("previous", "--tag", "renpy/v3.1.0", "--output", str(output)))

    def test_the_previous_command_rejects_a_missing_or_malformed_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broken = root / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            output = root / "previous.txt"
            for report in (root / "absent.json", broken):
                with self.subTest(report=report.name):
                    with self.assertRaisesRegex(
                        ReleaseError, "unreadable current release report"
                    ):
                        release_main(
                            (
                                "previous",
                                "--tag",
                                "renpy/v3.1.0",
                                "--current-exists",
                                "--current-report",
                                str(report),
                                "--output",
                                str(output),
                            )
                        )

    def test_the_previous_command_rejects_a_report_without_the_current_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = write(
                root, "release.json", release_report("renpy", "3.1.0", previous="3.0.0")
            )
            output = root / "previous.txt"
            with self.assertRaisesRegex(
                ReleaseError, "--current-report requires --current-exists"
            ):
                release_main(
                    (
                        "previous",
                        "--tag",
                        "renpy/v3.1.0",
                        "--current-report",
                        str(report),
                        "--output",
                        str(output),
                    )
                )


class RecordedPreviousReportTests(unittest.TestCase):
    def test_a_bootstrap_report_records_no_predecessor(self) -> None:
        self.assertIsNone(
            recorded_previous_tag("renpy/v3.1.0", release_report("renpy", "3.1.0", previous=None))
        )

    def test_a_report_records_its_older_predecessor_tag(self) -> None:
        self.assertEqual(
            recorded_previous_tag(
                "renpy/v3.1.0", release_report("renpy", "3.1.0", previous="3.0.0")
            ),
            "renpy/v3.0.0",
        )

    def test_a_report_that_is_not_an_object_is_rejected(self) -> None:
        for report in ([], "renpy", 7, None):
            with self.subTest(report=report), self.assertRaisesRegex(
                ReleaseError, "must be a JSON object"
            ):
                recorded_previous_tag("renpy/v3.1.0", report)

    def test_a_report_without_a_module_list_is_rejected(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "must describe its modules"):
            recorded_previous_tag("renpy/v3.1.0", {"modules": "renpy"})

    def test_a_report_must_describe_exactly_the_selected_module(self) -> None:
        duplicated = release_report("renpy", "3.1.0", previous=None)
        duplicated["modules"] = duplicated["modules"] * 2
        empty = release_report("renpy", "3.1.0", previous=None)
        empty["modules"] = []
        for report in (
            empty,
            release_report("rpgmaker", "1.1.0", previous=None),
            duplicated,
        ):
            with self.subTest(report=report), self.assertRaisesRegex(
                ReleaseError, "exactly one 'renpy' module"
            ):
                recorded_previous_tag("renpy/v3.1.0", report)

    def test_a_report_version_must_match_the_tag(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "version does not match the tag"):
            recorded_previous_tag(
                "renpy/v3.1.0", release_report("renpy", "3.2.0", previous="3.1.0")
            )

    def test_a_report_without_a_previous_version_is_rejected(self) -> None:
        report = release_report("renpy", "3.1.0", previous=None)
        del report["modules"][0]["previous_version"]
        with self.assertRaisesRegex(ReleaseError, "records no previous_version"):
            recorded_previous_tag("renpy/v3.1.0", report)

    def test_an_invalid_previous_version_is_rejected(self) -> None:
        for previous in ("next", 3):
            with self.subTest(previous=previous), self.assertRaisesRegex(
                ReleaseError, "invalid previous_version"
            ):
                recorded_previous_tag(
                    "renpy/v3.1.0", release_report("renpy", "3.1.0", previous=previous)
                )

    def test_a_previous_version_that_is_not_older_is_rejected(self) -> None:
        for previous in ("3.1.0", "3.2.0"):
            with self.subTest(previous=previous), self.assertRaisesRegex(
                ReleaseError, "is not older than"
            ):
                recorded_previous_tag(
                    "renpy/v3.1.0", release_report("renpy", "3.1.0", previous=previous)
                )


class ImplementationPathTests(unittest.TestCase):
    def test_the_release_module_is_loaded_from_the_tested_build_tree(self) -> None:
        self.assertEqual(
            Path(release_module.__file__).resolve(),
            (BUILD_ROOT / "core" / "release.py").resolve(),
        )


class SelectModuleTests(unittest.TestCase):
    def test_selecting_a_module_keeps_only_its_records_and_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = load_manifest(write(Path(temporary), "packages.json", manifest()))

            selected = select_module(document, "renpy")

            self.assertEqual([record["module"] for record in selected["modules"]], ["renpy"])
            self.assertEqual(
                [entry["name"] for entry in selected["archives"]],
                [module_archive("renpy", "3.1.0", "x64")],
            )

    def test_selecting_a_module_absent_from_the_manifest_fails(self) -> None:
        document = {"archives": [], "modules": [{"content": identity("x"), "module": "rpgmaker", "version": "1.1.0"}]}

        with self.assertRaisesRegex(ReleaseError, "has no 'renpy' record"):
            select_module(document, "renpy")


class VerifyAssetsTests(unittest.TestCase):
    def test_assets_directory_must_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ReleaseError, "assets directory must exist"):
                verify_assets(Path(temporary) / "absent", [])

    def test_assets_must_match_the_manifest_bytes_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = list(expected_archives("renpy", "3.1.0"))
            archives = [{"name": name, "sha256": asset_sha(name)} for name in names]
            assets = root / "assets"
            write_assets(assets, names)

            self.assertIsNone(verify_assets(assets, archives))

            (assets / "renpy-3.1.0-arm64.zip").write_bytes(asset_bytes("renpy-3.1.0-arm64.zip"))
            (assets / "renpy-3.0.0-x86.zip").write_bytes(b"foreign")
            with self.assertRaisesRegex(ReleaseError, "do not match the module archives"):
                verify_assets(assets, archives)
            (assets / "renpy-3.0.0-x86.zip").unlink()

            (assets / names[0]).unlink()
            with self.assertRaisesRegex(ReleaseError, "do not match the module archives"):
                verify_assets(assets, archives)
            (assets / names[0]).write_bytes(b"tampered")
            with self.assertRaisesRegex(ReleaseError, "digest mismatch"):
                verify_assets(assets, archives)


class ExpectedArchiveTests(unittest.TestCase):
    def test_expected_archives_cover_every_architecture_and_one_symbol_bundle(self) -> None:
        expected = expected_archives("renpy", "3.1.0")

        self.assertEqual(len(expected), len(ARCHITECTURES) + 1)
        self.assertIn(module_archive("renpy", "3.1.0", "arm64"), expected)
        self.assertIn(module_archive("renpy", "3.1.0", "x86"), expected)
        self.assertEqual(expected[-1], symbol_archive("renpy", "3.1.0"))


class ModuleSymbolBundlesTests(unittest.TestCase):
    def entries(self, root: Path, names: dict[str, bytes]) -> Path:
        path = root / "renpy-3.1.0-pdb.zip"
        with zipfile.ZipFile(path, "w") as bundle:
            for name, payload in names.items():
                bundle.writestr(name, payload)
        return path

    def test_a_bundle_maps_each_architecture_to_its_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.entries(root, {
                "x64/renpy.pdb": b"x64",
                "arm64/renpy.pdb": b"arm64",
            })

            self.assertEqual(
                release_module._symbol_bundle_entries(path, "renpy"),
                {"x64": b"x64", "arm64": b"arm64"},
            )

    def test_a_bundle_must_carry_at_least_one_known_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ReleaseError, "empty"):
                release_module._symbol_bundle_entries(self.entries(root, {}), "renpy")

    def test_duplicate_and_foreign_entries_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            duplicate = root / "duplicate.zip"
            with zipfile.ZipFile(duplicate, "w") as bundle:
                bundle.writestr("x64/renpy.pdb", b"first")
                bundle.writestr("x64/renpy.pdb", b"second")
            with self.assertRaisesRegex(ReleaseError, "repeats architecture x64"):
                release_module._symbol_bundle_entries(duplicate, "renpy")

            foreign = self.entries(root, {"riscv64/renpy.pdb": b"x"})
            with self.assertRaisesRegex(ReleaseError, "must be <arch>/renpy.pdb"):
                release_module._symbol_bundle_entries(foreign, "renpy")

            wrong_file = self.entries(root, {"x64/rpgmaker.pdb": b"x"})
            with self.assertRaisesRegex(ReleaseError, "must be <arch>/renpy.pdb"):
                release_module._symbol_bundle_entries(wrong_file, "renpy")

            traversal = self.entries(root, {"../renpy.pdb": b"x"})
            with self.assertRaisesRegex(ReleaseError, "must be <arch>/renpy.pdb"):
                release_module._symbol_bundle_entries(traversal, "renpy")

    def test_a_linked_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "linked.zip"
            information = zipfile.ZipInfo("x64/renpy.pdb")
            information.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr(information, "target")
            with self.assertRaisesRegex(ReleaseError, "plain file"):
                release_module._symbol_bundle_entries(path, "renpy")

    def test_an_oversized_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.entries(root, {"x64/renpy.pdb": b"0123456789"})
            with mock.patch.object(release_module, "_MAXIMUM_SYMBOL_BYTES", 4):
                with self.assertRaisesRegex(ReleaseError, "too large"):
                    release_module._symbol_bundle_entries(path, "renpy")

    def test_an_invalid_bundle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broken = root / "broken.zip"
            broken.write_bytes(b"not a zip")
            with self.assertRaisesRegex(ReleaseError, "unreadable"):
                release_module._symbol_bundle_entries(broken, "renpy")


class ModuleReleaseTests(unittest.TestCase):
    def ci_manifests(
        self,
        root: Path,
        *,
        module: str = "renpy",
        version: str | None = None,
        versions: dict[str, str] | None = None,
        modules: tuple[str, ...] = MODULES,
        symbol_architectures: tuple[str, ...] = ARCHITECTURES,
        contents: dict[str, str] | None = None,
        prefix: str = "packages",
    ) -> tuple[list[str], Path]:
        """Write one ``packages-<arch>`` tree per architecture and the flat asset directory."""
        versions = versions or dict(VERSIONS)
        version = version or versions[module]
        contents = {name: identity(name) for name in modules} | dict(contents or {})
        manifests: list[str] = []
        assets = root / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        for architecture in ARCHITECTURES:
            directory = root / f"{prefix}-{architecture}"
            directory.mkdir(parents=True, exist_ok=True)
            archives = []
            for module_name in modules:
                name = module_archive(module_name, versions[module_name], architecture)
                archives.append({"name": name, "sha256": asset_sha(name)})
                if module_name == module:
                    (assets / name).write_bytes(asset_bytes(name))
                if module_name == module and architecture not in symbol_architectures:
                    continue
                symbol_name = symbol_archive(module_name, versions[module_name])
                bundle = directory / symbol_name
                write_bundle(
                    bundle,
                    {f"{architecture}/{module_name}.pdb": bundle_payload(module_name, architecture)},
                )
                archives.append({"name": symbol_name, "sha256": digest(bundle)})
            document = {
                "archives": sorted(archives, key=lambda entry: entry["name"]),
                "modules": [
                    {"content": contents[name], "module": name, "version": versions[name]}
                    for name in modules
                ],
            }
            manifests.append(str(write(directory, "packages.json", document)))
        return manifests, assets

    def require(self, root: Path, name: str, *arguments: str) -> Path:
        output = root / name
        output.mkdir()
        self.assertEqual(0, release_main(("gate", "--output", str(output), *arguments)))
        return output

    def gate(self, root: Path, name: str, module: str, tag: str, assets: Path, *extra: str) -> int:
        output = root / name
        output.mkdir()
        return release_main((
            "gate", "--output", str(output), "--module", module, "--tag", tag,
            "--repository", str(root), "--assets", str(assets), *extra,
        ))

    def test_the_gate_assembles_the_final_symbol_bundle_and_four_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root)

            output = self.require(
                root, "gate",
                "--module", "renpy", "--tag", "renpy/v3.1.0",
                "--repository", str(root), "--assets", str(assets), *manifests,
            )

            packages = json.loads((output / "packages.json").read_text(encoding="utf-8"))
            self.assertEqual([record["module"] for record in packages["modules"]], ["renpy"])
            self.assertEqual(
                [entry["name"] for entry in packages["archives"]],
                sorted(expected_archives("renpy", "3.1.0")),
            )
            bundle = assets / symbol_archive("renpy", "3.1.0")
            with zipfile.ZipFile(bundle) as archive:
                self.assertEqual(
                    archive.namelist(),
                    ["arm64/renpy.pdb", "x64/renpy.pdb", "x86/renpy.pdb"],
                )
                self.assertEqual(archive.read("x64/renpy.pdb"), bundle_payload("renpy", "x64"))
            report = json.loads((output / "release.json").read_text(encoding="utf-8"))
            self.assertTrue(report["bootstrap"])
            notes = (output / "notes.md").read_text(encoding="utf-8")
            self.assertIn("| renpy | 3.1.0 | bootstrap |", notes)
            # The published body carries both language sections.
            self.assertIn("Version 3.1.0", notes)
            self.assertIn("\u0412\u0435\u0440\u0441\u0438\u044f 3.1.0", notes)
            self.assertIn("- renpy 3.1.0 release.", notes)

    def test_a_single_all_architecture_manifest_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            assets = root / "assets"
            assets.mkdir()
            archives = []
            for architecture in ARCHITECTURES:
                name = module_archive("renpy", "3.1.0", architecture)
                archives.append({"name": name, "sha256": asset_sha(name)})
                (assets / name).write_bytes(asset_bytes(name))
            bundle_name = symbol_archive("renpy", "3.1.0")
            bundle = root / bundle_name
            write_bundle(bundle, {
                f"{architecture}/renpy.pdb": bundle_payload("renpy", architecture)
                for architecture in ARCHITECTURES
            })
            archives.append({"name": bundle_name, "sha256": digest(bundle)})
            document = {
                "archives": sorted(archives, key=lambda entry: entry["name"]),
                "modules": [{"content": identity("renpy"), "module": "renpy", "version": "3.1.0"}],
            }
            path = write(root, "packages.json", document)

            self.require(
                root, "gate",
                "--module", "renpy", "--tag", "renpy/v3.1.0",
                "--repository", str(root), "--assets", str(assets), str(path),
            )

            with zipfile.ZipFile(assets / bundle_name) as archive:
                self.assertEqual(
                    archive.namelist(),
                    ["arm64/renpy.pdb", "x64/renpy.pdb", "x86/renpy.pdb"],
                )

    def test_a_missing_architecture_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root, symbol_architectures=("x86", "x64"))

            with self.assertRaisesRegex(ReleaseError, "x86, x64, arm64 symbol bundles"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)

    def test_repeated_symbol_bytes_across_architectures_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root)
            # Make arm64 ship the x64 bytes, so the architecture set is complete but repeated.
            arm = root / "packages-arm64" / symbol_archive("renpy", "3.1.0")
            write_bundle(arm, {"arm64/renpy.pdb": bundle_payload("renpy", "x64")})
            document = json.loads(Path(manifests[2]).read_text(encoding="utf-8"))
            for entry in document["archives"]:
                if entry["name"] == symbol_archive("renpy", "3.1.0"):
                    entry["sha256"] = digest(arm)
            Path(manifests[2]).write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(ReleaseError, "repeats symbol bytes"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)

    def test_a_repeated_architecture_inside_one_bundle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root)
            duplicate = root / "packages-x86" / symbol_archive("renpy", "3.1.0")
            with zipfile.ZipFile(duplicate, "w") as bundle:
                bundle.writestr("x86/renpy.pdb", b"a")
                bundle.writestr("x86/renpy.pdb", b"b")
            document = json.loads(Path(manifests[0]).read_text(encoding="utf-8"))
            for entry in document["archives"]:
                if entry["name"] == symbol_archive("renpy", "3.1.0"):
                    entry["sha256"] = digest(duplicate)
            Path(manifests[0]).write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(ReleaseError, "repeats architecture x86"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)

    def test_a_symbol_digest_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root)
            document = json.loads(Path(manifests[0]).read_text(encoding="utf-8"))
            for entry in document["archives"]:
                if entry["name"] == symbol_archive("renpy", "3.1.0"):
                    entry["sha256"] = identity("tampered")
            Path(manifests[0]).write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(ReleaseError, "digest mismatch"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)

    def test_a_missing_symbol_bundle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root)
            (root / "packages-x64" / symbol_archive("renpy", "3.1.0")).unlink()

            with self.assertRaisesRegex(ReleaseError, "missing"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)

    def test_a_link_symbol_bundle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root)
            bundle = root / "packages-x64" / symbol_archive("renpy", "3.1.0")
            outside = root / "outside.bin"
            outside.write_bytes(b"sentinel")
            bundle.unlink()
            try:
                import os
                os.symlink(outside, bundle)
            except (OSError, NotImplementedError):
                self.skipTest("host does not support symlink creation")

            with self.assertRaisesRegex(ReleaseError, "regular file"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)
            self.assertEqual(outside.read_bytes(), b"sentinel")

    def test_a_symbol_bundle_that_is_not_a_regular_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root)
            bundle = root / "packages-x64" / symbol_archive("renpy", "3.1.0")
            bundle.unlink()
            bundle.mkdir()

            with self.assertRaisesRegex(ReleaseError, "regular file"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)

    def test_an_assembled_bundle_that_is_a_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root)
            (assets / symbol_archive("renpy", "3.1.0")).mkdir()

            with self.assertRaisesRegex(ReleaseError, "is a directory"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)

    def test_a_linked_assembled_bundle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root)
            outside = root / "outside.bin"
            outside.write_bytes(b"sentinel")
            bundle = assets / symbol_archive("renpy", "3.1.0")
            try:
                os.symlink(outside, bundle)
            except (OSError, NotImplementedError):
                self.skipTest("host does not support symlink creation")

            with self.assertRaisesRegex(ReleaseError, "must not be a link"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)
            # The external file the pre-existing link pointed at was never followed.
            self.assertEqual(outside.read_bytes(), b"sentinel")

    def test_a_linked_assets_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, _assets = self.ci_manifests(root)
            outside = root / "outside"
            outside.mkdir()
            for architecture in ARCHITECTURES:
                name = module_archive("renpy", "3.1.0", architecture)
                (outside / name).write_bytes(asset_bytes(name))
            linked = root / "linked-assets"
            try:
                os.symlink(outside, linked, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("host does not support symlink creation")

            with self.assertRaisesRegex(ReleaseError, "must not be a link"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", linked, *manifests)
            # The linked directory stays exactly the module archives it held before the gate.
            self.assertEqual(
                sorted(path.name for path in outside.iterdir()),
                sorted(module_archive("renpy", "3.1.0", architecture) for architecture in ARCHITECTURES),
            )

    def test_a_linked_ancestor_of_the_assets_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, _assets = self.ci_manifests(root)
            outside = root / "outside"
            staged = outside / "assets"
            staged.mkdir(parents=True)
            for architecture in ARCHITECTURES:
                name = module_archive("renpy", "3.1.0", architecture)
                (staged / name).write_bytes(asset_bytes(name))
            sentinel = staged / symbol_archive("renpy", "3.1.0")
            sentinel.write_bytes(b"sentinel")
            linked = root / "linked-parent"
            try:
                os.symlink(outside, linked, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("host does not support symlink creation")
            # ``assets`` itself is a normal directory reached only through the linked parent.
            assets = linked / "assets"

            with self.assertRaisesRegex(ReleaseError, "must not be a link"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)
            # The linked ancestor was refused before the ZIP writer could follow it and
            # overwrite the external sentinel bytes.
            self.assertEqual(sentinel.read_bytes(), b"sentinel")

    def test_a_pre_existing_regular_symbol_bundle_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root)
            bundle = assets / symbol_archive("renpy", "3.1.0")
            bundle.write_bytes(b"stale local all-architecture bundle")

            self.assertEqual(0, self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests))
            with zipfile.ZipFile(bundle) as archive:
                self.assertEqual(
                    archive.namelist(),
                    ["arm64/renpy.pdb", "x64/renpy.pdb", "x86/renpy.pdb"],
                )

    def test_a_duplicate_architecture_across_manifests_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root)
            # The x64 manifest repeats the x86 architecture instead of shipping x64.
            x64 = root / "packages-x64" / symbol_archive("renpy", "3.1.0")
            write_bundle(x64, {"x86/renpy.pdb": b"x86-again"})
            document = json.loads(Path(manifests[1]).read_text(encoding="utf-8"))
            for entry in document["archives"]:
                if entry["name"] == symbol_archive("renpy", "3.1.0"):
                    entry["sha256"] = digest(x64)
            Path(manifests[1]).write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(ReleaseError, "ships architecture x86 more than once"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)

    def test_an_unexpected_selected_archive_record_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root)
            document = json.loads(Path(manifests[0]).read_text(encoding="utf-8"))
            document["archives"].append(
                {"name": "renpy-3.1.0-x86-extra.zip", "sha256": identity("extra")}
            )
            document["archives"].sort(key=lambda entry: entry["name"])
            Path(manifests[0]).write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(
                ReleaseError, "must be exactly its module and symbol packages"
            ):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)

    def test_the_final_bundle_must_cover_exactly_the_release_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root)
            # A foreign module archive living beside the manifest is never published, but a
            # module archive for the selected module with the wrong version is.
            (assets / module_archive("renpy", "3.0.0", "x64")).write_bytes(b"foreign")

            with self.assertRaisesRegex(ReleaseError, "do not match the module archives"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)

    def test_the_gate_requires_the_selector_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, _assets = self.ci_manifests(root)
            output = root / "gate"
            output.mkdir()

            with self.assertRaisesRegex(ReleaseError, "requires --tag, --repository, and --assets"):
                release_main(("gate", "--output", str(output), "--module", "renpy", *manifests))

    def test_the_gate_rejects_a_tag_that_selects_another_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root)

            with self.assertRaisesRegex(ReleaseError, "selects 'rpgmaker', not 'renpy'"):
                self.gate(root, "gate", "renpy", "rpgmaker/v1.1.0", assets, *manifests)

    def test_the_gate_rejects_a_tag_that_disagrees_with_the_package_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(
                root, version="3.2.0", versions={**VERSIONS, "renpy": "3.2.0"},
            )

            with self.assertRaisesRegex(ReleaseError, "does not match package version"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)

    def test_the_gate_fails_closed_when_the_change_log_disagrees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            (root / "src/modules/renpy" / CHANGELOGS["ru"]).write_text(
                changelog_entry("renpy", "3.0.0", "ru"), encoding="utf-8"
            )
            manifests, assets = self.ci_manifests(root)

            with self.assertRaisesRegex(ReleaseError, "ChangeLog"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)
            self.assertFalse((root / "gate" / "release.json").exists())

    def test_the_gate_follows_only_the_selected_module_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary), {**VERSIONS, "renpy": "3.2.0"})
            previous = str(write(root, "previous.json", manifest("x64")))
            manifests, assets = self.ci_manifests(
                root, version="3.2.0", versions={**VERSIONS, "renpy": "3.2.0"},
                contents={"renpy": identity("renpy-next")},
            )

            output = self.require(
                root, "gate",
                "--module", "renpy", "--tag", "renpy/v3.2.0",
                "--repository", str(root), "--assets", str(assets),
                "--previous", previous, *manifests,
            )

            report = json.loads((output / "release.json").read_text(encoding="utf-8"))
            self.assertFalse(report["bootstrap"])
            self.assertEqual(
                report["modules"],
                [
                    {
                        "content": identity("renpy-next"),
                        "module": "renpy",
                        "previous_version": "3.1.0",
                        "status": "released",
                        "version": "3.2.0",
                    }
                ],
            )

    def test_the_gate_rejects_a_content_change_without_a_bump(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            previous = str(write(root, "previous.json", manifest("x64")))
            manifests, assets = self.ci_manifests(root, contents={"renpy": identity("renpy-next")})

            with self.assertRaisesRegex(ReleaseError, "content changed without a version bump"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests, "--previous", previous)

    def test_the_gate_rejects_a_release_behind_the_published_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            previous = str(
                write(root, "previous.json", manifest("x64", renpy=("3.2.0", identity("renpy-head"))))
            )
            manifests, assets = self.ci_manifests(root)

            with self.assertRaisesRegex(ReleaseError, "older than 3.2.0"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests, "--previous", previous)

    def test_the_gate_requires_a_previous_manifest_to_describe_the_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            previous = {
                "archives": [],
                "modules": [{"content": identity("rpgmaker"), "module": "rpgmaker", "version": "1.1.0"}],
            }
            path = str(write(root, "previous.json", previous))
            manifests, assets = self.ci_manifests(root)

            with self.assertRaisesRegex(ReleaseError, "does not describe 'renpy'"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests, "--previous", path)

    def test_the_gate_requires_the_assets_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, _assets = self.ci_manifests(root)

            with self.assertRaisesRegex(ReleaseError, "assets directory must exist"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", root / "missing", *manifests)

    def test_the_gate_rejects_mixed_selected_and_all_module_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_repository(Path(temporary))
            manifests, assets = self.ci_manifests(root, modules=("renpy",), prefix="packages")
            # The arm64 manifest describes the full module set while the others describe one.
            other, _ = self.ci_manifests(root, modules=MODULES, prefix="full")
            manifests = [manifests[0], manifests[1], other[2]]

            with self.assertRaisesRegex(ReleaseError, "describe different module sets"):
                self.gate(root, "gate", "renpy", "renpy/v3.1.0", assets, *manifests)


if __name__ == "__main__":
    unittest.main(verbosity=2)

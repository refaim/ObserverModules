"""Standalone worker tests for the ``core.package`` packaging actions.

These assertions drive the real stage, archive, aggregate and validation CLI actions against a
materialised repository fixture, so the packaging behaviour stays covered.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.package import PackageError, licenses_document, main as package_main  # noqa: E402


MODULES = ("renpy", "rpgmaker", "zanzarah")
VERSIONS = {"renpy": "3.1.0", "rpgmaker": "1.1.0", "zanzarah": "2.1.0"}
LICENSES = {
    "renpy": ("Observer.txt", "rpatool.txt", "serde-pickle.txt", "zlib.txt"),
    "rpgmaker": ("Observer.txt", "rgssad.txt"),
    "zanzarah": ("Observer.txt", "zanzapak.txt"),
}
CHANGELOGS = ("ChangeLog.en.txt", "ChangeLog.ru.txt")
SEPARATOR = "=" * 72
PAYLOAD = ("ChangeLog.en.txt", "ChangeLog.ru.txt", "LICENSES.txt", "observer_user.ini", "renpy.so")
PROJECT = """<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemDefinitionGroup>
    <Link>
      <ModuleDefinitionFile>$(RepositoryRoot)src\\modules\\{module}\\{module}.def</ModuleDefinitionFile>
    </Link>
  </ItemDefinitionGroup>
  <ItemGroup>
    <ClCompile Include="$(RepositoryRoot)src\\modules\\{module}\\{module}.cpp" />
    <ClInclude Include="$(RepositoryRoot)src\\modules\\extractor.h" />
  </ItemGroup>
</Project>
"""


def write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def entry(version: str, note: str, language: str = "en") -> str:
    header = f"{'Version' if language == 'en' else '\u0412\u0435\u0440\u0441\u0438\u044f'} {version}"
    return f"{header}\n{'-' * len(header)}\n * {note}\n"


def section(title: str, content: bytes) -> bytes:
    heading = f"{SEPARATOR}\n{title}\n{SEPARATOR}\n\n".encode("utf-8")
    body = content if content.endswith(b"\n") else content + b"\n"
    return heading + body


class PackageActionTests(unittest.TestCase):
    def repository(self, root: Path) -> Path:
        for module, licenses in LICENSES.items():
            write(root, f"src/modules/{module}/observer_user.ini", f"[{module}]\n")
            write(root, f"src/modules/{module}/{module}.cpp", f"// {module}\n")
            write(root, f"src/modules/{module}/{module}.def", "EXPORTS\n")
            write(root, f"src/modules/{module}/VERSION", f"{VERSIONS[module]}\n")
            for name, language in zip(CHANGELOGS, ("en", "ru"), strict=True):
                write(
                    root,
                    f"src/modules/{module}/{name}",
                    entry(VERSIONS[module], f"{module} {language} release.", language),
                )
            write(root, f"build/projects/{module}.vcxproj", PROJECT.format(module=module))
            for name in licenses:
                write(root, f"licenses/{name}", f"license:{name}\n")
            write(root, f"src/modules/{module}/{module}_private.h", "#pragma once\n")
        write(root, "src/modules/extractor.h", "#pragma once\n")
        write(root, "src/modules/version.rc", "VS_VERSION_INFO VERSIONINFO\n")
        for name in (
            "build/ObserverProjectConfigurations.props",
            "build/ObserverConfiguration.props",
            "build/ObserverProject.props",
            "build/ObserverModuleVersion.props",
        ):
            write(root, name, "<Project />\n")
        write(root, "vcpkg.json", '{"dependencies":["zlib"]}\n')
        write(root, "build/vcpkg/triplets/observer-x64-windows-static.cmake", "static\n")
        write(root, "LICENSE.txt", "project license\n")
        return root

    @staticmethod
    def invoke(output: Path, *arguments: str) -> int:
        output.mkdir()
        with mock.patch.dict(os.environ, {"OBSERVER_OUT_DIR": str(output)}, clear=False):
            return package_main(arguments)

    @staticmethod
    def records(**overrides: str) -> tuple[str, ...]:
        identities = {module: hashlib.sha256(module.encode()).hexdigest() for module in MODULES}
        return tuple(
            argument
            for module in MODULES
            for argument in (
                "--module",
                overrides.get(module, f"{module}={VERSIONS[module]}={identities[module]}"),
            )
        )

    def test_module_stage_archive_and_aggregate_are_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            binary = root / "renpy.so"
            binary.write_bytes(b"module")
            stage = root / "stage"
            self.assertEqual(
                0,
                self.invoke(stage, "stage-module", "x64", "renpy", "3.1.0",
                            str(binary), str(repository)),
            )

            payload = stage / "payload"
            expected = set(PAYLOAD)
            self.assertEqual(
                expected,
                {
                    path.relative_to(payload).as_posix()
                    for path in payload.rglob("*")
                    if path.is_file()
                },
            )
            document = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                ("x64", "module", "renpy", "3.1.0"),
                (document["architecture"], document["kind"], document["module"],
                 document["version"]),
            )
            self.assertEqual(sorted(expected), [item["name"] for item in document["entries"]])

            archive_one, archive_two = root / "archive-one", root / "archive-two"
            self.invoke(archive_one, "archive-module", "x64", "renpy", "3.1.0", str(stage))
            self.invoke(archive_two, "archive-module", "x64", "renpy", "3.1.0", str(stage))
            first = archive_one / "renpy-3.1.0-x64.zip"
            second = archive_two / "renpy-3.1.0-x64.zip"
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(sorted(expected), archive.namelist())
                self.assertTrue(all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist()))
                self.assertEqual(b"module", archive.read("renpy.so"))
                # The raw bilingual histories travel at the archive root, byte for byte.
                for name in CHANGELOGS:
                    self.assertEqual(
                        archive.read(name),
                        (repository / "src/modules/renpy" / name).read_bytes(),
                    )
                # LICENSES.txt is the project license plus only this module's dependency notices.
                self.assertEqual(archive.read("LICENSES.txt"), licenses_document(repository, "renpy"))
                self.assertIn(b"project license\n", archive.read("LICENSES.txt"))
                self.assertIn(b"license:zlib.txt\n", archive.read("LICENSES.txt"))
                self.assertNotIn(b"license:rgssad.txt", archive.read("LICENSES.txt"))

            validation = root / "validation"
            self.assertEqual(
                0,
                self.invoke(validation, "validate-module", "x64", "renpy", "3.1.0",
                            str(first), str(stage)),
            )
            proof = json.loads((validation / "validation.json").read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), proof["sha256"])
            self.assertEqual(sorted(expected), [item["name"] for item in proof["entries"]])
            with self.assertRaisesRegex(PackageError, "manifest does not match"):
                self.invoke(root / "wrong-version-validation", "validate-module", "x64",
                            "renpy", "3.1.1", str(first), str(stage))

            tampered = root / "tampered.zip"
            with zipfile.ZipFile(first) as source, zipfile.ZipFile(tampered, "w") as target:
                for name in source.namelist():
                    target.writestr(name, b"changed" if name == "renpy.so" else source.read(name))
            with self.assertRaisesRegex(PackageError, "exact stage manifests"):
                self.invoke(root / "tampered-validation", "validate-module", "x64", "renpy",
                            "3.1.0", str(tampered), str(stage))
            duplicate = root / "duplicate.zip"
            with self.assertWarns(UserWarning), zipfile.ZipFile(duplicate, "w") as archive:
                archive.writestr("renpy.so", b"first")
                archive.writestr("renpy.so", b"second")
            for index, invalid in enumerate((duplicate, root / "invalid.zip")):
                if not invalid.exists():
                    invalid.write_bytes(b"not a zip")
                with self.subTest(invalid=invalid), self.assertRaisesRegex(PackageError, "exact stage manifests"):
                    self.invoke(root / f"invalid-validation-{index}", "validate-module",
                                "x64", "renpy", "3.1.0", str(invalid), str(stage))

            aggregate = root / "aggregate"
            self.invoke(aggregate, "aggregate", *self.records(), str(first))
            self.assertEqual((aggregate / first.name).read_bytes(), first.read_bytes())
            packages = json.loads((aggregate / "packages.json").read_text(encoding="utf-8"))
            self.assertEqual("renpy-3.1.0-x64.zip", packages["archives"][0]["name"])
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).hexdigest(),
                packages["archives"][0]["sha256"],
            )
            self.assertEqual(
                packages["modules"],
                [
                    {
                        "content": hashlib.sha256(module.encode()).hexdigest(),
                        "module": module,
                        "version": VERSIONS[module],
                    }
                    for module in MODULES
                ],
            )

    def test_licenses_document_is_deterministic_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")

            document = licenses_document(repository, "renpy")

            sections = b"\n".join([
                section("Observer Modules", (repository / "LICENSE.txt").read_bytes()),
                *(section(name, (repository / "licenses" / name).read_bytes())
                  for name in LICENSES["renpy"]),
            ])
            # The original notice sections survive byte for byte after the preamble.
            self.assertTrue(document.endswith(sections))
            preamble = document[: len(document) - len(sections)]
            self.assertTrue(preamble.strip())
            self.assertNotIn(SEPARATOR.encode(), preamble)
            # Stable and deterministic across repeated reads of the same source bytes.
            self.assertEqual(document, licenses_document(repository, "renpy"))
            self.assertEqual(document.count(SEPARATOR.encode()), 2 * (1 + len(LICENSES["renpy"])))

    def test_licenses_document_preamble_identifies_the_module_and_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")

            for module in MODULES:
                with self.subTest(module=module):
                    document = licenses_document(repository, module)
                    delimiter = SEPARATOR.encode("utf-8")
                    self.assertTrue(document.startswith(b"This file"), document[:80])
                    first = document.index(delimiter)
                    preamble = document[:first]

                    # The intro names the shipped artifact and the project it belongs to.
                    self.assertIn(f"{module}.so".encode("utf-8"), preamble)
                    self.assertIn(b"Observer Modules", preamble)
                    self.assertIn(b"https://github.com/refaim/ObserverModules", preamble)
                    # It explains that the following sections reproduce the notices in full.
                    self.assertIn(b"project license", preamble)
                    self.assertIn(b"third-party", preamble)
                    self.assertIn(b"format references", preamble)
                    self.assertIn(b"reproduced in full", preamble)
                    # It stays prose: no delimiters and a blank line before the first section.
                    self.assertNotIn(delimiter, preamble)
                    self.assertTrue(preamble.endswith(b"\n\n"))
                    # It does not misstate how the referenced components are incorporated.
                    self.assertNotIn(b"statically linked", preamble)
                    # One module's preamble never claims another module's artifact.
                    for other in (name for name in MODULES if name != module):
                        self.assertNotIn(f"{other}.so".encode("utf-8"), preamble)
                    # Each document keeps exactly this module's selective notices.
                    for name in LICENSES[module]:
                        self.assertIn(f"{SEPARATOR}\n{name}\n{SEPARATOR}".encode("utf-8"), document)
                    exclusive = {
                        name
                        for other in MODULES
                        if other != module
                        for name in LICENSES[other]
                    } - set(LICENSES[module])
                    for name in exclusive:
                        self.assertNotIn(f"{SEPARATOR}\n{name}\n{SEPARATOR}".encode("utf-8"), document)

    def test_licenses_document_fails_closed_on_missing_or_empty_notices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")

            (repository / "licenses/zlib.txt").unlink()
            with self.assertRaisesRegex(PackageError, "missing or unreadable"):
                licenses_document(repository, "renpy")

            (repository / "licenses/zlib.txt").write_text("zlib license\n", encoding="utf-8")
            (repository / "LICENSE.txt").write_bytes(b"   \n")
            with self.assertRaisesRegex(PackageError, "empty"):
                licenses_document(repository, "renpy")

    def test_licenses_document_normalizes_a_missing_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            (repository / "licenses/rpatool.txt").write_bytes(b"no trailing newline")

            document = licenses_document(repository, "renpy")

            self.assertIn(b"no trailing newline\n\n" + SEPARATOR.encode(), document)

    def test_stage_module_requires_both_language_change_logs_but_symbols_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            binary = root / "renpy.so"
            binary.write_bytes(b"module")

            for index, name in enumerate(CHANGELOGS):
                changelog = repository / "src/modules/renpy" / name
                original = changelog.read_bytes()
                changelog.unlink()
                with self.subTest(missing=name), self.assertRaisesRegex(PackageError, "ChangeLog"):
                    self.invoke(root / f"missing-{index}", "stage-module", "x64", "renpy", "3.1.0",
                                str(binary), str(repository))
                changelog.write_bytes(entry("3.0.0", "stale").encode("utf-8"))
                with self.subTest(stale=name), self.assertRaisesRegex(PackageError, "ChangeLog"):
                    self.invoke(root / f"stale-{index}", "stage-module", "x64", "renpy", "3.1.0",
                                str(binary), str(repository))
                changelog.write_bytes(original)

            symbol = root / "renpy.pdb"
            symbol.write_bytes(b"symbols")
            self.assertEqual(
                0,
                self.invoke(root / "symbol-stage", "stage-symbol", "x64", "renpy", str(symbol)),
            )

    def test_package_manifest_rejects_incomplete_or_malformed_module_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "renpy-3.1.0-x64.zip"
            archive.write_bytes(b"archive")
            identity = hashlib.sha256(b"renpy").hexdigest()
            for index, (records, message) in enumerate((
                (("--module", f"renpy=3.1.0={identity}"), "expected module set"),
                (self.records(renpy=f"pickle=3.1.0={identity}"), "invalid module record"),
                (self.records(renpy="renpy=3.1.0=not-a-digest"), "invalid module record"),
                (self.records(renpy=f"renpy=3.1={identity}"), "invalid module version"),
                (
                    (*self.records(), "--module", f"renpy=3.1.0={identity}"),
                    "duplicate module record",
                ),
            )):
                with self.subTest(records=records), self.assertRaisesRegex(PackageError, message):
                    self.invoke(root / f"bad-manifest-{index}", "aggregate", *records, str(archive))

    def test_smoke_extracts_and_runs_the_exact_archived_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "renpy-3.1.0-x64.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("renpy.so", b"exact packaged module")
            tests = root / "tests.exe"
            tests.write_bytes(b"test runner")
            output = root / "smoke"

            with mock.patch("core.package.subprocess.run") as run:
                self.invoke(output, "smoke", "x64", "renpy", str(archive), str(tests))

            module = output / "renpy.so"
            self.assertEqual(module.read_bytes(), b"exact packaged module")
            run.assert_called_once()
            arguments = run.call_args.args[0]
            self.assertEqual(arguments[:2], [str(tests), "[package-smoke]"])
            self.assertEqual(run.call_args.kwargs["cwd"], output)
            self.assertEqual(run.call_args.kwargs["env"]["OBSERVER_PACKAGE_MODULE"], str(module))
            self.assertEqual(run.call_args.kwargs["env"]["OBSERVER_PACKAGE_FORMAT"], "renpy")

            missing = root / "missing.zip"
            with zipfile.ZipFile(missing, "w") as package:
                package.writestr("other.so", b"wrong module")
            with self.assertRaisesRegex(PackageError, "archive has no renpy.so"):
                self.invoke(root / "missing-smoke", "smoke", "x64", "renpy", str(missing), str(tests))

    def test_manifest_mismatch_duplicate_archive_and_missing_output_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.repository(root / "repo")
            binary = root / "renpy.so"
            binary.write_bytes(b"module")
            stage = root / "stage"
            self.invoke(stage, "stage-module", "x86", "renpy", "3.1.0", str(binary), str(repository))
            (stage / "payload/extra.obj").write_bytes(b"unexpected")
            with self.assertRaisesRegex(PackageError, "manifest does not match"):
                self.invoke(root / "bad-archive", "archive-module", "x86", "renpy", "3.1.0", str(stage))
            with self.assertRaisesRegex(PackageError, "invalid module version"):
                self.invoke(root / "bad-version", "stage-module", "x86", "renpy", "3.1",
                            str(binary), str(repository))

            archive = root / "same.zip"
            archive.write_bytes(b"same")
            with self.assertRaisesRegex(PackageError, "duplicate archive name"):
                self.invoke(root / "bad-aggregate", "aggregate", *self.records(),
                            str(archive), str(archive))

        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(PackageError, "OBSERVER_OUT_DIR"):
            package_main(("aggregate", *self.records(), "missing.zip"))

    def test_the_retired_repository_symbol_commands_are_gone(self) -> None:
        for command in ("archive-symbols", "validate-symbols"):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                package_main((command,))


if __name__ == "__main__":
    unittest.main(verbosity=2)

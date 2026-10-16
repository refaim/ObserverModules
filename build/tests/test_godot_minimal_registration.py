"""Tests-first contract for Godot registration in the minimal build driver.

This module is written before Godot is wired into ``build/main.py`` and the MSBuild
projects, so every test below is expected to fail (RED) against the current tree.
The behaviour it pins is the *observable* integration contract:

* Godot is the fourth module of the default selection that drives the native audit
  and packaging routes (``main.MODULES`` / ``main.MODULE_BINARIES`` / ``main.LICENSES``);
* the default packaging route ships ``godot-1.1.1-bin-x64.zip``,
  ``godot-1.1.1-bin-x86.zip`` and ``godot-1.1.1-pdb.zip`` with the shipped
  payload, registration and notices;
* ``build/projects/tests.vcxproj`` references ``godot.vcxproj`` with the fixed
  project GUID and ``LinkLibraryDependencies=false``;
* ``build/projects/godot.vcxproj`` builds a ``godot.so`` dynamic library with the
  shared lifecycle translation units and its own adapters;
* ``vcpkg.json`` pins the Godot dependencies and the zlib-ng overlay on the frozen
  baseline, and every zlib consumer links the shared static zlib artifacts;
* the README publishes the Godot latest English/Russian notes.

The tests assert produced bytes, parsed XML/JSON fields and recorded invocations,
never re-imported source text, so they cannot pass by mirroring an internal
constant.  ``GuardedCase`` forbids any real native subprocess.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest
import subprocess
from unittest import mock
import xml.etree.ElementTree as ET
import zipfile

from test_minimal_build import GuardedCase

import main  # noqa: E402  (the build root is added to sys.path by test_minimal_build)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROJECTS = REPOSITORY_ROOT / "build" / "projects"
NS = {"msb": "http://schemas.microsoft.com/developer/msbuild/2003"}

GODOT = "godot"
GODOT_VERSION = "1.1.0"
GODOT_GUID = "{9C2F5E4A-7B31-4D58-8E27-1A6C4F0B9D34}"
BASELINE = "20ceb8adbf0504d4b0d5e4058a32f9d4fc896391"

# Independent fixture bytes: the shipped payloads are compared against these
# literals, never against a value rederived from the same source they came from.
GODOT_SO_X64 = b"fake godot module x64 payload\n"
GODOT_SO_X86 = b"fake godot module x86 payload\n"
GODOT_PDB_X64 = b"fake godot x64 program database\n"
GODOT_PDB_X86 = b"fake godot x86 program database\n"
GODOT_OBSERVER_INI = b"[Modules]\nGodot=modules\\godot.so\n"
GODOT_CHANGELOG_EN = f"Version {GODOT_VERSION}\n-------------\n + Godot module added.\n".encode()
GODOT_CHANGELOG_RU = f"Version {GODOT_VERSION}\n-------------\n + Godot module added.\n".encode()

# Required notices for the Godot module.  The set below is a subset of the
# registered notices; the vendored libraries (bcdec, dr_libs, qoa) are checked
# by name fragment so a future license audit can settle the exact file names.
REQUIRED_GODOT_NOTICES = (
    "YY-Thunks.txt",
    "Observer.txt",
    "GPL-3.0.txt",
    "godot.txt",
    "libspng.txt",
    "libogg.txt",
    "brotli.txt",
    "zstd.txt",
    "fastlz.txt",
    "zlib-ng.txt",
)
VENDORED_NOTICE_FRAGMENTS = {
    "bcdec": ("bcdec",),
    "dr_libs": ("dr_lib", "dr-lib", "drlibs"),
    "qoa": ("qoa",),
}
# Every license file the fixture may legitimately reference once Godot lands.
FIXTURE_LICENSE_NAMES = (
    "VC-LTL5.txt", "YY-Thunks.txt",
    "Observer.txt", "GPL-3.0.txt", "rpatool.txt", "serde-pickle.txt",
    "rgssad.txt", "godot.txt", "libspng.txt", "libogg.txt", "brotli.txt", "zstd.txt",
    "fastlz.txt", "zlib-ng.txt", "bcdec.txt", "dr_libs.txt", "qoa.txt", "zydis.txt", "zycore.txt", "mbedcrypto.txt",
)


def _parse(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def _project_references(root: ET.Element) -> list[ET.Element]:
    return root.findall("msb:ItemGroup/msb:ProjectReference", NS)


def _link_dependencies(path: Path) -> list[str]:
    """Return every ``Link/AdditionalDependencies`` value in a project file."""

    root = _parse(path)
    found: list[str] = []
    for element in root.findall("msb:ItemDefinitionGroup/msb:Link/msb:AdditionalDependencies", NS):
        if element.text:
            found.append(element.text)
    return found


def _imported_projects(path: Path) -> list[str]:
    """Return the import targets declared by an MSBuild project, by file name."""

    return [
        Path(element.get("Project", "")).name
        for element in _parse(path).findall("msb:Import", NS)
    ]


def _shared_zlib_policy(path: Path) -> dict[str, list[str]]:
    """Read the shared third-party link props as ``{configuration: [libraries]}``.

    Only Debug/Release-conditioned ``Link/AdditionalDependencies`` entries are
    collected; this is not a general MSBuild evaluator.
    """

    policy: dict[str, list[str]] = {"Debug": [], "Release": []}
    for element in _parse(path).findall(".//msb:Link/msb:AdditionalDependencies", NS):
        condition = (element.get("Condition", "") or "").replace(" ", "")
        for configuration, selectors in {
            "Debug": ("'$(Configuration)'=='Debug'", "'$(UseDebugLibraries)'=='true'"),
            "Release": (
                "'$(Configuration)'=='Release'",
                "'$(UseDebugLibraries)'!='true'",
                "'$(UseDebugLibraries)'=='false'",
            ),
        }.items():
            if any(selector in condition for selector in selectors):
                policy[configuration].extend(
                    token.strip() for token in (element.text or "").split(";") if token.strip()
                )
    return policy


def _compile_units(path: Path) -> list[str]:
    return [
        element.get("Include", "")
        for element in _parse(path).findall("msb:ItemGroup/msb:ClCompile", NS)
    ]


def _property(root: ET.Element, name: str) -> str | None:
    element = root.find(f".//msb:{name}", NS)
    return None if element is None else (element.text or "").strip()


class GodotRegistration(GuardedCase):
    """The default Python selection must carry Godot as its fourth module."""

    def _repository(self) -> Path:
        modules = {
            "renpy": "3.1.0",
            "rpgmaker": "1.1.0",
            "zanzarah": "2.1.0",
            GODOT: GODOT_VERSION,
        }
        (self.repo / "LICENSE.txt").write_bytes(b"Fixture MIT license text.\n")
        licenses = self.repo / "licenses"
        licenses.mkdir()
        for name in FIXTURE_LICENSE_NAMES:
            (licenses / name).write_bytes(f"Fixture notice: {name}\n".encode("utf-8"))
        for module, version in modules.items():
            directory = self.repo / "src" / "modules" / module
            directory.mkdir(parents=True)
            (directory / "VERSION").write_text(version + "\n", encoding="utf-8")
            if module == GODOT:
                (directory / "observer_user.ini").write_bytes(GODOT_OBSERVER_INI)
                (directory / "ChangeLog.en.txt").write_bytes(GODOT_CHANGELOG_EN)
                (directory / "ChangeLog.ru.txt").write_bytes(GODOT_CHANGELOG_RU)
            else:
                (directory / "observer_user.ini").write_bytes(
                    f"[Modules]\n{module}=modules\\{module}.so\n".encode("utf-8")
                )
                (directory / "ChangeLog.en.txt").write_bytes(b"Version note EN.\n")
                (directory / "ChangeLog.ru.txt").write_bytes(b"Version note RU.\n")
        for architecture, binary, symbol in (
            ("x64", GODOT_SO_X64, GODOT_PDB_X64),
            ("x86", GODOT_SO_X86, GODOT_PDB_X86),
        ):
            stage = self.repo / "out" / "native" / "bin" / architecture / "Release"
            stage.mkdir(parents=True)
            (stage / "godot.so").write_bytes(binary)
            (stage / "godot.pdb").write_bytes(symbol)
            for module in modules:
                if module == GODOT:
                    continue
                (stage / f"{module}.so").write_bytes(f"fake {module} .so {architecture}\n".encode())
                (stage / f"{module}.pdb").write_bytes(f"fake {module} .pdb {architecture}\n".encode())
        return self.repo

    def test_godot_is_the_fourth_default_module(self) -> None:
        self.assertIn(GODOT, main.MODULES, "the default module selection must include godot")
        self.assertEqual(main.MODULES[-1], GODOT, "godot must be the last default module")
        self.assertEqual(
            set(main.MODULES),
            {"renpy", "rpgmaker", "zanzarah", GODOT},
            "godot must join the three existing modules exactly once",
        )
        self.assertEqual(main.MODULE_BINARIES.get(GODOT), "godot.so")

    def test_godot_notice_set_is_registered(self) -> None:
        self.assertIn(GODOT, main.LICENSES, "godot must register its license notices")
        notices = tuple(main.LICENSES[GODOT])
        self.assertEqual(notices[:2], ("Observer.txt", "GPL-3.0.txt"))
        missing = [name for name in REQUIRED_GODOT_NOTICES if name not in notices]
        self.assertEqual(missing, [], f"missing required godot notices: {missing}")
        lowered = [name.lower() for name in notices]
        for library, fragments in VENDORED_NOTICE_FRAGMENTS.items():
            self.assertTrue(
                any(any(fragment in name for fragment in fragments) for name in lowered),
                f"godot notices must cover the vendored library {library}",
            )

    def test_default_packaging_route_ships_godot_archives(self) -> None:
        repository = self._repository()
        self.assertIn(GODOT, main.MODULES, "the default packaging route must include godot")
        created = main.package(repository=repository, output=repository / "out" / "packages")
        names = {Path(path).name for path in created}
        expected = {
            f"godot-{GODOT_VERSION}-bin-x64.zip",
            f"godot-{GODOT_VERSION}-bin-x86.zip",
            f"godot-{GODOT_VERSION}-pdb.zip",
        }
        self.assertTrue(
            expected <= names,
            f"default packaging must ship the godot archives; created: {sorted(names)}",
        )
        self.assertEqual(
            sorted(name for name in names if name.startswith("godot-")),
            [f"godot-{GODOT_VERSION}-bin-x64.zip", f"godot-{GODOT_VERSION}-bin-x86.zip", f"godot-{GODOT_VERSION}-pdb.zip"],
            "the pdb archive must sort last lexicographically",
        )

    def test_godot_archives_carry_shipped_payload_and_registration(self) -> None:
        repository = self._repository()
        self.assertIn(GODOT, main.LICENSES, "godot must be packageable before its archives can ship")
        main.package(repository=repository, output=repository / "out" / "packages", modules=(GODOT,))
        packages = repository / "out" / "packages"

        for architecture, binary in (("x64", GODOT_SO_X64), ("x86", GODOT_SO_X86)):
            archive = packages / f"godot-{GODOT_VERSION}-bin-{architecture}.zip"
            with zipfile.ZipFile(archive) as opened:
                members = {name: opened.read(name) for name in opened.namelist()}
            expected = {
                "godot.so": binary,
                "observer_user.ini": GODOT_OBSERVER_INI,
                "ChangeLog.en.txt": GODOT_CHANGELOG_EN,
                "ChangeLog.ru.txt": GODOT_CHANGELOG_RU,
            }
            self.assertEqual(sorted(members), sorted([*expected, "LICENSES.txt"]))
            for name, content in expected.items():
                self.assertEqual(members[name], content, f"{architecture}:{name}")
            notices = members["LICENSES.txt"]
            self.assertTrue(notices.strip(), "LICENSES.txt must not be empty")
            self.assertIn(b"Fixture notice: godot.txt", notices)

        with zipfile.ZipFile(packages / f"godot-{GODOT_VERSION}-pdb.zip") as opened:
            symbols = {name: opened.read(name) for name in opened.namelist()}
        self.assertEqual(symbols, {"x64/godot.pdb": GODOT_PDB_X64, "x86/godot.pdb": GODOT_PDB_X86})


class GodotProjectContract(GuardedCase):
    def test_link_dependency_inspection_ignores_empty_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dependencies.vcxproj"
            path.write_text(
                '<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">'
                '<ItemDefinitionGroup><Link><AdditionalDependencies />'
                '<AdditionalDependencies>first.lib;%(AdditionalDependencies)</AdditionalDependencies>'
                '</Link></ItemDefinitionGroup></Project>', encoding="utf-8",
            )
            self.assertEqual(_link_dependencies(path), ["first.lib;%(AdditionalDependencies)"])

    """The MSBuild half: the test project references a Godot module project."""

    def test_tests_project_references_the_godot_project(self) -> None:
        root = _parse(PROJECTS / "tests.vcxproj")
        references = _project_references(root)
        includes = sorted(Path(ref.get("Include", "")).name for ref in references)
        self.assertEqual(
            includes,
            ["godot.vcxproj", "renpy.vcxproj", "rpgmaker.vcxproj", "zanzarah.vcxproj"],
            "the suite must build the four module projects",
        )
        godot = next(
            ref for ref in references if Path(ref.get("Include", "")).name == "godot.vcxproj"
        )
        self.assertEqual(_property(godot, "Project"), GODOT_GUID)
        self.assertEqual((_property(godot, "LinkLibraryDependencies") or "").lower(), "false")
        self.assertEqual((_property(godot, "ReferenceOutputAssembly") or "").lower(), "false")

    def test_godot_project_definition_matches_module_contract(self) -> None:
        project = PROJECTS / "godot.vcxproj"
        self.assertTrue(project.is_file(), f"missing Godot module project: {project}")
        root = _parse(project)
        self.assertEqual(_property(root, "unpackratConfigurationType"), "DynamicLibrary")
        self.assertEqual(_property(root, "unpackratModuleName"), GODOT)
        self.assertEqual(_property(root, "TargetName"), GODOT)
        self.assertEqual(_property(root, "TargetExt"), ".so")
        self.assertIn(
            r"src\modules\godot\godot.def",
            (_property(root, "ModuleDefinitionFile") or "").replace("/", "\\"),
            "the Godot module must export through its own .def",
        )
        units = [unit.replace("\\", "/") for unit in _compile_units(project)]
        self.assertTrue(any(unit.endswith("src/dll.cpp") for unit in units))
        self.assertTrue(any(unit.endswith("src/archive.cpp") for unit in units))
        self.assertTrue(
            any(unit.endswith(".cpp") and "src/modules/godot/" in unit for unit in units),
            "the Godot project must compile its own adapter translation units",
        )

    def test_zlib_consumers_link_shared_static_artifacts(self) -> None:
        shared = PROJECTS.parent / "unpackrat-third-party-link.props"
        self.assertTrue(shared.is_file(), f"missing shared third-party link props: {shared}")
        policy = _shared_zlib_policy(shared)
        self.assertIn(
            "zlibstaticd.lib", policy["Debug"],
            "the shared link policy must name zlibstaticd.lib for Debug",
        )
        self.assertIn(
            "zlibstatic.lib", policy["Release"],
            "the shared link policy must name zlibstatic.lib for Release",
        )
        for name in (
            "tests.vcxproj", "renpy.vcxproj", "godot.vcxproj",
            "leak-probe.vcxproj", "fuzz-renpy.vcxproj",
        ):
            project = PROJECTS / name
            self.assertTrue(project.is_file(), f"missing module project: {project}")
            imported = _imported_projects(project)
            self.assertIn(
                "unpackrat-third-party-link.props", imported,
                f"{name} must import the shared third-party link props",
            )
            direct = _link_dependencies(project)
            for stale in ("zs.lib", "zsd.lib"):
                self.assertFalse(
                    any(stale in value for value in direct),
                    f"{name} must not list the stale baseline zlib {stale} directly",
                )
            self.assertFalse(
                any(value.lower().endswith(".dll") and "zlib" in value.lower() for value in direct),
                f"{name} must keep zlib statically linked, never as a shipped DLL",
            )


class GodotAuditContract(GuardedCase):
    """The native release audit must gate every shipped module, godot included."""

    def test_release_audit_inspects_the_godot_module(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run(argv, cwd=None, env=None, capture=False):
            recorded = tuple(str(item) for item in argv)
            calls.append(recorded)
            if recorded[1] == "/headers":
                machine = "14C machine (x86)" if "x86" in recorded[2] else "8664 machine (x64)"
                return subprocess.CompletedProcess(list(recorded), 0, machine)
            if recorded[1] == "/dependents":
                return subprocess.CompletedProcess(list(recorded), 0, "    KERNEL32.dll")
            exports = "   1    0 00001000 LoadSubModule\n   2    0 00001010 UnloadSubModule"
            return subprocess.CompletedProcess(list(recorded), 0, exports)

        with mock.patch.object(main, "run", fake_run), mock.patch.object(
            main, "_find_dumpbin", return_value=self.repo / "dumpbin.exe"
        ):
            for architecture in ("x64", "x86"):
                main._audit(self.repo, architecture)

        inspected = {
            (architecture, Path(recorded[2]).name)
            for recorded in calls
            if len(recorded) > 2 and recorded[1] == "/headers"
            for architecture in ("x64", "x86")
            if architecture in Path(recorded[2]).parts
        }
        self.assertTrue(
            {("x64", "godot.so"), ("x86", "godot.so")} <= inspected,
            f"the release audit must gate both Godot binaries; inspected: {sorted(inspected)}",
        )

    def test_coverage_ignores_vendor_code_but_not_first_party_adapters(self) -> None:
        ignored = (
            r"C:\work\third_party\bcdec\bcdec.cpp",
            "/work/third_party/dr_libs/dr_wav.h",
            r"C:\work\src\tests\unit\godot.cpp",
            "/work/src/fuzz/godot.cpp",
            "/work/vcpkg_installed/x64-windows/include/zlib.h",
            r"C:\Program Files\Microsoft Visual Studio\VC\include\vector",
            r"C:\Program Files (x86)\Windows Kits\10\Include\windows.h",
        )
        first_party = (
            "/work/src/core/godot/pck.cpp",
            r"C:\work\src\modules\godot\godot.cpp",
            "/work/src/archive.cpp",
            "/work/src/core/compression/zlib_codec.cpp",
            "/work/src/modules/godot/resource_converter.cpp",
        )
        for path in ignored:
            with self.subTest(path=path):
                self.assertRegex(path, main.COVERAGE_IGNORED)
        for path in first_party:
            with self.subTest(path=path):
                self.assertIsNone(re.search(main.COVERAGE_IGNORED, path))


class GodotSourceDiscoveryContract(GuardedCase):
    """Formatter discovery must skip the pinned top-level vendor tree, not first-party code."""

    def test_formatter_discovery_omits_vendor_headers_but_keeps_adapters(self) -> None:
        vendor = self.repo / "third_party" / "bcdec"
        vendor.mkdir(parents=True)
        (vendor / "bcdec.h").write_bytes(b"pinned upstream header\n")
        (vendor / "bcdec.cpp").write_bytes(b"pinned upstream source\n")

        adapter = self.repo / "src" / "core" / "codecs"
        adapter.mkdir(parents=True)
        (adapter / "png_codec.cpp").write_bytes(b"first-party codec adapter\n")
        (adapter / "png_codec.h").write_bytes(b"first-party codec adapter header\n")

        for ignored in (".git", ".venv", "out"):
            tree = self.repo / ignored / "nested"
            tree.mkdir(parents=True)
            (tree / "hidden.h").write_bytes(b"excluded\n")

        discovered = {
            path.relative_to(self.repo).as_posix()
            for path in main._source_files(self.repo, main._CPP_SUFFIXES)
        }

        self.assertNotIn("third_party/bcdec/bcdec.h", discovered)
        self.assertNotIn("third_party/bcdec/bcdec.cpp", discovered)
        self.assertIn("src/core/codecs/png_codec.cpp", discovered)
        self.assertIn("src/core/codecs/png_codec.h", discovered)
        for excluded in (".git", ".venv", "out"):
            self.assertNotIn(f"{excluded}/nested/hidden.h", discovered)


class GodotManifestContract(GuardedCase):
    """The pinned vcpkg manifest must carry the Godot dependencies and zlib-ng."""

    @staticmethod
    def _manifest() -> dict:
        return json.loads((REPOSITORY_ROOT / "vcpkg.json").read_text(encoding="utf-8"))

    def test_manifest_pins_godot_dependencies_and_zlib_ng(self) -> None:
        manifest = self._manifest()
        self.assertEqual(manifest.get("builtin-baseline"), BASELINE)
        names = {
            entry["name"] if isinstance(entry, dict) else entry
            for entry in manifest.get("dependencies", [])
        }
        required = {"libspng", "libogg", "brotli", "zstd", "fastlz"}
        self.assertTrue(
            required <= names,
            f"the manifest must declare the Godot dependencies; missing: {sorted(required - names)}",
        )
        self.assertEqual(
            self._zlib_ng_overlay_version(), "2.3.3",
            "the in-repo zlib-ng overlay must be pinned at exactly 2.3.3",
        )

    @staticmethod
    def _overlay_ports() -> list[str]:
        configuration = json.loads(
            (REPOSITORY_ROOT / "vcpkg-configuration.json").read_text(encoding="utf-8")
        )
        return [
            str(entry.get("path", "") if isinstance(entry, dict) else entry)
            .replace("\\", "/")
            .removeprefix("./")
            .rstrip("/")
            for entry in configuration.get("overlay-ports", [])
        ]

    def _zlib_ng_overlay_version(self) -> str:
        self.assertIn(
            "build/vcpkg/ports", self._overlay_ports(),
            "vcpkg-configuration.json must activate the in-repo overlay ports directory",
        )
        port = REPOSITORY_ROOT / "build" / "vcpkg" / "ports" / "zlib"
        descriptor = port / "vcpkg.json"
        self.assertTrue(descriptor.is_file(), f"missing zlib overlay descriptor: {descriptor}")
        metadata = json.loads(descriptor.read_text(encoding="utf-8"))
        self.assertEqual(metadata.get("name"), "zlib", "the overlay must replace the zlib port")
        version = str(metadata.get("version", ""))
        portfile = port / "portfile.cmake"
        self.assertTrue(portfile.is_file(), f"missing zlib overlay portfile: {portfile}")
        active = "\n".join(
            line.split("#", 1)[0] for line in portfile.read_text(encoding="utf-8").splitlines()
        )
        fetch = re.search(r"\bvcpkg_from_github\s*\(([^)]*)\)", active, re.S)
        self.assertIsNotNone(fetch, "the overlay must fetch its active upstream source")
        self.assertRegex(fetch.group(1), r"\bREPO\s+zlib-ng/zlib-ng(?:\s|$)")
        self.assertRegex(
            fetch.group(1),
            r'\bREF\s+(?:"\$\{VERSION\}"|\$\{VERSION\}|"2\.3\.3"|2\.3\.3)(?:\s|$)',
        )
        self.assertRegex(active, r"(?m)^\s*-DZLIB_COMPAT=ON(?:\s|$)")
        return version


class GodotDocsContract(GuardedCase):
    """The docs must publish the Godot latest English and Russian notes."""

    def test_readme_publishes_godot_latest_notes(self) -> None:
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Godot", readme)
        self.assertIn("src/modules/godot/ChangeLog.en.txt", readme)
        self.assertIn("src/modules/godot/ChangeLog.ru.txt", readme)
        version = (REPOSITORY_ROOT / "src/modules/godot/VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version, GODOT_VERSION)
        self.assertNotIn("licenses/VC-LTL5.txt", readme)
        self.assertIn("| Module | Formats | Release | ChangeLog |", readme)
        self.assertIn("| Godot | `.pck`, embedded PCK and same-stem EXE sidecars |", readme)
        self.assertIn("https://github.com/refaim/unpackrat/releases?q=godot", readme)
        self.assertNotIn("<<<<<<<", readme)
        self.assertNotIn("planned first release", readme)
        build_system = (REPOSITORY_ROOT / "docs" / "build-system.md").read_text(encoding="utf-8")
        self.assertIn(GODOT, build_system.lower())

    def test_readme_defers_the_component_inventory_to_the_retained_notices(self) -> None:
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        # The hand-maintained component and inspiration tables are gone; the notice
        # directory owns the machine-readable inventory.
        self.assertNotIn("| Project | License | Modules |", readme)
        self.assertNotIn("### Third-party components", readme)
        self.assertNotIn("### Sources of inspiration", readme)
        self.assertIn("`.rgssad` (XP, RGSS1), `.rgss2a` (VX, RGSS2), `.rgss3a`", readme)

    def test_godot_xp_runtime_recipe_has_a_minor_version(self) -> None:
        self.assertEqual(main.read_version(REPOSITORY_ROOT, GODOT), GODOT_VERSION)

    def test_godot_runtime_recipe_preserves_all_previous_changelog_sections(self) -> None:
        for language in ("en", "ru"):
            with self.subTest(language=language):
                notes = (REPOSITORY_ROOT / f"src/modules/godot/ChangeLog.{language}.txt").read_text(encoding="utf-8")
                versions = re.findall(r"^(?:Version|Версия)\s+(\S+)\s*$", notes, re.MULTILINE)
                self.assertEqual(versions, ["1.1.0", "1.0.1", "1.0.0"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

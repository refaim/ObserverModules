"""Contract for the first-party "unpackrat" branding rename.

These tests pin the observable requirements of the rename migration: the recorded
entry points, MSBuild infrastructure, vcpkg triplets and own C++ core keep the same
behavior, but every first-party name that carries the old product brand resolves to
its renamed spelling. Actor/host identities (the Observer ABI, the FAR/Observer install
layout, ``observer_user.ini``) are deliberately out of scope and stay untouched.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BUILD_ROOT = REPOSITORY_ROOT / "build"
sys.path.insert(0, str(BUILD_ROOT))

import main as build_main  # noqa: E402
import native  # noqa: E402


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


class EntryPointContractTests(unittest.TestCase):
    """The root launcher and the Python drivers agree on the renamed switch names."""

    def test_root_launcher_uses_the_renamed_argv_switch(self) -> None:
        launcher = _read("build.ps1")
        self.assertIn("$env:UNPACKRAT_BUILD_PRINT_ARGV", launcher)
        self.assertNotIn("OBSERVER_BUILD_PRINT_ARGV", launcher)

    def test_command_line_contract_uses_the_renamed_switch(self) -> None:
        contract = _read("build/tests/build-entry-point-contract.Tests.ps1")
        self.assertIn("$env:UNPACKRAT_BUILD_PRINT_ARGV", contract)
        self.assertNotIn("OBSERVER_BUILD_PRINT_ARGV", contract)

    def test_python_driver_program_names_are_renamed(self) -> None:
        self.assertEqual(build_main._parser().prog, "unpackrat-build")
        self.assertEqual(native._parser().prog, "unpackrat-native")


class NativeInfrastructureContractTests(unittest.TestCase):
    """Native and MSBuild paths resolve to the renamed files that exist on disk."""

    def test_native_build_submits_the_renamed_solution_project(self) -> None:
        self.assertTrue((BUILD_ROOT / "unpackrat.proj").is_file())
        self.assertFalse((BUILD_ROOT / "Observer.proj").exists())
        tools = native.NativeTools(msbuild=Path("msbuild"), vcpkg=Path("vcpkg"))
        argv = native.build_command(REPOSITORY_ROOT, "x64", "Release", 1, tools)
        self.assertIn(str(BUILD_ROOT / "unpackrat.proj"), argv)

    def test_renamed_triplet_is_the_one_native_resolves(self) -> None:
        expected = "unpackrat-x64-windows-static"
        self.assertEqual(native.triplet("x64"), expected)
        self.assertTrue((BUILD_ROOT / "vcpkg" / "triplets" / f"{expected}.cmake").is_file())
        self.assertFalse((BUILD_ROOT / "vcpkg" / "triplets" / "observer-x64-windows-static.cmake").exists())

    def test_solution_project_imports_and_lists_the_renamed_projects(self) -> None:
        project = _read("build/unpackrat.proj")
        self.assertIn("$(MSBuildThisFileDirectory)projects\\renpy.vcxproj", project)
        # The solution's MSBuild item family is renamed to unpackrat*.
        self.assertIn("unpackratProject", project)

    def test_module_fanout_projects_import_the_renamed_props(self) -> None:
        for project in ("renpy", "rpgmaker", "zanzarah", "tests"):
            text = _read(f"build/projects/{project}.vcxproj")
            self.assertIn("..\\unpackrat-project-configurations.props", text)
            self.assertIn("..\\unpackrat-configuration.props", text)
            self.assertIn("..\\unpackrat-project.props", text)
            self.assertNotIn("ObserverConfigurationType", text)
            self.assertIn("unpackratConfigurationType", text)


class MsBuildPropertyContractTests(unittest.TestCase):
    """The renamed props files expose exactly the unpackrat* property family."""

    def test_property_files_exist_under_renamed_names(self) -> None:
        for name in (
            "unpackrat-project-configurations.props",
            "unpackrat-configuration.props",
            "unpackrat-project.props",
            "unpackrat-module-version.props",
            "unpackrat-fuzz.props",
            "unpackrat-native-analysis.ruleset",
        ):
            self.assertTrue((BUILD_ROOT / name).is_file(), name)
        for stale in (
            "ObserverProjectConfigurations.props",
            "ObserverConfiguration.props",
            "ObserverProject.props",
            "ObserverModuleVersion.props",
            "ObserverFuzz.props",
            "ObserverNativeAnalysis.ruleset",
        ):
            self.assertFalse((BUILD_ROOT / stale).exists(), stale)

    def test_project_props_use_renamed_vcpkg_triplets(self) -> None:
        text = _read("build/unpackrat-project.props")
        self.assertIn("unpackrat-$(PlatformMoniker)-windows-static", text)
        self.assertNotIn("observer-$(PlatformMoniker)-windows-static", text)

    def test_module_version_props_define_renamed_macros(self) -> None:
        text = _read("build/unpackrat-module-version.props")
        self.assertIn("UNPACKRAT_MODULE_VERSION_MAJOR=$(unpackratModuleVersionMajor)", text)
        self.assertIn("UNPACKRAT_MODULE_NAME=$(unpackratModuleName)", text)
        self.assertIn("UNPACKRAT_MODULE_FILE=$(unpackratModuleName).so", text)
        self.assertNotIn("OBSERVER_MODULE", text)

    def test_module_version_props_note_names_the_observer_host_and_props_file(self) -> None:
        text = _read("build/unpackrat-module-version.props")
        # The external FAR/Observer host keeps its identity; only own plumbing is renamed.
        self.assertIn("Observer host", text)
        self.assertNotIn("unpackrat host", text)
        # The diagnostic must name the real props file that exists on disk.
        self.assertIn("unpackrat-module-version.props", text)
        self.assertNotIn("unpackratModuleVersion.props", text)

    def test_configured_analyzer_ruleset_resolves_to_an_existing_file(self) -> None:
        text = _read("build/unpackrat-project.props")
        root = ET.fromstring(text)
        namespace = {"msbuild": "http://schemas.microsoft.com/developer/msbuild/2003"}
        elements = root.findall(".//msbuild:CodeAnalysisRuleSet", namespace)
        self.assertTrue(elements, "unpackrat-project.props must configure CodeAnalysisRuleSet")
        resolved = []
        for element in elements:
            value = element.text
            self.assertIsNotNone(value)
            resolved.append(
                Path(value.replace("$(RepositoryRoot)", str(REPOSITORY_ROOT) + os.sep))
            )
        for path in resolved:
            self.assertTrue(path.is_file(), f"configured analyzer ruleset is missing: {path}")


class CppBrandingContractTests(unittest.TestCase):
    """Own application code moves to the unpackrat namespace and macro prefixes."""

    def test_core_namespaces_are_renamed(self) -> None:
        for source in (
            "src/core/io/bounded_stream.h",
            "src/core/io/bounded_stream.cpp",
            "src/core/compression/zlib_codec.h",
            "src/core/compression/zlib_codec.cpp",
            "src/core/archive_limits.h",
        ):
            text = _read(source)
            self.assertNotIn("namespace observer", text, source)
            self.assertIn("unpackrat", text, source)

    def test_module_translation_units_use_renamed_version_macros(self) -> None:
        for source in (
            "src/modules/renpy/renpy.cpp",
            "src/modules/rpgmaker/rpgmaker.cpp",
            "src/modules/zanzarah/zanzarah.cpp",
        ):
            text = _read(source)
            self.assertIn("UNPACKRAT_MODULE_VERSION_MAJOR", text, source)
            self.assertIn("unpackrat::io::bounded_stream", text, source)
            self.assertNotIn("observer::", text, source)

    def test_version_resource_uses_renamed_macros_and_product_name(self) -> None:
        text = _read("src/modules/version.rc")
        self.assertIn("UNPACKRAT_MODULE_NAME", text)
        self.assertNotIn("OBSERVER_MODULE", text)
        self.assertIn('VALUE "ProductName", "unpackrat"', text)

    def test_observer_abi_header_keeps_its_upstream_credit(self) -> None:
        text = _read("src/api.h")
        self.assertIn("github.com/lazyhamster/Observer", text)

    def test_host_test_adapter_keeps_the_observer_identity(self) -> None:
        # The adapter models the real FAR/Observer host protocol, so its own name stays.
        self.assertTrue((REPOSITORY_ROOT / "src/tests/framework/observer.cpp").is_file())
        self.assertTrue((REPOSITORY_ROOT / "src/tests/framework/observer.h").is_file())
        for module in ("renpy", "rpgmaker", "zanzarah"):
            self.assertTrue((REPOSITORY_ROOT / f"src/modules/{module}/observer_user.ini").is_file())

    def test_host_test_adapter_qualifies_members_with_its_declared_class(self) -> None:
        # ``test::observer`` is declared in namespace ``test``; the out-of-class
        # definitions must qualify with ``observer::`` and never the own core brand.
        text = _read("src/tests/framework/observer.cpp")
        self.assertIn("observer::observer()", text)
        self.assertIn("observer::list_files(", text)
        self.assertNotIn("unpackrat::observer", text)
        self.assertNotIn("unpackrat::list_files", text)

    def test_version_resource_declares_mit_copyright_and_renamed_comment(self) -> None:
        text = _read("src/modules/version.rc")
        self.assertIn(
            'VALUE "LegalCopyright", "Copyright (c) 2019-2026 Roman Kharitonov. Licensed under MIT."',
            text,
        )
        self.assertNotIn("GNU General Public License", text)
        self.assertIn("build/unpackrat-module-version.props", text)
        self.assertNotIn("unpackratModuleVersion.props", text)


class OwnBrandStringTests(unittest.TestCase):
    """Own first-party brand strings resolve to exactly ``unpackrat``."""

    def test_owned_files_avoid_the_invented_unpackrat_modules_brand(self) -> None:
        for relative in (
            "build/pyproject.toml",
            "build/core/__init__.py",
            "build/native.py",
            "build/diagnostics.py",
            "build/source_checks.py",
            "build/core/leak.py",
            "src/tests/support/archive_fixtures.cpp",
        ):
            self.assertNotIn("unpackrat-modules", _read(relative), relative)

    def test_owned_docstrings_and_lock_use_the_unpackrat_brand(self) -> None:
        self.assertIn("unpackrat build tooling", _read("build/core/__init__.py"))
        self.assertIn("unpackrat-gflags.lock", _read("build/core/leak.py"))


class EnvironmentVariableContractTests(unittest.TestCase):
    """Own environment variables and probe markers use the unpackrat prefix."""

    def test_owned_consumers_reference_renamed_variables(self) -> None:
        # ``build/core/package.py`` is an exclusive-B file handled in the isolated snapshot,
        # so only the owned consumers are pinned here.
        expected = {
            "build/core/binary_audit.py": "UNPACKRAT_OUT_DIR",
            "build/core/leak.py": "UNPACKRAT_LEAK_PROBE",
            "build/diagnostics.py": "UNPACKRAT_TEST_CORPUS",
        }
        for source, token in expected.items():
            self.assertIn(token, _read(source), source)

    def test_probe_marker_matches_the_cpp_probe(self) -> None:
        self.assertIn("UNPACKRAT_LEAK_PROBE", _read("src/tests/leaks/probe.cpp"))
        self.assertIn("UNPACKRAT_PACKAGE_MODULE", _read("src/tests/framework/observer.cpp"))
        self.assertIn("UNPACKRAT_TEST_CORPUS", _read("src/tests/framework/testcase.cpp"))


class PackageNameContractTests(unittest.TestCase):
    """The uv project names match between the manifests and their lockfiles."""

    def test_build_project_name_is_renamed_and_locked(self) -> None:
        self.assertIn('name = "unpackrat-build"', _read("build/pyproject.toml"))
        self.assertIn('name = "unpackrat-build"', _read("build/uv.lock"))
        self.assertNotIn('name = "observer-build"', _read("build/uv.lock"))

    def test_fixture_listing_tool_name_is_renamed_and_locked(self) -> None:
        self.assertIn('name = "unpackrat"', _read("src/tests/framework/tools/pyproject.toml"))
        self.assertIn('name = "unpackrat"', _read("src/tests/framework/tools/uv.lock"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

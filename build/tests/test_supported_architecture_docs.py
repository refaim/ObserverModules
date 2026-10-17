"""Document contract for the two supported Observer module architectures.

The Observer plugin host ships no ARM64 build that could load an unpackrat module, so the
project supports exactly x86 and x64. These tests pin the user-facing documents to that
target set: only x86 and x64 may be advertised, no ARM prerequisite, deferral or release
claim may survive, the CI narrative must describe the real per-stage required checks, and
the per-module symbol bundle must describe one PDB per supported architecture.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]

README = REPOSITORY / "README.md"
AGENTS = REPOSITORY / "AGENTS.md"
BUILD_SYSTEM = REPOSITORY / "docs" / "build-system.md"
METHODOLOGY = REPOSITORY / "docs" / "critical-software-methodology.md"

DOCUMENTS = (README, AGENTS, BUILD_SYSTEM, METHODOLOGY)

MODULES = ("renpy", "rpgmaker", "zanzarah", "godot")
LANGUAGES = ("en", "ru")

# The v145 master announced Windows XP..11 support.  Godot and Ren'Py carry that announcement in
# a historical section because a later licence-notices patch re-released them; the other modules
# still publish it as their current section.
PLATFORM_VERSION = {"godot": "1.1.0", "renpy": "3.2.0"}

# A word-start match for "arm" catches "ARM", "ARM64" and "arm64" without tripping on
# unrelated words such as the "farmanager" host domain.
_ARM = re.compile(r"(?i)\barm")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def flat(text: str) -> str:
    """Collapse markdown hard-wrapping so a phrase can be asserted across line breaks."""

    return " ".join(text.split())


class SupportedArchitectureDocumentTests(unittest.TestCase):
    def test_no_document_names_arm_as_a_target_prerequisite_or_release(self) -> None:
        for document in DOCUMENTS:
            with self.subTest(document=document.name):
                text = read(document)
                match = _ARM.search(text)
                self.assertIsNone(
                    match,
                    f"{document.name} still mentions ARM near {text[match.start():match.start() + 40]!r}"
                    if match is not None
                    else document.name,
                )

    def test_an_arm_target_in_a_document_is_rejected(self) -> None:
        case = SupportedArchitectureDocumentTests("test_no_document_names_arm_as_a_target_prerequisite_or_release")
        with mock.patch(__name__ + ".read", return_value="ARM64"), self.assertRaisesRegex(
            AssertionError, "README.md still mentions ARM"
        ):
            case.test_no_document_names_arm_as_a_target_prerequisite_or_release()

    def test_readme_advertises_only_x86_and_x64(self) -> None:
        readme = read(README)
        download = flat(readme[readme.index("## Download"):readme.index("## Install")])
        building = flat(readme[readme.index("## Building"):readme.index("## Links")])
        for section, text in (("Download", download), ("Building", building)):
            with self.subTest(section=section):
                self.assertIn("x86", text)
                self.assertIn("x64", text)
        # The install guidance keeps tying the ZIP architecture to FAR Manager and Observer.
        self.assertIn("matches your FAR Manager and Observer build", download)
        self.assertIn("(x86 or x64)", download)
        self.assertIn("for x86 and x64", building)

    def test_central_inventory_scopes_and_changelog_links(self) -> None:
        # The reviewed component inventory carries every upstream identity with a reviewed scope;
        # the README keeps the bilingual changelog links for every shipped module.
        inventory = json.loads(
            (REPOSITORY / "build" / "license-inventory.json").read_text(encoding="utf-8")
        )
        self.assertEqual(inventory["schema"], "unpackrat-license-inventory/3")
        upstream = (
            "https://github.com/nlohmann/json",
            "https://github.com/catchorg/Catch2",
            "https://github.com/lazyhamster/Observer",
            "https://github.com/Cyan4973/xxHash",
            "https://github.com/zlib-ng/zlib-ng",
            "https://github.com/luxrck/rgssad",
            "https://github.com/birkenfeld/serde-pickle",
            "https://github.com/Shizmob/rpatool",
        )
        urls = [
            str(component["source"].get("url"))
            for component in inventory["components"]
            if isinstance(component, dict) and isinstance(component.get("source"), dict)
        ]
        for project in upstream:
            with self.subTest(project=project):
                self.assertTrue(any(url.startswith(project) for url in urls), project)
        allowed_scopes = {"runtime", "test", "build", "reference"}
        for component in inventory["components"]:
            with self.subTest(component=component.get("name") if isinstance(component, dict) else component):
                self.assertIsInstance(component, dict)
                self.assertIn(component["scope"], allowed_scopes)
        readme = read(README)
        for module in MODULES:
            for language in LANGUAGES:
                with self.subTest(module=module, language=language):
                    self.assertIn(f"src/modules/{module}/ChangeLog.{language}.txt", readme)

    def test_build_system_release_contract_and_prerequisites_cover_two_architectures(self) -> None:
        docs = read(BUILD_SYSTEM)
        contract = flat(docs[docs.index("Release contract"):docs.index("## Prerequisites")])
        prerequisites = flat(docs[docs.index("## Prerequisites"):docs.index("## Map")])
        self.assertIn("x86, x64;", contract)
        self.assertIn("x86 and x64", prerequisites)
        self.assertIn("godot/vX.Y.Z", contract)

    def test_build_system_ci_describes_the_real_stage_checks(self) -> None:
        docs = read(BUILD_SYSTEM)
        ci = flat(docs[docs.index("## CI"):docs.index("## Open gaps")])
        # No aggregate gate survives: the required check names are the real per-stage jobs, so the
        # narrative must name them directly instead of collapsing them behind `source`/`x86`/`x64`.
        self.assertNotIn("aggregate", ci.lower())
        self.assertNotIn("Three verification jobs", ci)
        self.assertNotIn("Four verification jobs", ci)
        self.assertIn("required check", ci)
        for name in ("source-checks", "native-x86", "native-x64"):
            with self.subTest(name=name):
                self.assertIn(name, ci)

    def test_build_system_pdb_bundle_describes_one_pdb_per_supported_architecture(self) -> None:
        docs = read(BUILD_SYSTEM)
        paragraph = flat(docs[docs.index("A module release publishes"):docs.index("## CI")])
        self.assertIn("exactly two per-architecture module archives", paragraph)
        self.assertIn(
            "the published bundle carries one `<module>.pdb` for each of the two supported "
            "architectures (`x86` and `x64`)",
            paragraph,
        )

    def test_build_system_rejects_unsupported_targets_instead_of_deferring_them(self) -> None:
        docs = read(BUILD_SYSTEM)
        # The build narrative keeps its three architecture rejection rules and never promises a
        # silent deferral of a target the build does not support.
        self.assertIn("architecture rejections", docs)
        self.assertNotIn("reported deferred rather than executed", docs)

    def test_methodology_exact_toolchain_statement_is_x86_and_x64_only(self) -> None:
        docs = read(METHODOLOGY)
        item = flat(
            docs[docs.index("**Exact-toolchain tests:**"):docs.index("**Static analysis:**")]
        )
        self.assertIn("x86 and x64", item)
        self.assertNotIn("deferred", item)

    def test_methodology_describes_the_real_binary_gate_without_removed_checks(self) -> None:
        # The packaged bytes are not re-audited, so the methodology must not promise packaging
        # smoke tests, a PDB archive audit, an archive-content allowlist or a packaged-DLL-bytes
        # smoke test; it may only name the native gates the build actually runs.
        docs = flat(read(METHODOLOGY))
        for removed in (
            "packaging smoke tests",
            "package smoke tests",
            "PDB archive audit",
            "archive-content allowlists",
            "smoke tests of the packaged DLL bytes",
        ):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, docs)
        self.assertIn("exact exports", docs)
        self.assertIn("forbidden imports", docs)
        self.assertIn("BinSkim", docs)

    def test_changelogs_drop_the_arm_build_claim_and_keep_the_version_history(self) -> None:
        headers = {"en": "Version", "ru": "\u0412\u0435\u0440\u0441\u0438\u044f"}
        for module in MODULES:
            version = (
                REPOSITORY / "src" / "modules" / module / "VERSION"
            ).read_text(encoding="utf-8").strip()
            for language in LANGUAGES:
                name = f"ChangeLog.{language}.txt"
                with self.subTest(module=module, language=language):
                    lines = read(REPOSITORY / "src" / "modules" / module / name).splitlines()
                    self.assertIsNone(_ARM.search("\n".join(lines)), f"{module}/{name} claims ARM")
                    # The newest heading is derived from the current VERSION file, and the top
                    # section keeps at least one meaningful entry. Earlier releases stay in the
                    # history, so the original 1.0.0 lineage must survive.
                    self.assertEqual(lines[0], f"{headers[language]} {version}")
                    self.assertTrue(lines[1].startswith("-"), lines[1])
                    headings = tuple(headers.values())
                    body = lines[2:]
                    end = next(
                        (index for index, line in enumerate(body) if line.startswith(headings)),
                        len(body),
                    )
                    entries = [
                        line for line in body[:end] if line.lstrip().startswith(("*", "+"))
                    ]
                    self.assertGreaterEqual(len(entries), 1)
                    # The v145 Windows XP..11 announcement is preserved: in the current section
                    # of an untouched module, or in the historical section of a re-released one.
                    announcement = {
                        "en": "* Added support for all Windows versions from XP through Windows 11.",
                        "ru": "* Поддерживаются все версии Windows от XP до Windows 11.",
                    }[language]
                    platform_section = f"{headers[language]} {PLATFORM_VERSION.get(module, version)}"
                    text = "\n".join(lines)
                    self.assertIn(platform_section, text)
                    section = text.split(platform_section, 1)[1].split("\n\n", 1)[0]
                    self.assertIn(announcement, [line.strip() for line in section.splitlines()])
                    # The historical 1.0.0 lineage survives.
                    self.assertIn(f"{headers[language]} 1.0.0", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()

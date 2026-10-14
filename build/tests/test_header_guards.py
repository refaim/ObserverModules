from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

# The project namespace that owns every first-party guard macro: uppercase tokens
# separated by single underscores. Requiring the prefix makes the macro project-owned,
# and forbidding a leading underscore or a doubled underscore keeps it nonreserved.
_PROJECT_GUARD = re.compile(r"UNPACKRAT_[A-Z0-9]+(?:_[A-Z0-9]+)*")


def _headers() -> list[Path]:
    return sorted(SRC.rglob("*.h"))


def _outer_guard(text: str) -> str | None:
    """Return the macro of the first directive line, or None when it is not an #ifndef."""
    for line in text.splitlines():
        if line.strip():
            match = re.match(r"#\s*ifndef\s+([A-Za-z0-9_]+)", line)
            return match.group(1) if match else None
    return None


def whole_file_guard_faults(text: str) -> list[str]:
    """Return the reasons ``text`` is not wrapped in one whole-file guard, or ``[]``.

    The outer guard must open on the first directive, define that very macro on the next,
    and have its own ``#endif`` close on the last line.  Anything after a guard that closes
    early - including a later, unrelated ``#if`` block - is a fault, so a header that only
    looks guarded on its first and last lines is rejected rather than mistaken for protected.
    """

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return ["the file is empty"]
    opening = re.match(r"#\s*ifndef\s+([A-Za-z0-9_]+)\s*$", lines[0])
    if opening is None:
        return ["the first directive is not an outer #ifndef guard"]
    guard = opening.group(1)
    faults: list[str] = []
    definition = re.match(r"#\s*define\s+([A-Za-z0-9_]+)\s*$", lines[1]) if len(lines) > 1 else None
    if definition is None or definition.group(1) != guard:
        faults.append(f"guard {guard} is not followed by a matching #define")
    depth = 0
    closing = None
    for index, line in enumerate(lines):
        keyword = re.match(r"#\s*(\w+)", line)
        if keyword is None:
            continue
        directive = keyword.group(1)
        if directive in ("if", "ifdef", "ifndef"):
            depth += 1
        elif directive == "endif":
            depth -= 1
            if depth == 0:
                closing = index
                break
    if closing is None:
        faults.append("the outer guard is never closed")
    elif closing != len(lines) - 1:
        faults.append("the outer guard closes before the end of the file")
    return faults


class HeaderGuardTests(unittest.TestCase):
    def test_first_party_header_set_is_nonempty(self) -> None:
        self.assertTrue(_headers(), "no first-party headers were found under src")

    def test_each_header_opens_with_a_project_outer_guard(self) -> None:
        for header in _headers():
            relative = header.relative_to(ROOT).as_posix()
            with self.subTest(header=relative):
                text = header.read_text(encoding="utf-8")
                self.assertFalse(
                    re.search(r"#\s*pragma\s+once\b", text),
                    f"{relative} still relies on #pragma once",
                )
                body = [line for line in text.splitlines() if line.strip()]
                self.assertTrue(body, f"{relative} is empty")
                opening = re.match(r"#\s*ifndef\s+([A-Za-z0-9_]+)", body[0])
                self.assertIsNotNone(
                    opening, f"{relative} does not open with an outer #ifndef guard"
                )
                guard = opening.group(1)
                self.assertIsNotNone(
                    _PROJECT_GUARD.fullmatch(guard),
                    f"{relative} guard {guard!r} is not a project-owned nonreserved macro",
                )
                definition = re.match(r"#\s*define\s+([A-Za-z0-9_]+)", body[1])
                self.assertIsNotNone(
                    definition, f"{relative} guard is not followed by a matching #define"
                )
                self.assertEqual(
                    definition.group(1), guard,
                    f"{relative} #define does not match its #ifndef guard",
                )
                self.assertTrue(
                    re.match(r"#\s*endif\b", body[-1]),
                    f"{relative} does not close with a final #endif",
                )
                self.assertEqual(
                    whole_file_guard_faults(text), [],
                    f"{relative} is not wrapped in one whole-file guard",
                )

    def test_guard_macros_are_unique_across_headers(self) -> None:
        guards = [_outer_guard(header.read_text(encoding="utf-8")) for header in _headers()]
        self.assertTrue(all(guards), "every header must expose an outer guard macro")
        self.assertEqual(
            len(set(guards)), len(guards), "guard macros are reused across headers"
        )

    def test_outer_guard_helper_reports_no_guard_for_unprotected_text(self) -> None:
        # The helper's negative branch is the whole point of the foreign-text fixtures below;
        # pin it directly so a header whose first directive is not #ifndef is never mistaken
        # for a guarded one.
        self.assertIsNone(_outer_guard(""))
        self.assertIsNone(_outer_guard("\n   \n"))
        self.assertIsNone(_outer_guard("#pragma once\n\nstruct Fib {};\n"))
        self.assertIsNone(_outer_guard("// leading comment\nstruct Fib {};\n"))
        self.assertEqual(_outer_guard("#ifndef UNPACKRAT_FIB_H\n#endif\n"), "UNPACKRAT_FIB_H")


class HeaderGuardNegativeFixtureTests(unittest.TestCase):
    """Whole-file fixtures for the guard checker the positive test shares.

    The positive test inspects the first, second and last lines of every real header, so a
    guard that closes *early* - before the declarations - or foreign text that never opens an
    ``#ifndef`` at all would slip through.  These fixtures pin ``whole_file_guard_faults``: it
    must still accept the nested conditionals a real header such as ``src/api.h`` carries while
    rejecting the early close, the missing close, a mismatched ``#define`` and an unbalanced
    nested ``#if``.
    """

    def _faults(self, text: str) -> list[str]:
        checker = globals().get("whole_file_guard_faults")
        self.assertTrue(
            callable(checker),
            "the whole-file guard checker whole_file_guard_faults is not implemented yet",
        )
        return list(checker(text))

    def test_early_outer_guard_closure_before_declarations_is_rejected(self) -> None:
        # The outer guard closes after its own #define, then declarations follow with no
        # reopened guard: the file is not actually protected and must be flagged.
        text = (
            "#ifndef UNPACKRAT_EARLY_H\n"
            "#define UNPACKRAT_EARLY_H\n"
            "#endif\n"
            "\n"
            "struct FixtureEarly {};\n"
        )
        self.assertTrue(self._faults(text))

    def test_an_early_guard_followed_by_an_unrelated_guard_is_rejected(self) -> None:
        # The original review counterexample: the outer guard closes early and a *different*
        # #ifndef/#endif then wraps only the declarations, which must still be a fault.
        text = (
            "#ifndef UNPACKRAT_FIRST_H\n"
            "#define UNPACKRAT_FIRST_H\n"
            "#endif\n"
            "\n"
            "#ifndef UNPACKRAT_SECOND_H\n"
            "struct FixtureSecond {};\n"
            "#endif\n"
        )
        self.assertTrue(self._faults(text))

    def test_a_guard_that_never_closes_is_rejected(self) -> None:
        text = (
            "#ifndef UNPACKRAT_OPEN_H\n"
            "#define UNPACKRAT_OPEN_H\n"
            "\n"
            "struct FixtureOpen {};\n"
        )
        self.assertTrue(self._faults(text))

    def test_a_mismatched_define_is_rejected(self) -> None:
        text = (
            "#ifndef UNPACKRAT_DEFINE_H\n"
            "#define UNPACKRAT_OTHER_H\n"
            "\n"
            "struct FixtureDefine {};\n"
            "\n"
            "#endif\n"
        )
        self.assertTrue(self._faults(text))

    def test_an_unbalanced_nested_conditional_is_rejected(self) -> None:
        text = (
            "#ifndef UNPACKRAT_UNBALANCED_H\n"
            "#define UNPACKRAT_UNBALANCED_H\n"
            "\n"
            "#if 1\n"
            "struct FixtureUnbalanced {};\n"
            "\n"
            "#endif\n"
        )
        self.assertTrue(self._faults(text))

    def test_a_bare_guard_line_without_a_body_is_rejected(self) -> None:
        self.assertTrue(self._faults("#ifndef UNPACKRAT_BARE_H\n"))

    def test_a_guard_with_nested_conditionals_is_accepted(self) -> None:
        # A protected header nests its own platform guards inside the outer guard, so the
        # checker must follow the depth to the matching outer #endif rather than the first one.
        text = (
            "#ifndef UNPACKRAT_NESTED_H\n"
            "#define UNPACKRAT_NESTED_H\n"
            "\n"
            "#ifndef WINVER\n"
            "#define WINVER 0x0600\n"
            "#endif\n"
            "\n"
            "struct FixtureNested {};\n"
            "\n"
            "#endif\n"
        )
        self.assertEqual(self._faults(text), [])

    def test_the_checked_in_api_header_is_accepted(self) -> None:
        self.assertEqual(self._faults((SRC / "api.h").read_text(encoding="utf-8")), [])

    def test_empty_text_is_rejected(self) -> None:
        for text in ("", "\n\n", "   \n\t\n"):
            with self.subTest(text=repr(text)):
                self.assertTrue(self._faults(text))

    def test_text_without_an_outer_ifndef_is_rejected(self) -> None:
        for text in (
            "#pragma once\n\nstruct FixturePragma {};\n",
            "// a bare declaration\nstruct FixtureBare {};\n",
        ):
            with self.subTest(text=text.splitlines()[0]):
                self.assertTrue(self._faults(text))

    def test_a_whole_file_guard_is_accepted(self) -> None:
        # The checker must not simply reject everything: a correctly wrapped header has no
        # fault, so PHASE2 cannot satisfy the negatives with an always-fail helper.
        self.assertEqual(
            self._faults(
                "#ifndef UNPACKRAT_GOOD_H\n"
                "#define UNPACKRAT_GOOD_H\n"
                "\n"
                "struct FixtureGood {};\n"
                "\n"
                "#endif\n"
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()

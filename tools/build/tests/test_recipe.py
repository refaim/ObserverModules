from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


BUILD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD_ROOT))

from core.graph import GraphError  # noqa: E402
from core.recipe import Recipe, RecipeError  # noqa: E402


def rendered_recipe(**overrides: object) -> str:
    document: dict[str, object] = {
        "name": "analyze-renpy-x64",
        "pool": "slot",
        "inputs": ["compile-renpy-x64"],
        "script": {
            "exec": ["pwsh.exe", "-NoProfile", "-Command", "-"],
            "data": "Write-Output 'привет'\r\n",
        },
    }
    document.update(overrides)
    return json.dumps(document, ensure_ascii=False)


class RecipeTests(unittest.TestCase):
    def test_repository_recipe_is_parsed_without_reordering_process_data(self) -> None:
        recipe = Recipe.parse(rendered_recipe())

        self.assertEqual(recipe.name, "analyze-renpy-x64")
        self.assertEqual(recipe.pool, "slot")
        self.assertEqual(recipe.inputs, ("compile-renpy-x64",))
        self.assertEqual(recipe.argv, ("pwsh.exe", "-NoProfile", "-Command", "-"))
        self.assertEqual(recipe.data, "Write-Output 'привет'\r\n".encode())

    def test_node_bridge_uses_signed_uid_direct_dependencies_and_process_data(self) -> None:
        recipe = Recipe.parse(rendered_recipe())
        current = recipe.to_node(
            uid="0123456789abcdef0123456789abcdef",
            env={"OBSERVER_OUT_DIR": r"C:\repo\out", "ZED": "last"},
            cwd=r"C:\repo\out\work\one",
        )

        self.assertEqual(current.name, recipe.name)
        self.assertEqual(current.inputs, ("compile-renpy-x64",))
        self.assertEqual(current.command.argv, recipe.argv)
        self.assertEqual(current.command.stdin, recipe.data)
        self.assertEqual(current.command.cwd, r"C:\repo\out\work\one")
        self.assertEqual(
            current.command.env,
            (("OBSERVER_OUT_DIR", r"C:\repo\out"), ("ZED", "last")),
        )

    def test_only_required_repository_fields_are_interpreted(self) -> None:
        recipe = Recipe.parse(rendered_recipe(metadata={"owner": "repository"}))
        duplicate_name = rendered_recipe().replace(
            '{"name": "analyze-renpy-x64",',
            '{"name": "old", "name": "analyze-renpy-x64",',
        )

        self.assertEqual(recipe.name, "analyze-renpy-x64")
        self.assertEqual(Recipe.parse(duplicate_name).name, "analyze-renpy-x64")

    def test_invalid_json_or_missing_required_fields_are_rejected(self) -> None:
        with self.assertRaises(RecipeError):
            Recipe.parse("not json")

        valid = json.loads(rendered_recipe())
        for field in ("name", "pool", "inputs", "script"):
            document = dict(valid)
            del document[field]
            with self.subTest(field=field), self.assertRaises(RecipeError):
                Recipe.parse(json.dumps(document))

        for field in ("exec", "data"):
            script = dict(valid["script"])
            del script[field]
            with self.subTest(script_field=field), self.assertRaises(RecipeError):
                Recipe.parse(json.dumps({**valid, "script": script}))

    def test_graph_and_process_descriptors_remain_validation_boundaries(self) -> None:
        recipe = Recipe.parse(rendered_recipe())

        with self.assertRaises(GraphError):
            recipe.to_node(uid="not-md5")
        with self.assertRaises(GraphError):
            Recipe.parse(rendered_recipe(name="unsafe name")).to_node(uid="0" * 32)
        with self.assertRaises(GraphError):
            Recipe.parse(
                rendered_recipe(script={"exec": ["bad\0argv"], "data": ""})
            ).to_node(uid="0" * 32)


if __name__ == "__main__":
    unittest.main(verbosity=2)

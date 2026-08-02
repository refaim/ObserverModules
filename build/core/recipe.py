"""Bridge trusted repository-rendered JSON recipes to graph nodes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json

from core.graph import Command, GraphError, Node, Result


class RecipeError(ValueError):
    """A required repository recipe field is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Recipe:
    name: str
    pool: str
    inputs: tuple[str, ...]
    argv: tuple[str, ...]
    data: bytes
    results: tuple[Result, ...] = ()

    @classmethod
    def parse(cls, rendered: str | bytes) -> Recipe:
        """Read only the fields emitted by the repository templates."""

        try:
            document = json.loads(rendered)
            script = document["script"]
            declared_results = document["results"]
            if not isinstance(declared_results, list):
                raise TypeError("results must be a list")
            return cls(
                name=document["name"],
                pool=document["pool"],
                inputs=tuple(document["inputs"]),
                argv=tuple(script["exec"]),
                data=script["data"].encode("utf-8"),
                results=tuple(
                    Result(
                        result["id"],
                        result["kind"],
                        result["media_type"],
                        result["path"],
                    )
                    for result in declared_results
                ),
            )
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            KeyError,
            TypeError,
            AttributeError,
            GraphError,
        ) as error:
            raise RecipeError("rendered recipe is missing required JSON fields") from error

    def to_node(
        self,
        *,
        uid: str,
        env: Mapping[str, str] | Iterable[tuple[str, str]] = (),
        cwd: str | None = None,
    ) -> Node:
        environment = env.items() if isinstance(env, Mapping) else env
        return Node(
            name=self.name,
            uid=uid,
            pool=self.pool,
            command=Command(self.argv, tuple(environment), cwd, self.data),
            inputs=self.inputs,
            results=self.results,
        )

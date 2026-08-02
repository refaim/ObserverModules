"""Bridge trusted repository-rendered JSON recipes to graph nodes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json

from core.graph import Command, Node


class RecipeError(ValueError):
    """A required repository recipe field is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Recipe:
    name: str
    pool: str
    inputs: tuple[str, ...]
    argv: tuple[str, ...]
    data: bytes

    @classmethod
    def parse(cls, rendered: str | bytes) -> Recipe:
        """Read only the fields emitted by the repository templates."""

        try:
            document = json.loads(rendered)
            script = document["script"]
            return cls(
                name=document["name"],
                pool=document["pool"],
                inputs=tuple(document["inputs"]),
                argv=tuple(script["exec"]),
                data=script["data"].encode("utf-8"),
            )
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            KeyError,
            TypeError,
            AttributeError,
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
        )

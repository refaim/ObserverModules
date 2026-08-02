"""Create signed graph nodes from rendered repository recipes."""

from dataclasses import dataclass
import json
from pathlib import Path

from core.graph import Node
from core.recipe import Recipe
from core.render import TemplateRenderer
from core.sign import content_uid


@dataclass(frozen=True, slots=True)
class NodeFactory:
    renderer: TemplateRenderer
    cwd: Path
    identity: dict[str, str]
    environment: tuple[tuple[str, str], ...] = ()

    def make(
        self,
        template: str,
        name: str,
        pool: str,
        variables: dict[str, object],
        *,
        files: dict[str, bytes],
        dependencies: tuple[Node, ...] = (),
        config: dict[str, str],
        identity: dict[str, str] | None = None,
        environment: tuple[tuple[str, str], ...] | None = None,
        cwd: Path | None = None,
    ) -> Node:
        descriptor = {
            "name": name,
            "pool": pool,
            "inputs": [node.name for node in dependencies],
        } | variables
        rendered = self.renderer.render(template, descriptor)
        node_environment = self.environment if environment is None else environment
        node_cwd = cwd or self.cwd
        runtime = json.dumps(
            {"cwd": str(node_cwd), "environment": node_environment},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        uid = content_uid(
            recipe=rendered,
            inputs=files,
            dependencies={node.name: node.uid for node in dependencies},
            toolchain=identity or self.identity,
            config=dict(config) | {"runtime": runtime},
        )
        return Recipe.parse(rendered).to_node(
            uid=uid,
            env=node_environment,
            cwd=str(node_cwd),
        )

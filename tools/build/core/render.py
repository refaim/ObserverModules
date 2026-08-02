"""Deterministic rendering for inherited build recipes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jinja2


def ps_quote(value: object) -> str:
    """Return *value* as one PowerShell single-quoted string literal."""

    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class TemplateRenderer:
    """Render flat, inherited Jinja recipes with undefined values forbidden."""

    def __init__(self, template_dir: Path) -> None:
        root = template_dir.resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(root)

        self._environment = jinja2.Environment(
            loader=jinja2.FileSystemLoader(root),
            undefined=jinja2.StrictUndefined,
            autoescape=False,
            auto_reload=False,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
            newline_sequence="\n",
        )
        self._environment.filters["json"] = _json
        self._environment.filters["ps_quote"] = ps_quote

    def render(self, template_name: str, variables: Mapping[str, Any]) -> str:
        """Render *template_name* using an explicit variable mapping."""

        template = self._environment.get_template(template_name)
        return template.render(dict(variables))

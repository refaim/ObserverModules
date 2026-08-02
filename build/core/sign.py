"""Canonical MD5 identities for rendered content-addressed recipes."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import TypeAlias


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = (
    JsonScalar | list["JsonValue"] | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
)
_MD5_UID = re.compile(r"[0-9a-f]{32}")


def _items(
    mapping: Mapping[str, object], description: str
) -> list[tuple[str, object]]:
    result: list[tuple[str, object]] = []
    for key, value in mapping.items():
        if not isinstance(key, str):
            raise TypeError(f"{description} must be strings")
        result.append((key, value))
    return sorted(result)


def _identity(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {
            key: _identity(item)
            for key, item in _items(value, "identity mapping keys")
        }
    if isinstance(value, (list, tuple)):
        return [_identity(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported identity value: {type(value).__name__}")


def content_uid(
    *,
    recipe: str | bytes,
    inputs: Mapping[str, bytes],
    dependencies: Mapping[str, str],
    toolchain: Mapping[str, JsonValue],
    config: Mapping[str, JsonValue],
) -> str:
    """Hash one unambiguous canonical-JSON description of a node."""

    recipe_bytes = recipe.encode("utf-8") if isinstance(recipe, str) else recipe
    if type(recipe_bytes) is not bytes:
        raise TypeError("recipe must be str or bytes")

    input_data: list[list[str]] = []
    for name, data in _items(inputs, "input names"):
        if type(data) is not bytes:
            raise TypeError("input contents must be bytes")
        input_data.append([name, data.hex()])

    dependency_data: list[list[str]] = []
    for name, uid in _items(dependencies, "dependency names"):
        if not isinstance(uid, str) or _MD5_UID.fullmatch(uid) is None:
            raise ValueError(f"dependency {name!r} does not have a canonical MD5 UID")
        dependency_data.append([name, uid])

    payload = json.dumps(
        {
            "config": _identity(config),
            "dependencies": dependency_data,
            "format": "observer-build-content-v1",
            "inputs": input_data,
            "recipe": recipe_bytes.hex(),
            "toolchain": _identity(toolchain),
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.md5(payload, usedforsecurity=False).hexdigest()

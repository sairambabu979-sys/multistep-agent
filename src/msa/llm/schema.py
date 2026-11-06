"""JSON Schemas that structured outputs and strict tools accept.

Two kinds of mistake are caught when a tool or answer type is defined, not as a 400 in
the middle of a paid run:

- **Open or partial objects.** Strict mode needs every object closed
  (``additionalProperties: false``), and here every property must also be required.
  Pydantic emits both for ``extra="forbid"`` models whose fields have no defaults. An open
  ``dict`` field is refused rather than silently narrowed to ``{}``.
- **Unsupported keywords.** Strict mode rejects numeric and length constraints
  (``minimum``, ``maxLength``, ...) and most formats. The SDK's own ``transform_schema``
  moves those into the field's description, where the model still reads them; pydantic
  still enforces them on every tool input and answer, client-side.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import anthropic
from pydantic import BaseModel

# The keywords a strict schema node may carry (what transform_schema emits).
SUPPORTED_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "description",
        "enum",
        "format",
        "items",
        "minItems",
        "properties",
        "required",
        "title",
        "type",
    }
)


class SchemaError(ValueError):
    pass


def _nodes(node: Any, path: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every schema node, walked structurally: property names are never mistaken for
    keywords, so a field called ``minimum`` is fine."""
    if not isinstance(node, dict):
        return
    yield path, node
    for key in ("properties", "$defs"):
        for name, child in (node.get(key) or {}).items():
            yield from _nodes(child, f"{path}/{key}/{name}")
    if isinstance(node.get("items"), dict):
        yield from _nodes(node["items"], f"{path}/items")
    for key in ("anyOf", "allOf", "oneOf"):
        for index, child in enumerate(node.get(key) or []):
            yield from _nodes(child, f"{path}/{key}/{index}")


def strictness_problems(schema: dict[str, Any], *, keywords: bool = True) -> list[str]:
    problems: list[str] = []
    for path, node in _nodes(schema, "#"):
        if keywords:
            problems += [
                f"{path}: {key!r} is not supported in strict mode"
                for key in sorted(set(node) - SUPPORTED_KEYWORDS)
            ]
        if node.get("type") != "object" and "properties" not in node:
            continue
        if node.get("additionalProperties") is not False:
            problems.append(f"{path}: additionalProperties must be false")
        missing = sorted(set(node.get("properties", {})) - set(node.get("required", [])))
        if missing:
            problems.append(f"{path}: every property must be required, not {missing}")
    return problems


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The schema to send for ``model``: checked closed and complete, then transformed so it
    holds only keywords strict mode supports."""

    def refuse(problems: list[str]) -> SchemaError:
        return SchemaError(f"{model.__name__} is not strict-compatible: " + "; ".join(problems))

    raw = model.model_json_schema()
    if problems := strictness_problems(raw, keywords=False):
        raise refuse(problems)
    try:
        schema: dict[str, Any] = anthropic.transform_schema(raw)
    except ValueError as exc:  # e.g. a field typed Any has no "type"
        raise refuse([str(exc)]) from exc
    if problems := strictness_problems(schema):
        raise refuse(problems)
    return schema

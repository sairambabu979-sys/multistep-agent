"""JSON Schemas that structured outputs and strict tools accept.

Strict mode requires every object to close itself (``additionalProperties: false``) and to
require all of its properties. Pydantic emits the first for ``extra="forbid"`` models and
the second for fields without defaults. A model that slips (a default, an open dict) fails
here, at definition time, rather than as a 400 in the middle of a paid run.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel


class SchemaError(ValueError):
    pass


def _objects(node: Any, path: str) -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield path, node
        for key, value in node.items():
            yield from _objects(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _objects(value, f"{path}/{index}")


def strictness_problems(schema: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for path, obj in _objects(schema, "#"):
        properties = set(obj.get("properties", {}))
        if obj.get("additionalProperties") is not False:
            problems.append(f"{path}: additionalProperties must be false")
        missing = sorted(properties - set(obj.get("required", [])))
        if missing:
            problems.append(f"{path}: every property must be required, not {missing}")
    return problems


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    if problems := strictness_problems(schema):
        raise SchemaError(f"{model.__name__} is not strict-compatible: " + "; ".join(problems))
    return schema

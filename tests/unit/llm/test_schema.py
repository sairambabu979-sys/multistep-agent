"""Strict-mode schemas are checked when a tool or answer type is defined, not mid-run."""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from msa.llm.schema import SchemaError, strict_schema, strictness_problems


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Finding(Strict):
    path: str
    line: int
    severity: Literal["blocker", "nit"]


class Review(Strict):
    findings: list[Finding]
    summary: str | None


class Open(BaseModel):
    path: str


class Defaulted(Strict):
    path: str
    line: int = 0


class NestedOpen(Strict):
    inner: Open


def test_a_closed_model_with_required_fields_is_strict() -> None:
    schema = strict_schema(Review)
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["Finding"]["additionalProperties"] is False
    assert strictness_problems(schema) == []


@pytest.mark.parametrize(
    ("model", "problem"),
    [
        (Open, "#: additionalProperties must be false"),
        (Defaulted, "#: every property must be required, not ['line']"),
        (NestedOpen, "#/$defs/Open: additionalProperties must be false"),
    ],
)
def test_models_strict_mode_would_reject_fail_at_definition(
    model: type[BaseModel], problem: str
) -> None:
    with pytest.raises(SchemaError, match=f"{model.__name__} is not strict-compatible") as info:
        strict_schema(model)
    assert problem in str(info.value)


def test_every_problem_is_reported_with_its_location() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a"],
        "$defs": {"X": {"properties": {"c": {}}, "additionalProperties": True, "required": ["c"]}},
        "anyOf": [{"type": "object", "additionalProperties": False}],
    }
    assert strictness_problems(schema) == [
        "#: additionalProperties must be false",
        "#: every property must be required, not ['b']",
        "#/$defs/X: additionalProperties must be false",
    ]


def test_non_object_schemas_have_nothing_to_close() -> None:
    assert strictness_problems({"type": "array", "items": {"type": "string"}}) == []

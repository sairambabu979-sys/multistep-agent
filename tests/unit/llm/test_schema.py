"""Strict-mode schemas are checked when a tool or answer type is defined, not mid-run."""

from __future__ import annotations

from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

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


class Constrained(Strict):
    line: int = Field(ge=1, description="1-based")
    name: str = Field(max_length=10)
    minimum: str  # a property named like a keyword is just a property


class OpenDict(Strict):
    labels: dict[str, str]


class Untyped(Strict):
    anything: Any


def test_unsupported_constraints_move_into_the_description_and_are_still_enforced() -> None:
    schema = strict_schema(Constrained)
    line = schema["properties"]["line"]
    assert "minimum" not in line
    assert line["description"] == "1-based\n\n{minimum: 1}"
    assert "maxLength" not in schema["properties"]["name"]
    assert strictness_problems(schema) == []
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        Constrained(line=0, name="x", minimum="m")


def test_unsupported_keywords_are_reported_where_they_occur() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["n"],
        "properties": {"n": {"type": "integer", "minimum": 1, "multipleOf": 2}},
    }
    assert strictness_problems(schema) == [
        "#/properties/n: 'minimum' is not supported in strict mode",
        "#/properties/n: 'multipleOf' is not supported in strict mode",
    ]
    assert strictness_problems(schema, keywords=False) == []


def test_an_open_dict_field_is_refused_rather_than_narrowed_to_empty() -> None:
    with pytest.raises(SchemaError, match="labels: additionalProperties must be false"):
        strict_schema(OpenDict)


def test_a_field_without_a_type_is_refused() -> None:
    with pytest.raises(SchemaError, match="Untyped is not strict-compatible"):
        strict_schema(Untyped)


def test_boolean_subschemas_are_walked_without_error() -> None:
    assert strictness_problems({"anyOf": [True, {"type": "null"}]}) == []


def test_a_transform_that_left_an_unsupported_keyword_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def leaky(schema: dict[str, Any]) -> dict[str, Any]:
        return {**schema, "minimum": 1}

    monkeypatch.setattr("msa.llm.schema.anthropic.transform_schema", leaky)
    with pytest.raises(SchemaError, match="'minimum' is not supported in strict mode"):
        strict_schema(Review)

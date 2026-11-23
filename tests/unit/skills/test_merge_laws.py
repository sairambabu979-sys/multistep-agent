"""M5's exit criterion: the layer merge is a lawful, right-biased union (PLAN §5.3)."""

from __future__ import annotations

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from msa.skills.preferences.resolve import Patches, merge

IDS = st.sampled_from(["a.one", "a.two", "b.three", "c.four"])
FIELDS = st.sampled_from(["rule", "enforcement", "rationale", "scope", "check"])
VALUES = st.one_of(st.sampled_from(["hard", "soft", "info", "off"]), st.text(max_size=5), st.none())
PATCHES: st.SearchStrategy[Patches] = st.dictionaries(
    IDS, st.dictionaries(FIELDS, VALUES, max_size=4), max_size=4
)


@given(PATCHES, PATCHES, PATCHES)
def test_merge_is_associative(a: Patches, b: Patches, c: Patches) -> None:
    assert merge(merge(a, b), c) == merge(a, merge(b, c))


@given(PATCHES)
def test_the_empty_layer_is_the_identity(a: Patches) -> None:
    assert merge({}, a) == a
    assert merge(a, {}) == a


@given(PATCHES)
def test_merge_is_idempotent(a: Patches) -> None:
    assert merge(a, a) == a


@given(PATCHES, PATCHES)
def test_the_higher_layer_wins_field_by_field_and_lower_fields_are_kept(
    lower: Patches, higher: Patches
) -> None:
    merged = merge(lower, higher)
    assert set(merged) == set(lower) | set(higher)
    for key, fields in merged.items():
        for name, value in fields.items():
            expected: Any = higher.get(key, {}).get(name, lower.get(key, {}).get(name))
            assert value == expected
        assert set(fields) == set(lower.get(key, {})) | set(higher.get(key, {}))


@given(PATCHES, PATCHES)
def test_merge_does_not_mutate_its_inputs(a: Patches, b: Patches) -> None:
    before = (repr(a), repr(b))
    merge(a, b)
    assert (repr(a), repr(b)) == before

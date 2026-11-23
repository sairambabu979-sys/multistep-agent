"""Shared glob semantics, and the soundness of the overlap test conflicts rely on."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from msa import globs

SEGMENT = st.text(alphabet="ab.", min_size=1, max_size=3)
GLOB_SEGMENT = st.one_of(
    SEGMENT, st.just("*"), st.just("**"), st.just("a*"), st.just("*.b"), st.just("?")
)
GLOBS = st.lists(GLOB_SEGMENT, min_size=1, max_size=4).map("/".join)
PATHS = st.lists(SEGMENT, min_size=1, max_size=5).map("/".join)


@given(GLOBS, GLOBS, PATHS)
def test_may_overlap_never_misses_a_shared_path(first: str, second: str, path: str) -> None:
    """Soundness: if one path matches both globs, they are reported as overlapping."""
    if globs.matches(first, path) and globs.matches(second, path):
        assert globs.may_overlap(first, second), (first, second, path)


@pytest.mark.parametrize(
    ("first", "second", "overlap"),
    [
        ("src/**", "tests/**", False),
        ("src/**", "src/api/**", True),
        ("**/*.py", "**/*.md", False),
        ("**/*.py", "src/x.py", True),
        ("**/x.py", "x.py", True),  # a leading **/ may match nothing
        ("src/*.py", "src/*.pyi", False),
        ("**", "anything", True),
        ("a.py", "b.py", False),
    ],
)
def test_may_overlap_examples(first: str, second: str, overlap: bool) -> None:
    assert globs.may_overlap(first, second) is overlap
    assert globs.may_overlap(second, first) is overlap


def test_validate_refuses_globs_that_cannot_name_a_relative_path() -> None:
    assert globs.validate("src/**") == "src/**"
    for bad in ("/abs", "a//b", "../x", "a/./b", "a\x00b"):
        with pytest.raises(ValueError, match="use a relative path glob"):
            globs.validate(bad)

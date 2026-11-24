import pytest

from textkit import wrap


def test_nothing_to_wrap() -> None:
    assert wrap("", 10) == []


def test_width_must_be_positive() -> None:
    with pytest.raises(ValueError):
        wrap("a", 0)

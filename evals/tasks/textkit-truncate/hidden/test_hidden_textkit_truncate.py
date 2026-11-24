import pytest
from textkit import truncate


def test_text_that_fits_is_unchanged():
    assert truncate("short text", 10) == "short text"


def test_whole_words_then_the_ellipsis():
    assert truncate("the quick brown fox", 15) == "the quick…"
    assert truncate("the quick brown fox", 16) == "the quick brown…"


def test_a_first_word_too_long_is_cut():
    assert truncate("supercalifragilistic", 8) == "superca…"


def test_another_ellipsis():
    assert truncate("the quick brown fox", 12, ellipsis="...") == "the quick..."


def test_the_width_must_exceed_the_ellipsis():
    with pytest.raises(ValueError):
        truncate("abc", 1)

from textkit import norm, title_case


def test_norm_collapses_whitespace() -> None:
    assert norm("  a \t b\n\nc  ") == "a b c"


def test_title_case_keeps_small_words_small() -> None:
    assert title_case("the lord of the rings") == "The Lord of the Rings"
    assert title_case("A TALE OF TWO CITIES") == "A Tale of Two Cities"

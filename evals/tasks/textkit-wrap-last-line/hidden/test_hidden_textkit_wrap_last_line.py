from textkit import wrap


def test_the_last_line_is_kept():
    assert wrap("the quick brown fox", 10) == ["the quick", "brown fox"]
    assert wrap("hello", 10) == ["hello"]


def test_a_long_word_gets_a_line_of_its_own():
    assert wrap("a supercalifragilistic word", 8) == ["a", "supercalifragilistic", "word"]


def test_a_line_may_fill_the_width():
    assert wrap("aaaa bbbb", 9) == ["aaaa bbbb"]
    assert wrap("aaaa bbbb cc", 9) == ["aaaa bbbb", "cc"]


def test_only_whitespace():
    assert wrap("   \n ", 5) == []

from textkit import title_case


def test_the_last_word_is_capitalised():
    assert title_case("what are you up to") == "What Are You Up To"
    assert title_case("something to believe in") == "Something to Believe In"
    assert title_case("of") == "Of"


def test_small_words_in_the_middle_stay_small():
    assert title_case("the lord of the rings") == "The Lord of the Rings"
    assert title_case("a tale of two cities") == "A Tale of Two Cities"

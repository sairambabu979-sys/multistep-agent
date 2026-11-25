from textkit import word_frequencies

TEXT = "The cat and the hat. THE END, and that's that!"


def test_counts():
    assert word_frequencies(TEXT) == [
        ("the", 3),
        ("and", 2),
        ("cat", 1),
        ("end", 1),
        ("hat", 1),
        ("that", 1),
        ("that's", 1),
    ]


def test_top():
    assert word_frequencies(TEXT, top=2) == [("the", 3), ("and", 2)]


def test_no_words():
    assert word_frequencies("  ...  ") == []


def test_digits():
    assert word_frequencies("r2d2 R2D2 c3po") == [("r2d2", 2), ("c3po", 1)]

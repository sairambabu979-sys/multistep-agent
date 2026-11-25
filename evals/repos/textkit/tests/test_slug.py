from textkit import slugify


def test_words_are_joined_by_the_separator() -> None:
    assert slugify("Hello World") == "hello-world"
    assert slugify("Hello World", separator="_") == "hello_world"


def test_accents_are_dropped() -> None:
    assert slugify("Crème brûlée") == "creme-brulee"

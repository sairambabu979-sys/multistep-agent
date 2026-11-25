import pytest
from textkit import slugify


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("Hello, World!", "hello-world"),
        ("  -Already slugged- ", "already-slugged"),
        ("--a--b--", "a-b"),
        ("!!!", ""),
        ("", ""),
        ("Crème brûlée!", "creme-brulee"),
        ("v2.0 release", "v2-0-release"),
    ],
)
def test_slugs(text, slug):
    assert slugify(text) == slug


def test_any_separator():
    assert slugify("...Hello, World!", separator="_") == "hello_world"

import ast
import pathlib
import warnings

import pytest
import textkit
import textkit.utils
from textkit import normalize_whitespace, slugify, title_case


def test_the_new_name():
    assert normalize_whitespace("  a \t b ") == "a b"


@pytest.mark.parametrize("norm", [textkit.norm, textkit.utils.norm])
def test_the_old_name_still_works_and_warns(norm):
    with pytest.warns(DeprecationWarning, match="normalize_whitespace"):
        assert norm("  a \t b ") == "a b"


def test_nothing_inside_calls_the_old_name():
    for path in pathlib.Path(textkit.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "norm"
        ]
        assert not calls, f"{path.name} still calls norm()"


def test_behaviour_is_unchanged():
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # nothing inside may use the deprecated name
        assert title_case("the  lord of the rings") == "The Lord of the Rings"
        assert slugify("Hello  World") == "hello-world"

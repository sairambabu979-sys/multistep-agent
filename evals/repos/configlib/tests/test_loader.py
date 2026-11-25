import pytest

from configlib import coerce, layered, load

BASE = """
# defaults
[server]
host = "localhost"
port = 8080
debug = false
"""


def test_values_are_coerced() -> None:
    assert [coerce(v) for v in ("true", "42", "1.5", "'x y'", "plain")] == [
        True,
        42,
        1.5,
        "x y",
        "plain",
    ]


def test_load() -> None:
    assert load(BASE) == {"server": {"host": "localhost", "port": 8080, "debug": False}}


def test_a_key_outside_a_section_is_an_error() -> None:
    with pytest.raises(ValueError, match="line 1"):
        load("port = 1")


def test_later_files_override_earlier_ones_key_by_key() -> None:
    assert layered(BASE, "[server]\nport = 9090")["server"] == {
        "host": "localhost",
        "port": 9090,
        "debug": False,
    }

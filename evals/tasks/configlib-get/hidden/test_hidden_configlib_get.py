import pytest
from configlib import get

CONFIG = {"db": {"port": 5432, "host": "localhost"}}


def test_found():
    assert get(CONFIG, "db.port") == 5432


def test_a_default_for_what_is_missing():
    assert get(CONFIG, "db.user", "admin") == "admin"
    assert get(CONFIG, "cache.size", None) is None


def test_missing_without_a_default_names_the_path():
    with pytest.raises(KeyError, match="cache.size"):
        get(CONFIG, "cache.size")
    with pytest.raises(KeyError, match="db.user"):
        get(CONFIG, "db.user")


@pytest.mark.parametrize("path", ["db", "db.port.x", ""])
def test_a_path_needs_exactly_one_dot(path):
    with pytest.raises(ValueError):
        get(CONFIG, path)

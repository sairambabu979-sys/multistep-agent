from datetime import timedelta

import pytest

from schedule import format_duration, parse_duration


def test_parse() -> None:
    assert parse_duration("1h30m") == timedelta(minutes=90)
    assert parse_duration("45s") == timedelta(seconds=45)


def test_not_a_duration() -> None:
    for text in ("", "1x", "30m1h"):
        with pytest.raises(ValueError):
            parse_duration(text)


def test_format_round_trips() -> None:
    for text in ("1h30m", "45s", "2h", "1h1s"):
        assert format_duration(parse_duration(text)) == text
    assert format_duration(timedelta(0)) == "0s"

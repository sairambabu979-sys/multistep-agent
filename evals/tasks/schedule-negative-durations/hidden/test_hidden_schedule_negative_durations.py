from datetime import timedelta

from schedule import format_duration


def test_negative_durations():
    assert format_duration(timedelta(seconds=-90)) == "-1m30s"
    assert format_duration(timedelta(hours=-2)) == "-2h"
    assert format_duration(-timedelta(hours=1, seconds=1)) == "-1h1s"


def test_the_rest_is_unchanged():
    assert format_duration(timedelta(minutes=90)) == "1h30m"
    assert format_duration(timedelta(0)) == "0s"

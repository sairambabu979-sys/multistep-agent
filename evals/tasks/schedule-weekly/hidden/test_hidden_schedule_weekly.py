from datetime import date

from schedule import MONDAY, WEDNESDAY, weekly


def test_every_monday_and_wednesday():
    assert weekly(date(2026, 1, 1), date(2026, 1, 14), {MONDAY, WEDNESDAY}) == [
        date(2026, 1, 5),
        date(2026, 1, 7),
        date(2026, 1, 12),
        date(2026, 1, 14),
    ]


def test_both_ends_are_included():
    assert weekly(date(2026, 1, 5), date(2026, 1, 5), [MONDAY]) == [date(2026, 1, 5)]


def test_nothing_to_give():
    assert weekly(date(2026, 1, 10), date(2026, 1, 1), {MONDAY}) == []
    assert weekly(date(2026, 1, 1), date(2026, 1, 31), set()) == []

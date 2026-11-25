from datetime import date
from enum import IntEnum

import schedule.days
from schedule import FRIDAY, MONDAY, Weekday, add_business_days, is_weekend


def test_weekday_is_an_int_enum():
    assert issubclass(Weekday, IntEnum)
    assert [m.name for m in Weekday] == [
        "MONDAY",
        "TUESDAY",
        "WEDNESDAY",
        "THURSDAY",
        "FRIDAY",
        "SATURDAY",
        "SUNDAY",
    ]
    assert [int(m) for m in Weekday] == list(range(7))


def test_the_old_names_are_the_members():
    assert MONDAY is Weekday.MONDAY
    assert schedule.days.SUNDAY is Weekday.SUNDAY
    assert date(2026, 1, 2).weekday() == FRIDAY


def test_behaviour_is_unchanged():
    assert is_weekend(date(2026, 1, 3))
    assert not is_weekend(date(2026, 1, 2))
    assert add_business_days(date(2026, 1, 2), 1) == date(2026, 1, 5)

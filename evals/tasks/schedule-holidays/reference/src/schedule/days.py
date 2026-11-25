"""Weekdays and business days, Monday to Friday."""

from collections.abc import Collection
from datetime import date, timedelta

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)


def is_weekend(day: date) -> bool:
    return day.weekday() in (SATURDAY, SUNDAY)


def is_business_day(day: date, holidays: Collection[date] = ()) -> bool:
    return not is_weekend(day) and day not in holidays


def add_business_days(day: date, count: int, holidays: Collection[date] = ()) -> date:
    """The date ``count`` business days after ``day``, or before it for a negative count.
    A count of 0 gives ``day`` itself."""
    step = 1
    remaining = abs(count)
    current = day
    while remaining:
        current += timedelta(days=step)
        if is_business_day(current, holidays):
            remaining -= 1
    return current

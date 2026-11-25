"""Weekdays and business days, Monday to Friday."""

from datetime import date, timedelta
from enum import IntEnum


class Weekday(IntEnum):
    """Numbered as date.weekday() numbers them."""

    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6


MONDAY = Weekday.MONDAY
TUESDAY = Weekday.TUESDAY
WEDNESDAY = Weekday.WEDNESDAY
THURSDAY = Weekday.THURSDAY
FRIDAY = Weekday.FRIDAY
SATURDAY = Weekday.SATURDAY
SUNDAY = Weekday.SUNDAY


def is_weekend(day: date) -> bool:
    return day.weekday() in (SATURDAY, SUNDAY)


def add_business_days(day: date, count: int) -> date:
    """The date ``count`` business days after ``day``, or before it for a negative count.
    A count of 0 gives ``day`` itself."""
    step = 1
    remaining = abs(count)
    current = day
    while remaining:
        current += timedelta(days=step)
        if not is_weekend(current):
            remaining -= 1
    return current

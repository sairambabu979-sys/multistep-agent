"""Weekdays and business days, Monday to Friday."""

from datetime import date, timedelta

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)


def is_weekend(day: date) -> bool:
    return day.weekday() in (SATURDAY, SUNDAY)


def add_business_days(day: date, count: int) -> date:
    """The date ``count`` business days after ``day``, or before it for a negative count.
    A count of 0 gives ``day`` itself."""
    step = 1 if count >= 0 else -1
    remaining = abs(count)
    current = day
    while remaining:
        current += timedelta(days=step)
        if not is_weekend(current):
            remaining -= 1
    return current

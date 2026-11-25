"""Dates that repeat."""

from collections.abc import Collection
from datetime import date, timedelta


def weekly(start: date, until: date, weekdays: Collection[int]) -> list[date]:
    """Every date from ``start`` to ``until`` inclusive that falls on one of ``weekdays``."""
    days = []
    current = start
    while current <= until:
        if current.weekday() in weekdays:
            days.append(current)
        current += timedelta(days=1)
    return days

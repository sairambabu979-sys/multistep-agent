from datetime import date

from schedule import add_business_days, is_business_day

NEW_YEAR = date(2026, 1, 1)  # a Thursday


def test_is_business_day():
    assert is_business_day(date(2026, 1, 2))
    assert not is_business_day(date(2026, 1, 3))
    assert not is_business_day(NEW_YEAR, holidays={NEW_YEAR})
    assert is_business_day(NEW_YEAR)


def test_holidays_are_skipped():
    assert add_business_days(date(2025, 12, 31), 1, holidays={NEW_YEAR}) == date(2026, 1, 2)
    two = [NEW_YEAR, date(2026, 1, 2)]
    assert add_business_days(date(2025, 12, 31), 2, holidays=two) == date(2026, 1, 6)


def test_without_holidays_nothing_changes():
    assert add_business_days(date(2025, 12, 31), 1) == NEW_YEAR

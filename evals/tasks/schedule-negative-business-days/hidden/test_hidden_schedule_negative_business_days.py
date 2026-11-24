from datetime import date

from schedule import add_business_days

MONDAY_JAN_5 = date(2026, 1, 5)


def test_a_negative_count_goes_back():
    assert add_business_days(MONDAY_JAN_5, -1) == date(2026, 1, 2)
    assert add_business_days(MONDAY_JAN_5, -5) == date(2025, 12, 29)
    assert add_business_days(date(2026, 1, 4), -1) == date(2026, 1, 2)  # from a Sunday


def test_a_positive_count_is_unchanged():
    assert add_business_days(date(2026, 1, 2), 1) == MONDAY_JAN_5


def test_forward_then_back_is_where_you_started():
    for count in range(1, 12):
        assert add_business_days(add_business_days(MONDAY_JAN_5, count), -count) == MONDAY_JAN_5

from datetime import date

from schedule import add_business_days, is_weekend

FRIDAY_JAN_2 = date(2026, 1, 2)


def test_weekends() -> None:
    assert is_weekend(date(2026, 1, 3))
    assert not is_weekend(FRIDAY_JAN_2)


def test_business_days_skip_the_weekend() -> None:
    assert add_business_days(FRIDAY_JAN_2, 1) == date(2026, 1, 5)
    assert add_business_days(FRIDAY_JAN_2, 5) == date(2026, 1, 9)
    assert add_business_days(FRIDAY_JAN_2, 0) == FRIDAY_JAN_2

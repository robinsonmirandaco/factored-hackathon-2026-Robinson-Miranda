"""When a request for information is past its deadline (TRZ-28 CA3)."""

from datetime import date

from app.services.info_requests import is_overdue

DUE = date(2026, 6, 24)


def test_the_customer_has_the_whole_due_day() -> None:
    assert not is_overdue(DUE, date(2026, 6, 23))
    assert not is_overdue(DUE, DUE)
    assert is_overdue(DUE, date(2026, 6, 25))

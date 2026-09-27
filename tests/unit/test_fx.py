"""Currency conversion with the rate of the transaction date (TRZ-14)."""

from datetime import date

import pytest

from app.domain.fx import (
    APPROX_LABEL,
    convert,
    display_amount,
    local_currency,
    to_usd,
)

DAY = date(2026, 6, 10)
# Two rates as they are in daily_exchange_rates for 2026-06-10 (TRZ-14 CA5).
DATASET = {
    (DAY, "USD", "MXN"): 17.000934,
    (DAY, "ARS", "USD"): 0.002803,
}


def test_uses_the_rate_of_the_transaction_date() -> None:
    c = convert(100.0, "USD", "MXN", DAY, DATASET)

    assert c is not None
    assert (c.amount, c.currency, c.rate, c.rate_date) == (1700.09, "MXN", 17.000934, DAY)


def test_same_currency_needs_no_rate() -> None:
    c = convert(42.5, "COP", "COP", DAY, {})

    assert c is not None and (c.amount, c.rate) == (42.5, 1.0)


@pytest.mark.parametrize("back", [1, 2, 3])
def test_a_day_without_rate_takes_the_closest_earlier_day(back: int) -> None:
    rates = {(date(2026, 6, 10 - back), "USD", "MXN"): 17.0, (date(2026, 6, 5), "USD", "MXN"): 1.0}

    c = convert(10.0, "USD", "MXN", DAY, rates)

    assert c is not None and c.amount == 170.0 and c.rate_date == date(2026, 6, 10 - back)


def test_no_rate_in_three_days_is_not_convertible() -> None:
    four_days_before = {(date(2026, 6, 6), "USD", "MXN"): 17.0}
    # A later rate never describes an earlier charge.
    next_day = {(date(2026, 6, 11), "USD", "MXN"): 17.0}

    assert convert(10.0, "USD", "MXN", DAY, four_days_before) is None
    assert convert(10.0, "USD", "MXN", DAY, next_day) is None


def test_policy_amount_is_in_usd() -> None:
    assert to_usd(500_000.0, "ARS", DAY, DATASET) == 1401.5
    assert to_usd(80.0, "USD", DAY, {}) == 80.0
    assert to_usd(500_000.0, "ARS", date(2026, 6, 20), DATASET) is None


def test_display_keeps_the_registered_amount_first_with_a_labelled_equivalent() -> None:
    d = display_amount(100.0, "USD", "MXN", DAY, DATASET)

    assert (d.amount, d.currency) == (100.0, "USD")
    assert (d.converted_amount, d.converted_currency, d.label) == (1700.09, "MXN", APPROX_LABEL)
    assert d.convertible


def test_display_in_local_currency_has_no_secondary_figure() -> None:
    d = display_amount(90_000.0, "COP", "COP", DAY, {})

    assert d.converted_amount is None and d.label is None and d.convertible


def test_display_without_rate_says_it_is_not_convertible() -> None:
    d = display_amount(100.0, "USD", "MXN", date(2026, 6, 20), DATASET)

    assert d.converted_amount is None and d.label is None and not d.convertible
    assert (d.amount, d.currency) == (100.0, "USD")


def test_local_currency_comes_from_the_reference_data() -> None:
    assert [local_currency(c) for c in ("AR", "CO", "MX")] == ["ARS", "COP", "MXN"]

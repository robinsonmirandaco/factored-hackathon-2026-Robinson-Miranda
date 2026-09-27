"""Currency conversion with the rate of the transaction date (design 6.2, 8, 10.2).

Amounts are converted with `daily_exchange_rates` of the day the transaction happened, never of
today: policy thresholds are in USD and the ARS rate moves fast enough that a later rate would
put the same charge in a different band. The rule lives here; where the rates come from
(Postgres in the service, gold files in the evaluation) is the caller's concern.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache
from pathlib import Path

import yaml

# A rate older than this no longer describes the day of the charge (TRZ-14 CA2).
MAX_STALE_DAYS = 3
APPROX_LABEL = "aprox., tasa del día de la transacción"
NORMALIZATION_PATH = Path("config/normalization.yaml")

Rates = Mapping[tuple[date, str, str], float]


@dataclass(frozen=True)
class Conversion:
    """An amount converted with the rate of a given day.

    Attributes:
        amount: Converted amount, rounded to cents.
        currency: Target currency.
        rate: Rate applied.
        rate_date: Day of the rate: the transaction date or up to MAX_STALE_DAYS earlier.
    """

    amount: float
    currency: str
    rate: float
    rate_date: date


@dataclass(frozen=True)
class AmountDisplay:
    """How a charge amount is shown: registered first, converted as a labelled secondary.

    Attributes:
        amount: Registered amount, the primary figure.
        currency: Registered currency.
        converted_amount: Amount in the customer's local currency; None when it is the same
            currency or no rate was found.
        converted_currency: Local currency of the customer.
        label: Label of the converted figure; None when there is no converted figure.
        convertible: False when the currencies differ and no rate was found, so the system
            has to say it cannot show the equivalent.
    """

    amount: float
    currency: str
    converted_amount: float | None
    converted_currency: str
    label: str | None
    convertible: bool


def convert(amount: float, source: str, target: str, on: date, rates: Rates) -> Conversion | None:
    """Converts an amount with the rate of the transaction date.

    When that day has no rate, the closest earlier day within MAX_STALE_DAYS is used.

    Args:
        amount: Amount in the source currency.
        source: ISO code of the source currency.
        target: ISO code of the target currency.
        on: Date of the transaction.
        rates: Rates keyed by (date, source, target).

    Returns:
        The conversion, or None when no rate exists in the allowed days (not convertible).
    """
    if source == target:
        return Conversion(round(amount, 2), target, 1.0, on)
    for back in range(MAX_STALE_DAYS + 1):
        day = on - timedelta(days=back)
        rate = rates.get((day, source, target))
        if rate is not None:
            return Conversion(round(amount * rate, 2), target, rate, day)
    return None


def to_usd(amount: float, currency: str, on: date, rates: Rates) -> float | None:
    """Converts an amount to USD, the currency of the policy thresholds (TRZ-14 CA3).

    Args:
        amount: Amount in its registered currency.
        currency: Registered currency.
        on: Date of the transaction.
        rates: Rates keyed by (date, source, target).

    Returns:
        The USD amount, or None when it is not convertible.
    """
    conversion = convert(amount, currency, "USD", on, rates)
    return None if conversion is None else conversion.amount


def display_amount(
    amount: float, currency: str, local_currency: str, on: date, rates: Rates
) -> AmountDisplay:
    """Builds the display of a charge amount for the customer (TRZ-14 CA4).

    Args:
        amount: Registered amount.
        currency: Registered currency.
        local_currency: Local currency of the customer's country.
        on: Date of the transaction.
        rates: Rates keyed by (date, source, target).

    Returns:
        The registered amount with its approximate local equivalent, when there is one.
    """
    if currency == local_currency:
        return AmountDisplay(amount, currency, None, local_currency, None, True)
    conversion = convert(amount, currency, local_currency, on, rates)
    if conversion is None:
        return AmountDisplay(amount, currency, None, local_currency, None, False)
    return AmountDisplay(amount, currency, conversion.amount, local_currency, APPROX_LABEL, True)


@cache
def local_currency(country_code: str, path: Path = NORMALIZATION_PATH) -> str:
    """Local currency of a home country, from the versioned reference data.

    Args:
        country_code: ISO country code of the customer.
        path: config/normalization.yaml.

    Returns:
        The ISO currency code.

    Raises:
        KeyError: When the country is not in the reference data.
    """
    home = yaml.safe_load(path.read_text())["home_countries"]
    return str(home[country_code]["currency"])

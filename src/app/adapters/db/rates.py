"""Reads exchange rates from the serving database for `app.domain.fx`."""

from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.fx import MAX_STALE_DAYS, Rates

_RATES = text(
    "SELECT date, source_currency, target_currency, exchange_rate FROM exchange_rates "
    "WHERE source_currency = :source AND target_currency = :target "
    "AND date BETWEEN :first AND :last"
)


def rates_near(session: Session, on: date, pairs: set[tuple[str, str]]) -> Rates:
    """Loads the rates `convert` may use for a transaction date.

    Args:
        session: Open database session.
        on: Date of the transaction.
        pairs: (source, target) currency pairs needed; pairs of one currency are skipped.

    Returns:
        Rates of the pairs from MAX_STALE_DAYS before `on` up to `on`, keyed by
        (date, source, target).
    """
    return rates_between(session, on, on, pairs)


def rates_between(session: Session, first: date, last: date, pairs: set[tuple[str, str]]) -> Rates:
    """Loads the rates `convert` may use for any transaction date in a range.

    Args:
        session: Open database session.
        first: Earliest transaction date.
        last: Latest transaction date.
        pairs: (source, target) currency pairs needed; pairs of one currency are skipped.

    Returns:
        Rates of the pairs from MAX_STALE_DAYS before `first` up to `last`, keyed by
        (date, source, target).
    """
    rates: dict[tuple[date, str, str], float] = {}
    for source, target in sorted(pairs):
        if source == target:
            continue
        rows = session.execute(
            _RATES,
            {
                "source": source,
                "target": target,
                "first": first - timedelta(days=MAX_STALE_DAYS),
                "last": last,
            },
        ).all()
        rates.update({(d, s, t): float(r) for d, s, t, r in rows})
    return rates

"""Simulated business clock.

The dataset ends on 2026-06-17, so relative dates, the dispute window and deadlines are computed
against TRAZO_NOW instead of the wall clock; with the real date the 120-day window would hold no
transactions. Session expiry and audit timestamps are security and operations, not bank data, so
they keep using the real clock in `app.core.time.utcnow`.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

# Single-day expressions in Spanish and Portuguese, mapped to how many days back they point.
_DAYS_BACK = {
    "hoy": 0,
    "hoje": 0,
    "ayer": 1,
    "ontem": 1,
    "anteayer": 2,
    "antier": 2,
    "anteontem": 2,
}


@dataclass(frozen=True)
class SimulatedClock:
    """The "now" of the bank data, fixed per service or per evaluation case.

    Attributes:
        now: Naive wall time, read as the local time of the customer's country, like the
            transaction timestamps it is compared with.
    """

    now: datetime

    def __post_init__(self) -> None:
        # Transaction timestamps are naive local time; an aware "now" would fail to compare.
        if self.now.tzinfo is not None:
            raise ValueError("TRAZO_NOW must be a naive local datetime, without a UTC offset")

    def today(self) -> date:
        """Returns the simulated current date.

        Returns:
            The date part of `now`.
        """
        return self.now.date()

    def days_ago(self, days: int) -> datetime:
        """Returns the start of a window that ends at `now`.

        Args:
            days: Window length in calendar days.

        Returns:
            `now` minus the given number of days.
        """
        return self.now - timedelta(days=days)

    def resolve_relative_date(self, expression: str) -> date | None:
        """Resolves a single-day relative expression such as "ayer" or "ontem".

        Args:
            expression: Expression written by the customer, in Spanish or Portuguese.

        Returns:
            The date it refers to, or None when the expression is not a known single-day one.
        """
        days_back = _DAYS_BACK.get(" ".join(expression.lower().split()))
        if days_back is None:
            return None
        return self.today() - timedelta(days=days_back)

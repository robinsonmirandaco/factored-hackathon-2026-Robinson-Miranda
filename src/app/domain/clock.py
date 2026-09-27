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

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_EXACT_DAYS = {"today": 0, "yesterday": 1, "day_before_yesterday": 2}


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

    def relative_window(self, key: str, count: int = 1) -> tuple[int, int] | None:
        """Resolves a language-neutral relative expression to a window of days back.

        Customers are vague about dates, so every window covers each day the expression can
        honestly mean: "el viernes pasado" may be the latest Friday or the one before it, and
        "hace 3 días" is one day either side of three days ago.

        Args:
            key: today, yesterday, day_before_yesterday, a weekday name ("friday"), a weekday
                with "last" ("last_friday"), this_week, last_week, weekend, last_month,
                early_this_month, few_days, recently, days_ago, weeks_ago or months_ago.
            count: How many units back, for days_ago, weeks_ago and months_ago.

        Returns:
            Fewest and most days back from `today()`, both inclusive, or None for an unknown key
            or a window that would end in the future.
        """
        today = self.today()
        weekday = today.weekday()
        if key in _EXACT_DAYS:
            return _EXACT_DAYS[key], _EXACT_DAYS[key]
        day_name = key.removeprefix("last_")
        if day_name in WEEKDAYS:
            # The latest such weekday strictly before today; "last" also admits the week before.
            back = (weekday - WEEKDAYS.index(day_name)) % 7 or 7
            return (back, back + 7) if key.startswith("last_") else (back, back)
        if key == "this_week":
            return 0, weekday
        if key == "last_week":
            return weekday + 1, weekday + 7
        if key == "weekend":
            sunday_back = (weekday - 6) % 7 or 7
            return sunday_back, sunday_back + 1
        if key == "last_month":
            last_of_previous = today.replace(day=1) - timedelta(days=1)
            return today.day, (today - last_of_previous.replace(day=1)).days
        if key == "early_this_month":
            return max(0, today.day - 10), today.day - 1
        if key == "few_days":
            return 2, 7
        if key == "recently":
            return 0, 14
        # A month back is vaguer than a week back, so its slack is wider.
        spans = {"days_ago": (1, 1), "weeks_ago": (7, 3), "months_ago": (30, 10)}
        if key in spans and count > 0:
            unit, slack = spans[key]
            return max(0, unit * count - slack), unit * count + slack
        return None

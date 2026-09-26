"""Business days per country: weekdays that are not bank holidays.

The holiday table lives in config/holidays.yaml with the official source of every date. A
calendar only answers for the years it covers; outside them it raises instead of silently
counting a holiday as a business day.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml


class OutsideCalendarError(ValueError):
    """Raised when a date falls outside the years a holiday calendar covers."""


@dataclass(frozen=True)
class HolidayCalendar:
    """Bank holidays of one country for a closed range of years.

    Attributes:
        country: ISO 3166-1 alpha-2 code.
        first_year: First year the table covers.
        last_year: Last year the table covers.
        holidays: Days banks do not operate, weekends excluded or not.
    """

    country: str
    first_year: int
    last_year: int
    holidays: frozenset[date]

    def is_business_day(self, day: date) -> bool:
        """Tells whether banks operate on a day.

        Args:
            day: Day to check.

        Returns:
            True for a weekday that is not a bank holiday.

        Raises:
            OutsideCalendarError: If the day is outside the covered years.
        """
        if not self.first_year <= day.year <= self.last_year:
            raise OutsideCalendarError(
                f"{self.country} holidays cover {self.first_year}-{self.last_year}, not {day}"
            )
        return day.weekday() < 5 and day not in self.holidays

    def add_business_days(self, start: date, days: int) -> date:
        """Counts business days forward, starting the day after `start`.

        Args:
            start: Day the count starts from; it is not counted itself.
            days: Business days to add; must be positive.

        Returns:
            The day on which the count reaches `days`.

        Raises:
            ValueError: If `days` is not positive.
            OutsideCalendarError: If the count leaves the covered years.
        """
        if days < 1:
            raise ValueError("days must be positive")
        day, counted = start, 0
        while counted < days:
            day += timedelta(days=1)
            if self.is_business_day(day):
                counted += 1
        return day


def load_calendars(path: str | Path) -> dict[str, HolidayCalendar]:
    """Loads the holiday table, one calendar per country.

    Args:
        path: YAML file with `countries.<code>.holidays[].date`.

    Returns:
        Calendars keyed by country code.
    """
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    first, last = raw["years"]
    return {
        code: HolidayCalendar(
            country=code,
            first_year=first,
            last_year=last,
            holidays=frozenset(h["date"] for h in country["holidays"]),
        )
        for code, country in raw["countries"].items()
    }

"""Business days per country and the demo policy deadlines they back."""

import re
from datetime import date
from pathlib import Path

import pytest
import yaml

from app.domain.business_days import HolidayCalendar, OutsideCalendarError, load_calendars
from app.domain.policy_passages import (
    PolicyDeadline,
    Unsupported,
    load_passages,
    policy_deadline,
)

CONFIG = Path(__file__).resolve().parents[2] / "config"
HOLIDAYS = CONFIG / "holidays.yaml"
PASSAGES = CONFIG / "policy_passages.yaml"


@pytest.fixture(scope="module")
def calendars() -> dict[str, HolidayCalendar]:
    return load_calendars(HOLIDAYS)


@pytest.fixture(scope="module")
def passages():
    return load_passages(PASSAGES)


# ---- holiday table ------------------------------------------------------------------------


def test_table_covers_the_three_countries_from_2023_to_2026(calendars):
    assert set(calendars) == {"MX", "CO", "AR"}
    for cal in calendars.values():
        assert (cal.first_year, cal.last_year) == (2023, 2026)
        assert {d.year for d in cal.holidays} == {2023, 2024, 2025, 2026}


def test_every_holiday_cites_a_listed_official_source():
    raw = yaml.safe_load(HOLIDAYS.read_text(encoding="utf-8"))
    for code, country in raw["countries"].items():
        sources = country["sources"]
        assert all(s["url"].startswith("https://") for s in sources.values()), code
        for h in country["holidays"]:
            assert isinstance(h["date"], date), (code, h)
            assert h["name"], (code, h)
            assert h["source"] in sources, (code, h)


def test_no_holiday_is_listed_twice():
    raw = yaml.safe_load(HOLIDAYS.read_text(encoding="utf-8"))
    for code, country in raw["countries"].items():
        dates = [h["date"] for h in country["holidays"]]
        assert len(dates) == len(set(dates)), code


# ---- business days ------------------------------------------------------------------------


def test_15_business_days_from_june_17_2026_in_mexico(calendars):
    assert calendars["MX"].add_business_days(date(2026, 6, 17), 15) == date(2026, 7, 8)


def test_15_business_days_from_may_26_2026_in_mexico(calendars):
    assert calendars["MX"].add_business_days(date(2026, 5, 26), 15) == date(2026, 6, 16)


def test_weekends_are_skipped(calendars):
    # Friday 2026-06-19 plus one business day is Monday 2026-06-22 in Mexico.
    assert calendars["MX"].add_business_days(date(2026, 6, 19), 1) == date(2026, 6, 22)


def test_count_from_a_weekend_starts_on_monday(calendars):
    assert calendars["MX"].add_business_days(date(2026, 6, 20), 1) == date(2026, 6, 22)


@pytest.mark.parametrize(
    ("country", "start", "expected"),
    [
        # Mexico: Wednesday 2026-09-16, Independence Day (CNBV 2026).
        ("MX", date(2026, 9, 15), date(2026, 9, 17)),
        # Colombia: Monday 2026-07-13, the new 9 July holiday moved to Monday (Ley 2578 de 2026).
        ("CO", date(2026, 7, 10), date(2026, 7, 14)),
        # Argentina: Thursday 2026-07-09 and the tourism day on Friday 2026-07-10 (BCRA 2026).
        ("AR", date(2026, 7, 8), date(2026, 7, 13)),
    ],
)
def test_count_skips_a_bank_holiday_of_each_country(calendars, country, start, expected):
    assert calendars[country].add_business_days(start, 1) == expected


def test_same_start_gives_a_different_deadline_per_country(calendars):
    # Colombia loses Monday 2026-06-29 (San Pedro y San Pablo); Mexico and Argentina do not.
    start = date(2026, 6, 17)
    assert calendars["MX"].add_business_days(start, 15) == date(2026, 7, 8)
    assert calendars["AR"].add_business_days(start, 15) == date(2026, 7, 8)
    assert calendars["CO"].add_business_days(start, 15) == date(2026, 7, 9)


def test_argentine_bank_day_is_a_holiday_every_year(calendars):
    # The bank workers' agreement fixes 6 November with no move, so every year follows one rule.
    for year in (2023, 2024, 2025, 2026):
        assert not calendars["AR"].is_business_day(date(year, 11, 6))


def test_unconfirmed_closures_are_flagged_and_not_counted(calendars):
    raw = yaml.safe_load(HOLIDAYS.read_text(encoding="utf-8"))
    for code, country in raw["countries"].items():
        for u in country.get("unconfirmed", []):
            assert u["reason"], (code, u)
            assert u["source"] in country["sources"], (code, u)
            assert u["date"] not in calendars[code].holidays, (code, u)
    # Colombia's year-end closing has no official source, so 2025-12-31 still counts.
    assert calendars["CO"].is_business_day(date(2025, 12, 31))


def test_dates_outside_the_table_raise(calendars):
    with pytest.raises(OutsideCalendarError):
        calendars["MX"].add_business_days(date(2026, 12, 20), 15)
    with pytest.raises(OutsideCalendarError):
        calendars["CO"].is_business_day(date(2022, 12, 30))


def test_non_positive_days_are_rejected(calendars):
    with pytest.raises(ValueError):
        calendars["AR"].add_business_days(date(2026, 6, 17), 0)


# ---- demo policy passages -----------------------------------------------------------------


def test_every_passage_has_an_id_and_spanish_and_portuguese_text(passages):
    assert passages
    for p in passages.values():
        assert re.fullmatch(r"§\d+\.\d+", p.id), p.id
        assert set(p.text) == {"es", "pt"} and all(p.text.values()), p.id


def test_every_passage_is_labelled_as_demo_policy(passages):
    for p in passages.values():
        assert "política de demostración" in p.label["es"]
        assert "política de demonstração" in p.label["pt"]


def test_response_deadline_is_backed_by_its_passage(passages, calendars):
    d = policy_deadline(passages, calendars, "response_deadline", date(2026, 6, 17), "MX", "es")
    assert isinstance(d, PolicyDeadline)
    assert (d.due, d.passage_id) == (date(2026, 7, 8), "§2.1")
    assert "15 días hábiles" in d.text
    assert "política de demostración" in d.label


def test_response_deadline_in_portuguese_cites_the_same_passage(passages, calendars):
    d = policy_deadline(passages, calendars, "response_deadline", date(2026, 6, 17), "MX", "pt")
    assert isinstance(d, PolicyDeadline)
    assert d.passage_id == "§2.1"
    assert "15 dias úteis" in d.text


def test_dispute_window_counts_calendar_days(passages, calendars):
    d = policy_deadline(passages, calendars, "dispute_window", date(2026, 2, 17), "AR", "es")
    assert isinstance(d, PolicyDeadline)
    assert (d.due, d.passage_id) == (date(2026, 6, 17), "§1.1")


@pytest.mark.parametrize(
    ("rule", "start", "country", "language"),
    [
        ("refund_deadline", date(2026, 6, 17), "MX", "es"),  # no passage
        ("card_block", date(2026, 6, 17), "MX", "es"),  # passage without a deadline
        ("response_deadline", date(2026, 6, 17), "BR", "pt"),  # no holiday table
        ("response_deadline", date(2026, 12, 20), "CO", "es"),  # past the table
        ("response_deadline", date(2026, 6, 17), "MX", "en"),  # no text in that language
    ],
)
def test_without_backing_the_answer_is_unsupported(
    passages, calendars, rule, start, country, language
):
    result = policy_deadline(passages, calendars, rule, start, country, language)
    assert isinstance(result, Unsupported)
    assert result.reason

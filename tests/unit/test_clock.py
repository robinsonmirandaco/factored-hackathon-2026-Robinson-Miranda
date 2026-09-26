"""The simulated clock anchors relative dates and data windows to TRAZO_NOW."""

from datetime import UTC, date, datetime

import pytest

from app.core.config import Settings
from app.domain.clock import SimulatedClock

TRAZO_NOW = datetime(2026, 6, 17, 23, 59)


def test_default_trazo_now_is_the_last_day_of_the_dataset(monkeypatch):
    monkeypatch.delenv("TRAZO_NOW", raising=False)
    settings = Settings(database_url="postgresql+psycopg://unused", _env_file=None)
    assert settings.trazo_now == TRAZO_NOW


def test_trazo_now_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("TRAZO_NOW", "2025-01-10T09:30:00")
    settings = Settings(database_url="postgresql+psycopg://unused", _env_file=None)
    assert SimulatedClock(settings.trazo_now).today() == date(2025, 1, 10)


def test_aware_now_is_rejected():
    # Transaction timestamps are naive local time; mixing in an offset would break comparisons.
    with pytest.raises(ValueError):
        SimulatedClock(datetime(2026, 6, 17, 23, 59, tzinfo=UTC))


def test_yesterday_resolves_to_june_16():
    assert SimulatedClock(TRAZO_NOW).resolve_relative_date("ayer") == date(2026, 6, 16)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("hoy", date(2026, 6, 17)),
        ("hoje", date(2026, 6, 17)),
        ("Ontem", date(2026, 6, 16)),
        ("  AYER ", date(2026, 6, 16)),
        ("anteayer", date(2026, 6, 15)),
        ("antier", date(2026, 6, 15)),
        ("anteontem", date(2026, 6, 15)),
    ],
)
def test_single_day_expressions_in_spanish_and_portuguese(expression, expected):
    assert SimulatedClock(TRAZO_NOW).resolve_relative_date(expression) == expected


def test_unknown_expression_is_not_guessed():
    assert SimulatedClock(TRAZO_NOW).resolve_relative_date("la semana pasada") is None


def test_window_start_counts_back_from_simulated_now():
    assert SimulatedClock(TRAZO_NOW).days_ago(120) == datetime(2026, 2, 17, 23, 59)


def test_each_case_can_fix_its_own_now():
    # Evaluation cases are spread over the dataset period, each with the moment it was written.
    early = SimulatedClock(datetime(2024, 3, 1, 8, 0))
    late = SimulatedClock(TRAZO_NOW)
    assert early.resolve_relative_date("ayer") == date(2024, 2, 29)
    assert late.resolve_relative_date("ayer") == date(2026, 6, 16)

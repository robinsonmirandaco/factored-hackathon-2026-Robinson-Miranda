"""Retention of redacted conversations (TRZ-41): the cutoff on the real clock, and the marker the
database writes, which the screens compare against."""

from datetime import datetime
from pathlib import Path

from app.domain.retention import PURGED_TEXT, cutoff, shown

MIGRATION = Path(__file__).resolve().parents[2] / "db" / "migrations" / "0022_retention.sql"


def test_the_cutoff_is_the_retention_before_the_run() -> None:
    assert cutoff(datetime(2026, 10, 3, 12, 0), 90) == datetime(2026, 7, 5, 12, 0)


def test_the_marker_is_the_one_the_database_writes() -> None:
    sql = MIGRATION.read_text()
    assert sql.count(f"'{PURGED_TEXT}'") == 2


def test_the_marker_is_shown_in_the_language_of_the_screen() -> None:
    assert shown(PURGED_TEXT, "es") == "[eliminado por retención]"
    assert shown(PURGED_TEXT, "pt") == "[removido por retenção]"
    # Any other text, and a missing one, is shown as it is.
    assert shown("No compré nada", "pt") == "No compré nada"
    assert shown(None, "pt") is None

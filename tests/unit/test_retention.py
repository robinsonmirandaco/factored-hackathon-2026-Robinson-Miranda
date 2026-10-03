"""Retention of redacted conversations (TRZ-41): the cutoff on the real clock, and the marker the
database writes, which the screens compare against."""

from datetime import datetime
from pathlib import Path

from app.domain.retention import PURGED_TEXT, cutoff

MIGRATION = Path(__file__).resolve().parents[2] / "db" / "migrations" / "0022_retention.sql"


def test_the_cutoff_is_the_retention_before_the_run() -> None:
    assert cutoff(datetime(2026, 10, 3, 12, 0), 90) == datetime(2026, 7, 5, 12, 0)


def test_the_marker_is_the_one_the_database_writes() -> None:
    sql = MIGRATION.read_text()
    assert sql.count(f"'{PURGED_TEXT}'") == 2

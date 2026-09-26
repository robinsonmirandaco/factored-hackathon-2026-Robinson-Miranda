"""The simulated clock drives data windows; the real clock drives audit timestamps."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.adapters.db.models import AuditRecord
from app.adapters.db.session import Database
from app.cli.eval import run
from app.domain.clock import SimulatedClock
from app.services.ingestion import ingest_rows
from app.services.tools import lookup_transaction

CASES = Path(__file__).resolve().parents[2] / "eval" / "cases"
TRAZO_NOW = datetime(2026, 6, 17, 23, 59)

pytestmark = pytest.mark.integration


@pytest.fixture
def db(database_url: str):
    database = Database(database_url)
    database.create_all()
    with database.session() as s:
        ingest_rows(
            s,
            "test",
            [{"id": "C1"}],
            [
                {
                    "id": "TX1",
                    "customer_id": "C1",
                    "amount": 120.0,
                    "merchant": "Oxxo",
                    "timestamp": datetime(2026, 6, 10, 14, 0),
                    "status": "approved",
                }
            ],
            [],
        )
    yield database
    database.dispose()


def test_lookup_window_ends_at_simulated_now(db):
    with db.session() as s:
        found = lookup_transaction(s, SimulatedClock(TRAZO_NOW), "C1", amount=120.0)
        # With the wall clock the June charge would be outside a 14-day window.
        too_late = lookup_transaction(s, SimulatedClock(datetime(2026, 9, 26, 12, 0)), "C1")
        # A case set before the charge must not see it: it had not happened yet.
        too_early = lookup_transaction(s, SimulatedClock(datetime(2026, 6, 9, 12, 0)), "C1")
    assert [m["tx_id"] for m in found.data["matches"]] == ["TX1"]
    assert too_late.data["count"] == 0
    assert too_early.data["count"] == 0


def test_audit_timestamps_use_the_real_clock(db):
    with db.session() as s:
        lookup_transaction(s, SimulatedClock(TRAZO_NOW), "C1", case_id="K1")
    with db.session() as s:
        created = s.execute(select(AuditRecord.created_at)).scalars().all()
    assert created
    # Audit rows are operations, not bank data: they are stamped after the simulated date.
    assert all(c > TRAZO_NOW + timedelta(days=1) for c in created)


def test_eval_case_runs_at_its_own_now(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    name = "05_blocked_over_limit_escalates.yaml"
    case = yaml.safe_load((CASES / name).read_text())
    # Passes only if the fixtures and the lookup window both follow the case clock: with
    # either on TRAZO_NOW, the charge falls outside the window and the reason changes.
    case["now"] = "2024-03-01T10:00:00"
    d = tmp_path / "cases"
    d.mkdir()
    (d / name).write_text(yaml.safe_dump(case))

    results, _ = run(d, tmp_path / "reports")
    assert results[0].status == "pass", results[0].turns[0].mismatches

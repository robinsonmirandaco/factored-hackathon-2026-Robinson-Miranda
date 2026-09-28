"""The simulated clock drives data windows; the real clock drives audit timestamps."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.adapters.db.models import AuditRecord
from app.adapters.db.session import Database, SchemaUrls
from app.cli.eval import run
from app.domain.clock import SimulatedClock
from app.services.identification import identify_by_button, load_candidates
from tests.serving_data import card, customer, load, transaction

CASES = Path(__file__).resolve().parents[2] / "eval" / "cases"
TRAZO_NOW = datetime(2026, 6, 17, 23, 59)

pytestmark = pytest.mark.integration


@pytest.fixture
def db(schema: SchemaUrls):
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1")],
        [transaction("TX1", "C1", "P1", datetime(2026, 6, 10, 14, 0), amount=120.0)],
    )
    database = Database(schema.app)
    yield database
    database.dispose()


def test_candidate_window_ends_at_simulated_now(db):
    with db.session(customer_id="C1") as s:
        found = load_candidates(s, "C1", SimulatedClock(TRAZO_NOW), 120)
        # With a clock 132 days after the charge it is outside the 120-day window.
        too_late = load_candidates(s, "C1", SimulatedClock(datetime(2026, 10, 20, 12, 0)), 120)
        # A case set before the charge must not see it: it had not happened yet.
        too_early = load_candidates(s, "C1", SimulatedClock(datetime(2026, 6, 9, 12, 0)), 120)
    assert [c.transaction_id for c in found] == ["TX1"]
    assert too_late == []
    assert too_early == []


def test_audit_timestamps_use_the_real_clock(db):
    with db.session(customer_id="C1") as s:
        identify_by_button(s, SimulatedClock(TRAZO_NOW), "C1", "TX1", 120, case_id="K1")
    with db.session(customer_id="C1") as s:
        created = s.execute(select(AuditRecord.created_at)).scalars().all()
    assert created
    # Audit rows are operations, not bank data: they are stamped after the simulated date.
    assert all(c > TRAZO_NOW + timedelta(days=1) for c in created)


def test_eval_case_runs_at_its_own_now(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    name = "06_amount_1000_01_edge.yaml"
    case = yaml.safe_load((CASES / name).read_text())
    # Passes only if the fixtures and the candidate window both follow the case clock: with
    # either on TRAZO_NOW, the charge falls outside the window and the rule changes.
    case["now"] = "2024-03-01T10:00:00"
    d = tmp_path / "cases"
    d.mkdir()
    (d / name).write_text(yaml.safe_dump(case))

    results, _ = run(d, tmp_path / "reports")
    assert results[0].status == "pass", results[0].turns[0].mismatches

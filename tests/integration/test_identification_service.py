"""Identification on Postgres (TRZ-15 CA1, CA2, CA6, CA9): the session customer's candidates,
the same predicate as the evaluation, and the audit row."""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select, text

from app.adapters.db.models import AuditRecord
from app.adapters.db.session import Database, SchemaUrls
from app.core.config import Settings
from app.domain.clock import SimulatedClock
from app.domain.identification import COMPONENTS, Candidate, Params, is_disputable
from app.schemas.comprehension import Comprehension
from app.services.identification import identify_by_button, identify_charge, load_candidates
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = Settings().trazo_now
PARAMS = Params("identification-test", "rules", dict.fromkeys(COMPONENTS, 0.2), 0.05, 0.8)


@pytest.fixture
def db(schema: SchemaUrls):
    rows = [
        transaction("IN1", "C1", "P1", NOW - timedelta(days=3), merchant_name="MercaYa"),
        transaction("IN2", "C1", "P1", NOW - timedelta(days=40), amount=5000.0),
        transaction("PEND", "C1", "P1", NOW - timedelta(days=1), transaction_status="Pending"),
        transaction("WD", "C1", "P1", NOW - timedelta(days=2), transaction_type="Withdrawal"),
        transaction("DECL", "C1", "P1", NOW - timedelta(days=2), transaction_status="Declined"),
        transaction("OLD", "C1", "P1", NOW - timedelta(days=121)),
        transaction("FUT", "C1", "P1", NOW + timedelta(hours=1)),
        transaction("OTHER", "C2", "P2", NOW - timedelta(days=3), merchant_name="MercaYa"),
    ]
    load(schema.admin, [customer("C1"), customer("C2")], [card("P1", "C1"), card("P2", "C2")], rows)
    owner = create_engine(schema.admin)
    with owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO exchange_rates (date, source_currency, target_currency, "
                "exchange_rate) VALUES (:d, 'USD', 'COP', 4000.0)"
            ),
            {"d": (NOW - timedelta(days=3)).date()},
        )
    owner.dispose()
    database = Database(schema.app)
    yield database
    database.dispose()


def test_candidates_are_the_session_customers_disputable_transactions(db: Database) -> None:
    clock = SimulatedClock(NOW)
    with db.session(customer_id="C1") as s:
        found = load_candidates(s, "C1", clock)
        # Row level security hides another customer's rows even if asked by id.
        foreign = load_candidates(s, "C2", clock)

    assert sorted(c.transaction_id for c in found) == ["IN1", "IN2", "PEND", "WD"]
    assert foreign == []
    # The evaluation filters the gold rows with this predicate; both must agree.
    assert all(is_disputable(c, NOW) for c in found)


def test_the_evaluation_predicate_rejects_what_the_query_leaves_out() -> None:
    def cand(**extra) -> Candidate:
        base = {
            "transaction_id": "X",
            "timestamp": NOW - timedelta(days=2),
            "amount": 1.0,
            "currency": "COP",
            "channel": "POS",
            "merchant_name": None,
            "transaction_type": "Purchase",
            "status": "Approved",
        }
        return Candidate(**{**base, **extra})

    assert not is_disputable(cand(status="Declined"), NOW)
    assert not is_disputable(cand(timestamp=NOW - timedelta(days=121)), NOW)
    assert not is_disputable(cand(timestamp=NOW + timedelta(hours=1)), NOW)


def test_identify_charge_scores_converts_and_audits(db: Database) -> None:
    clues = Comprehension.model_validate(
        {
            "intent": "unrecognized_charge",
            "language": "es-CO",
            # 0.025 USD is 100 COP at the rate of the day of IN1.
            "amount": {"value": 0.025, "currency": "USD", "evidence": "x"},
            "merchant_hint": {"value": "MercaYa", "evidence": "x"},
        }
    )
    with db.session(customer_id="C1") as s:
        result = identify_charge(s, SimulatedClock(NOW), "C1", clues, PARAMS, "COP", "K1")
    with db.session(customer_id="C1") as s:
        row = s.execute(
            select(AuditRecord).where(AuditRecord.action == "identify_transaction")
        ).scalar_one()

    assert result.scored[0].candidate.transaction_id == "IN1"
    assert result.scored[0].components["amount"] == pytest.approx(0.0)
    assert result.conformal_set == ("IN1",) and result.decision == "identified"
    # Rates exist only for the day of IN1; the other days cannot convert the USD amount.
    assert result.amount_not_convertible
    assert row.result["decision"] == "identified"
    assert row.payload["params_version"] == "identification-test"
    assert row.result["top"][0]["components"].keys() == set(COMPONENTS)


def test_the_button_door_checks_ownership_and_window(db: Database) -> None:
    clock = SimulatedClock(NOW)
    with db.session(customer_id="C1") as s:
        mine = identify_by_button(s, clock, "C1", "IN2")
        old = identify_by_button(s, clock, "C1", "OLD")
        other = identify_by_button(s, clock, "C1", "OTHER")

    assert (mine.door, mine.decision, mine.conformal_set) == ("button", "identified", ("IN2",))
    assert old.decision == "not_found" and other.decision == "not_found"

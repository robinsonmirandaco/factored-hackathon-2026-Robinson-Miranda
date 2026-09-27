"""The state-changing tools on the serving schema, run as trazo_app under the customer's context:
blocking cards writes card_blocks (an early part of TRZ-18 CA2) and opening a dispute writes
disputes, both idempotent."""

from datetime import datetime

import pytest
from sqlalchemy import create_engine, text

from app.adapters.db.session import Database, SchemaUrls
from app.services.tools import freeze_card, open_dispute
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

AT = datetime(2026, 6, 10, 12, 0)


@pytest.fixture
def db(schema: SchemaUrls):
    load(
        schema.admin,
        [customer("C1")],
        [
            card("P1", "C1"),
            card("P2", "C1", product_type="savings_account"),
            card("P3", "C1", product_status="Closed"),
        ],
        [transaction("TX1", "C1", "P1", AT, amount=42.5)],
    )
    owner = create_engine(schema.admin)
    with owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, customer_id, intent, trace_id) "
                "VALUES ('K1', 'C1', 'x', 't')"
            )
        )
    owner.dispose()
    database = Database(schema.app)
    yield database
    database.dispose()


def test_freeze_card_blocks_active_cards_once(db: Database) -> None:
    with db.session(customer_id="C1") as s:
        first = freeze_card(s, "C1", "K1", "lost_or_stolen_card")
        replay = freeze_card(s, "C1", "K1", "lost_or_stolen_card")
    with db.session(customer_id="C1") as s:
        statuses = dict(s.execute(text("SELECT product_id, product_status FROM products")).all())
        blocks = s.execute(text("SELECT product_id, status_before FROM card_blocks")).all()

    assert first.ok and first.data["blocked_products"] == ["P1"]
    assert replay.message == "already applied (idempotent)" and replay.data == first.data
    # Only the active card; the account and the closed card are untouched.
    assert statuses == {"P1": "Blocked", "P2": "Active", "P3": "Closed"}
    assert blocks == [("P1", "Active")]


def test_open_dispute_writes_a_row_and_leaves_the_transaction(db: Database) -> None:
    with db.session(customer_id="C1") as s:
        first = open_dispute(s, "TX1", "K1", "unrecognized_charge", "not me")
        open_dispute(s, "TX1", "K1", "unrecognized_charge", "not me")
    with db.session(customer_id="C1") as s:
        disputes = s.execute(
            text("SELECT transaction_id, dispute_type, amount, folio FROM disputes")
        ).all()
        status = s.execute(text("SELECT transaction_status FROM transactions")).scalar_one()

    assert first.ok
    assert disputes == [("TX1", "unrecognized_charge", 42.5, None)]
    assert status == "Approved"

"""The state-changing tools on the serving schema, run as trazo_app under the customer's context
(TRZ-18): the dispute with its folio, business date and deadline (CA1), the block of the card of
the charge only (CA2), replays that write nothing (CA4), ids of another customer (CA5) and
balances left untouched (CA6)."""

import re
from collections.abc import Iterator
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.adapters.db.session import Database, SchemaUrls
from app.core.config import Settings
from app.domain.business_days import load_calendars
from app.domain.clock import SimulatedClock
from app.domain.policy import load_policy
from app.domain.policy_passages import (
    PolicyDeadline,
    Unsupported,
    load_passages,
    policy_deadline,
)
from app.services.tools import OwnershipError, block_card, register_dispute
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

AT = datetime(2026, 6, 10, 12, 0)
NOW = datetime(2026, 6, 17, 23, 59)
CLOCK = SimulatedClock(NOW)
SETTINGS = Settings()
PASSAGES = load_passages(
    SETTINGS.policy_passages_path, load_policy(SETTINGS.policy_path).dispute_window_days
)
CALENDARS = load_calendars(SETTINGS.holidays_path)


def deadline(start: date) -> PolicyDeadline | Unsupported:
    return policy_deadline(PASSAGES, CALENDARS, "response_deadline", start, "CO", "es")


def unsupported(start: date) -> PolicyDeadline | Unsupported:
    return Unsupported("no passage for rule 'response_deadline'")


@pytest.fixture
def schema_c1(schema: SchemaUrls) -> SchemaUrls:
    load(
        schema.admin,
        [customer("C1"), customer("C2")],
        [
            card("P1", "C1", current_balance=900.0, credit_limit=2500.0),
            card("P1B", "C1", product_type="debit_card", current_balance=300.0),
            card("P2", "C1", product_type="savings_account", current_balance=5000.0),
            card("P3", "C1", product_status="Closed"),
            card("P9", "C2"),
        ],
        [
            transaction("TX1", "C1", "P1", AT, amount=42.5),
            transaction("TXS", "C1", "P2", AT, amount=80.0, transaction_type="Withdrawal"),
            transaction("TXC", "C1", "P3", AT, amount=15.0),
            transaction("TX9", "C2", "P9", AT, amount=10.0),
        ],
    )
    owner = create_engine(schema.admin)
    with owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, customer_id, intent, trace_id) "
                "VALUES ('K1', 'C1', 'x', 't'), ('K2', 'C1', 'x', 't')"
            )
        )
    owner.dispose()
    return schema


@pytest.fixture
def db(schema_c1: SchemaUrls) -> Iterator[Database]:
    database = Database(schema_c1.app)
    yield database
    database.dispose()


def _rows(s: Session, sql: str) -> list[tuple]:
    return [tuple(r) for r in s.execute(text(sql))]


def _register(s: Session, case_id: str = "K1", tx: str = "TX1", **kw: object):
    return register_dispute(
        s, CLOCK, kw.get("deadline", deadline), "C1", case_id, tx, "unrecognized_charge"
    )


# ---- CA1: folio, transaction, type, amount and deadline ----------------------------------


def test_a_dispute_gets_a_folio_its_business_date_and_the_backed_deadline(db: Database) -> None:
    with db.session(customer_id="C1") as s:
        first = _register(s)
    with db.session(customer_id="C1") as s:
        rows = _rows(
            s,
            "SELECT folio, transaction_id, dispute_type, amount, currency, business_at, due_date "
            "FROM disputes",
        )
        status = s.execute(
            text("SELECT transaction_status FROM transactions WHERE transaction_id = 'TX1'")
        ).scalar_one()

    expected_due = deadline(NOW.date())
    assert isinstance(expected_due, PolicyDeadline)
    assert first.ok and re.fullmatch(r"DSP-2026-\d{5}", first.data["folio"])
    assert rows == [
        (first.data["folio"], "TX1", "unrecognized_charge", 42.5, "COP", NOW, expected_due.due)
    ]
    assert first.data["due_date_passage"] == "§2.1"
    assert status == "Approved"


def test_folios_are_consecutive(db: Database) -> None:
    with db.session(customer_id="C1") as s:
        # Two charges: one charge takes one opened dispute (migration 0014).
        a = _register(s, "K1", "TX1").data["folio"]
        b = _register(s, "K2", "TXS").data["folio"]
    assert int(b[-5:]) == int(a[-5:]) + 1


def test_without_a_backing_passage_the_deadline_is_left_empty_with_the_reason(
    db: Database,
) -> None:
    with db.session(customer_id="C1") as s:
        res = _register(s, deadline=unsupported)
    with db.session(customer_id="C1") as s:
        due = s.execute(text("SELECT due_date FROM disputes")).scalar_one()
    assert due is None
    assert res.data["due_date"] is None
    assert res.data["due_date_unsupported"] == "no passage for rule 'response_deadline'"


# ---- CA2: only the card of the charge ------------------------------------------------------


def test_block_card_blocks_only_the_card_of_the_charge(db: Database) -> None:
    with db.session(customer_id="C1") as s:
        res = block_card(s, "C1", "K1", "TX1", "unrecognized_charge")
    with db.session(customer_id="C1") as s:
        statuses = dict(_rows(s, "SELECT product_id, product_status FROM products"))
        blocks = _rows(s, "SELECT product_id, case_id, status_before FROM card_blocks")

    assert res.ok and res.data == {
        "product_id": "P1",
        "status_before": "Active",
        "status_after": "Blocked",
    }
    # The other active card of the customer stays active.
    assert statuses == {"P1": "Blocked", "P1B": "Active", "P2": "Active", "P3": "Closed"}
    assert blocks == [("P1", "K1", "Active")]


@pytest.mark.parametrize(("tx", "reason"), [("TXS", "not_a_card"), ("TXC", "card_not_active")])
def test_a_product_that_cannot_be_blocked_is_left_as_it_is(
    db: Database, tx: str, reason: str
) -> None:
    with db.session(customer_id="C1") as s:
        res = block_card(s, "C1", "K1", tx, "unrecognized_charge")
    with db.session(customer_id="C1") as s:
        blocks = s.execute(text("SELECT count(*) FROM card_blocks")).scalar_one()
    assert (res.ok, res.message, blocks) == (False, reason, 0)


# ---- CA4: a replay writes nothing ----------------------------------------------------------


def test_replaying_either_tool_returns_the_same_result_and_writes_nothing(
    db: Database,
) -> None:
    with db.session(customer_id="C1") as s:
        dispute = _register(s)
        block = block_card(s, "C1", "K1", "TX1", "unrecognized_charge")
    with db.session(customer_id="C1") as s:
        again = _register(s)
        block_again = block_card(s, "C1", "K1", "TX1", "unrecognized_charge")
        counts = _rows(
            s,
            "SELECT (SELECT count(*) FROM disputes), (SELECT count(*) FROM card_blocks), "
            "(SELECT count(*) FROM audit_log WHERE action IN ('register_dispute', 'block_card'))",
        )

    assert again.data == dispute.data and again.message == "already applied (idempotent)"
    assert block_again.data == block.data and block_again.message == again.message
    assert counts == [(1, 1, 2)]


# ---- CA5: ids of another customer ----------------------------------------------------------


def test_an_id_of_another_customer_is_refused_under_row_level_security(db: Database) -> None:
    with pytest.raises(OwnershipError):
        with db.session(customer_id="C1") as s:
            _register(s, tx="TX9")
    with pytest.raises(OwnershipError):
        with db.session(customer_id="C1") as s:
            block_card(s, "C1", "K1", "TX9", "unrecognized_charge")


def test_the_tools_check_ownership_themselves_without_row_level_security(
    schema_c1: SchemaUrls,
) -> None:
    # The owner bypasses RLS and sees TX9: the second layer is the tools' own check.
    owner = Database(schema_c1.admin)
    try:
        with pytest.raises(OwnershipError):
            with owner.session() as s:
                _register(s, tx="TX9")
        with pytest.raises(OwnershipError):
            with owner.session() as s:
                block_card(s, "C1", "K1", "TX9", "unrecognized_charge")
        with owner.session() as s:
            counts = _rows(
                s,
                "SELECT (SELECT count(*) FROM disputes), (SELECT count(*) FROM card_blocks), "
                "(SELECT product_status FROM products WHERE product_id = 'P9')",
            )
    finally:
        owner.dispose()
    assert counts == [(0, 0, "Active")]


# ---- CA6: no money moves -------------------------------------------------------------------


def test_registering_and_blocking_move_no_money(db: Database, schema_c1: SchemaUrls) -> None:
    query = "SELECT product_id, current_balance, credit_limit FROM products ORDER BY product_id"
    money = "SELECT transaction_id, amount, transaction_status FROM transactions ORDER BY 1"
    owner = Database(schema_c1.admin)
    try:
        with owner.session() as s:
            before = (_rows(s, query), _rows(s, money))
        with db.session(customer_id="C1") as s:
            _register(s)
            block_card(s, "C1", "K1", "TX1", "unrecognized_charge")
        with owner.session() as s:
            after = (_rows(s, query), _rows(s, money))
    finally:
        owner.dispose()
    assert after == before

"""Read-back after acting (TRZ-19): a verified action is confirmed (CA1, CA2); a tool that
answers ok without writing, or writes something else, leaves the case failed and escalated with
no confirmation to the customer (CA3, CA4)."""

from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.adapters.db.models import Dispute
from app.adapters.db.session import SchemaUrls
from app.core.config import Settings
from app.domain.policy_passages import PolicyDeadline, Unsupported
from app.main import create_app
from app.services import tools as T
from app.services.agent import VERIFICATION_FAILED_REASON
from tests.agent_support import still_not_recognized
from tests.auth_support import customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 17, 23, 59)
NETFLIX = "No reconozco un cargo de 120 dólares en Netflix"
STOLEN = "Me robaron la tarjeta y hay un cargo de 120 dólares en Netflix que no hice"
CINEPOLIS = "Me cobraron de más en Cinépolis: eran 40 dólares y me cobraron 400 dólares"


@pytest.fixture
def client(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1")],
        [
            transaction(
                "TX1",
                "C1",
                "P1",
                NOW - timedelta(hours=20),
                amount=120.0,
                currency="USD",
                merchant_name="Netflix",
            ),
            transaction(
                "TX2",
                "C1",
                "P1",
                NOW - timedelta(hours=30),
                amount=400.0,
                currency="USD",
                merchant_name="Cinépolis",
            ),
        ],
    )
    settings = Settings(database_url=database_url, llm_enabled=False, log_level="WARNING")
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        c.headers.update(customer_headers(c, "C1"))
        yield c


def _query(schema: SchemaUrls, sql: str) -> list[tuple]:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        rows = [tuple(r) for r in conn.execute(text(sql))]
    engine.dispose()
    return rows


def _confirmed(client: TestClient, message: str) -> dict:
    first = client.post("/chat", json={"message": message}).json()
    pending = still_not_recognized(client, first["case_id"])
    assert pending["outcome"] == "awaiting_confirmation"
    return _confirm(client, pending["case_id"], pending["pending_action"]["action_id"])


def _confirm(client: TestClient, case_id: str, action_id: str) -> dict:
    r = client.post(
        "/chat", json={"message": "sí", "case_id": case_id, "confirm_action_id": action_id}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _checks(schema: SchemaUrls, case_id: str) -> list[tuple]:
    return _query(
        schema,
        f"SELECT payload->>'action', verified, result->'mismatches' FROM audit_log "
        f"WHERE case_id = '{case_id}' AND action = 'verify_action' ORDER BY id",
    )


def _assert_failed(r: dict, schema: SchemaUrls) -> None:
    assert (r["outcome"], r["actions_taken"], r["dispute_folio"]) == ("failed", [], None)
    assert "DSP-" not in r["reply"] and "Registramos" not in r["reply"]
    assert _query(
        schema, f"SELECT status, escalation_reason FROM cases WHERE id = '{r['case_id']}'"
    ) == [("failed", VERIFICATION_FAILED_REASON)]


# ---- CA1, CA2: verified actions --------------------------------------------------------


def test_a_verified_registration_and_block_are_confirmed(
    client: TestClient, schema: SchemaUrls
) -> None:
    r = _confirmed(client, STOLEN)

    assert (r["outcome"], r["actions_taken"]) == (
        "registered_verified",
        ["register_dispute", "block_card"],
    )
    assert r["dispute_folio"] in r["reply"]
    assert _checks(schema, r["case_id"]) == [
        ("register_dispute", True, []),
        ("block_card", True, []),
    ]
    assert _query(schema, f"SELECT status FROM cases WHERE id = '{r['case_id']}'") == [
        ("registered_verified",)
    ]


def test_a_double_send_reads_the_action_back_again(client: TestClient, schema: SchemaUrls) -> None:
    first = client.post("/chat", json={"message": NETFLIX}).json()
    pending = still_not_recognized(client, first["case_id"])
    action_id = pending["pending_action"]["action_id"]
    once = _confirm(client, first["case_id"], action_id)
    twice = _confirm(client, first["case_id"], action_id)

    assert twice["outcome"] == "registered_verified"
    assert twice["dispute_folio"] == once["dispute_folio"]
    assert _checks(schema, first["case_id"]) == [("register_dispute", True, [])] * 2


# ---- CA3, CA4: a tool that answers ok without writing ------------------------------------


def test_a_registration_that_writes_nothing_fails_and_escalates(
    client: TestClient, schema: SchemaUrls, monkeypatch: pytest.MonkeyPatch
) -> None:
    def says_ok(*_a: object, **_k: object) -> T.ToolResult:
        return T.ToolResult(True, {"folio": "DSP-2026-00042", "due_date": "2026-07-09"})

    monkeypatch.setattr(T, "register_dispute", says_ok)
    r = _confirmed(client, NETFLIX)

    _assert_failed(r, schema)
    # The card is in the customer's hands: no urgent redirect.
    assert "línea de bloqueo" not in r["reply"]
    assert _checks(schema, r["case_id"]) == [("register_dispute", False, ["missing"])]
    assert _query(schema, "SELECT count(*) FROM disputes") == [(0,)]


def test_a_registration_that_writes_nothing_leaves_the_card_unblocked(
    client: TestClient, schema: SchemaUrls, monkeypatch: pytest.MonkeyPatch
) -> None:
    def says_ok(*_a: object, **_k: object) -> T.ToolResult:
        return T.ToolResult(True, {"folio": "DSP-2026-00042", "due_date": "2026-07-09"})

    def must_not_run(*_a: object, **_k: object) -> T.ToolResult:
        raise AssertionError("the card was blocked without a verified dispute")

    monkeypatch.setattr(T, "register_dispute", says_ok)
    monkeypatch.setattr(T, "block_card", must_not_run)
    r = _confirmed(client, STOLEN)

    _assert_failed(r, schema)
    # Without the card and without a block, the customer is sent to block it.
    assert "línea de bloqueo" in r["reply"]
    assert _checks(schema, r["case_id"]) == [("register_dispute", False, ["missing"])]
    assert _query(schema, "SELECT count(*) FROM disputes") == [(0,)]
    assert _query(schema, "SELECT count(*) FROM card_blocks") == [(0,)]
    assert _query(schema, "SELECT product_status FROM products") == [("Active",)]


def test_a_block_that_writes_nothing_sends_a_customer_without_the_card_to_block_it(
    client: TestClient, schema: SchemaUrls, monkeypatch: pytest.MonkeyPatch
) -> None:
    def says_blocked(*_a: object, **_k: object) -> T.ToolResult:
        return T.ToolResult(
            True, {"product_id": "P1", "status_before": "Active", "status_after": "Blocked"}
        )

    monkeypatch.setattr(T, "block_card", says_blocked)
    r = _confirmed(client, STOLEN)

    _assert_failed(r, schema)
    assert "línea de bloqueo" in r["reply"]
    assert _checks(schema, r["case_id"]) == [
        ("register_dispute", True, []),
        ("block_card", False, ["missing"]),
    ]
    # The dispute itself was verified and stays a valid one; the card was never blocked.
    assert _query(schema, "SELECT status FROM disputes") == [("opened",)]
    assert _query(schema, "SELECT product_status FROM products") == [("Active",)]


def test_a_registration_written_with_another_amount_is_marked_and_does_not_count(
    client: TestClient, schema: SchemaUrls, monkeypatch: pytest.MonkeyPatch
) -> None:
    def writes_wrong(
        session: Session,
        _clock: object,
        deadline: Callable[[date], PolicyDeadline | Unsupported],
        customer_id: str,
        case_id: str,
        transaction_id: str,
        dispute_type: str,
    ) -> T.ToolResult:
        due = deadline(NOW.date())
        assert isinstance(due, PolicyDeadline)
        session.add(
            Dispute(
                folio="DSP-2026-00042",
                customer_id=customer_id,
                case_id=case_id,
                transaction_id=transaction_id,
                dispute_type=dispute_type,
                amount=12.0,
                currency="USD",
                business_at=NOW,
                due_date=due.due,
            )
        )
        return T.ToolResult(True, {"folio": "DSP-2026-00042", "due_date": due.due.isoformat()})

    monkeypatch.setattr(T, "register_dispute", writes_wrong)
    r = _confirmed(client, NETFLIX)

    _assert_failed(r, schema)
    assert _checks(schema, r["case_id"]) == [("register_dispute", False, ["amount"])]
    assert _query(schema, "SELECT status FROM disputes") == [("verification_failed",)]
    monkeypatch.undo()

    # A dispute that failed its read-back is not an open dispute of the customer.
    nxt = client.post("/chat", json={"message": CINEPOLIS}).json()
    assert nxt["outcome"] == "awaiting_confirmation"


def test_a_failed_read_back_on_a_case_escalated_before_still_leaves_it_failed(
    client: TestClient, schema: SchemaUrls, monkeypatch: pytest.MonkeyPatch
) -> None:
    def says_ok(*_a: object, **_k: object) -> T.ToolResult:
        return T.ToolResult(True, {"folio": "DSP-2026-00042", "due_date": "2026-07-09"})

    first = client.post("/chat", json={"message": NETFLIX}).json()
    case_id = first["case_id"]
    pending = still_not_recognized(client, case_id)
    # An earlier turn of this case already put it in the queue.
    engine = create_engine(schema.admin)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log (trace_id, case_id, customer_id, actor, action, "
                "idempotency_key, result) VALUES ('t', :case_id, 'C1', 'tool', "
                "'escalate_to_human', :key, '{\"status\": \"escalated\"}')"
            ),
            {"case_id": case_id, "key": f"{case_id}:escalate_to_human"},
        )
    engine.dispose()
    monkeypatch.setattr(T, "register_dispute", says_ok)
    r = _confirm(client, case_id, pending["pending_action"]["action_id"])

    _assert_failed(r, schema)
    assert _query(
        schema,
        f"SELECT count(*) FROM audit_log WHERE idempotency_key = '{case_id}:escalate_to_human'",
    ) == [(1,)]
    assert _query(
        schema,
        f"SELECT result->>'status', result->>'reason' FROM audit_log "
        f"WHERE case_id = '{case_id}' AND action = 'verification_failed'",
    ) == [("failed", VERIFICATION_FAILED_REASON)]

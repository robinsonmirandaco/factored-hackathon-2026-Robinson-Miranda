"""Registering and blocking through /chat (TRZ-18): a confirmation runs only the exact pending
action it names (CA3), a double send repeats nothing (CA4), a foreign id found by a tool stops the
case and leaves nothing behind (CA5), the card that cannot be blocked is said so, and a registered
dispute counts for the open-dispute rule on its business date."""

import re
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.adapters.db.session import SchemaUrls
from app.core.config import Settings
from app.main import create_app
from app.services import tools as T
from tests.agent_support import still_not_recognized
from tests.auth_support import customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 17, 23, 59)
NETFLIX = "No reconozco un cargo de 120 dólares en Netflix"
STOLEN = "Me robaron la tarjeta y hay un cargo de 120 dólares en Netflix que no hice"
CINEPOLIS = "Me cobraron de más en Cinépolis: eran 40 dólares y me cobraron 400 dólares"


def _rows(**netflix: object) -> list[dict]:
    return [
        transaction(
            "TX1",
            "C1",
            "P1",
            NOW - timedelta(hours=20),
            amount=120.0,
            currency="USD",
            merchant_name="Netflix",
            **netflix,
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
    ]


def _client(database_url: str) -> Iterator[TestClient]:
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


def _counts(schema: SchemaUrls) -> tuple:
    return _query(
        schema,
        "SELECT (SELECT count(*) FROM disputes), (SELECT count(*) FROM card_blocks)",
    )[0]


def _confirm(client: TestClient, case_id: str, action_id: str) -> dict:
    r = client.post(
        "/chat", json={"message": "sí", "case_id": case_id, "confirm_action_id": action_id}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _pending(client: TestClient, message: str = NETFLIX) -> dict:
    first = client.post("/chat", json={"message": message}).json()
    body = still_not_recognized(client, first["case_id"])
    assert body["outcome"] == "awaiting_confirmation"
    return body


@pytest.fixture
def client(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1"), card("P1B", "C1", product_type="debit_card")],
        _rows(),
    )
    yield from _client(database_url)


# ---- CA3: the exact pending action ------------------------------------------------------


def test_a_confirmation_runs_the_action_it_names(client: TestClient, schema: SchemaUrls) -> None:
    pending = _pending(client, STOLEN)
    assert pending["pending_action"]["action"] == "register_and_block"
    done = _confirm(client, pending["case_id"], pending["pending_action"]["action_id"])

    assert (done["outcome"], done["actions_taken"]) == (
        "registered",
        ["register_dispute", "block_card"],
    )
    assert re.fullmatch(r"DSP-2026-\d{5}", done["dispute_folio"])
    assert done["dispute_folio"] in done["reply"]
    # Only the card of the charge: the other card of the customer stays active.
    assert _query(schema, "SELECT product_id, product_status FROM products ORDER BY 1") == [
        ("P1", "Blocked"),
        ("P1B", "Active"),
    ]
    assert _query(schema, "SELECT status FROM case_actions") == [("executed",)]


def test_a_replaced_action_runs_nothing(client: TestClient, schema: SchemaUrls) -> None:
    first = client.post("/chat", json={"message": CINEPOLIS}).json()
    assert first["outcome"] == "awaiting_confirmation"
    old = first["pending_action"]["action_id"]
    # The customer writes again on the same case and a new action is offered.
    second = client.post("/chat", json={"message": CINEPOLIS, "case_id": first["case_id"]}).json()
    new = second["pending_action"]["action_id"]
    assert new != old

    late = _confirm(client, first["case_id"], old)
    assert (late["outcome"], late["actions_taken"], late["dispute_folio"]) == (
        "no_pending_action",
        [],
        None,
    )
    assert _counts(schema) == (0, 0)
    assert _confirm(client, first["case_id"], new)["outcome"] == "registered"
    assert _query(schema, f"SELECT status FROM case_actions WHERE id = '{old}'") == [("replaced",)]


def test_a_double_send_returns_the_same_folio_and_writes_nothing(
    client: TestClient, schema: SchemaUrls
) -> None:
    pending = _pending(client, STOLEN)
    action_id = pending["pending_action"]["action_id"]
    once = _confirm(client, pending["case_id"], action_id)
    twice = _confirm(client, pending["case_id"], action_id)

    assert (twice["outcome"], twice["actions_taken"], twice["dispute_folio"]) == (
        "registered",
        once["actions_taken"],
        once["dispute_folio"],
    )
    assert _counts(schema) == (1, 1)


def test_an_action_cancelled_by_a_new_message_runs_nothing(
    client: TestClient, schema: SchemaUrls
) -> None:
    pending = _pending(client)
    action_id = pending["pending_action"]["action_id"]
    client.post("/chat", json={"message": "¿Cuál es mi saldo?", "case_id": pending["case_id"]})
    late = _confirm(client, pending["case_id"], action_id)
    assert (late["outcome"], late["actions_taken"]) == ("no_pending_action", [])
    assert _counts(schema) == (0, 0)
    assert _query(schema, "SELECT status FROM case_actions") == [("canceled",)]


def test_an_action_cancelled_by_a_security_stop_runs_nothing(
    client: TestClient, schema: SchemaUrls
) -> None:
    pending = _pending(client)
    action_id = pending["pending_action"]["action_id"]
    stopped = client.post(
        "/chat", json={"customer_id": "C2", "message": NETFLIX, "case_id": pending["case_id"]}
    ).json()
    assert stopped["outcome"] == "security_blocked"
    late = _confirm(client, pending["case_id"], action_id)
    assert (late["outcome"], late["actions_taken"]) == ("no_pending_action", [])
    assert _counts(schema) == (0, 0)


def test_the_action_of_another_case_runs_nothing(client: TestClient, schema: SchemaUrls) -> None:
    one = _pending(client)
    other = client.post("/chat", json={"message": CINEPOLIS}).json()
    late = _confirm(client, other["case_id"], one["pending_action"]["action_id"])
    assert (late["outcome"], late["actions_taken"]) == ("no_pending_action", [])
    assert _counts(schema) == (0, 0)


# ---- CA5: a foreign id found by a tool -----------------------------------------------------


@pytest.mark.parametrize("failing", ["register_dispute", "block_card"])
def test_a_foreign_id_found_by_a_tool_stops_the_case_and_leaves_nothing(
    client: TestClient, schema: SchemaUrls, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    def foreign(*_a: object, **_k: object) -> None:
        raise T.OwnershipError("transaction not found for the session customer")

    pending = _pending(client, STOLEN)
    monkeypatch.setattr(T, failing, foreign)
    r = _confirm(client, pending["case_id"], pending["pending_action"]["action_id"])

    assert (r["outcome"], r["actions_taken"], r["dispute_folio"]) == ("security_blocked", [], None)
    # When the block fails, the dispute written just before is rolled back with it.
    assert _counts(schema) == (0, 0)
    assert _query(schema, "SELECT status FROM case_actions") == [("canceled",)]
    # Only the savepoint is undone: the security stop is committed with the rest of the turn.
    case_id = pending["case_id"]
    assert _query(schema, f"SELECT status FROM cases WHERE id = '{case_id}'") == [
        ("security_blocked",)
    ]
    trail = _query(
        schema,
        f"SELECT actor, action, payload->>'reason', result->>'rule', result->>'status' "
        f"FROM audit_log WHERE case_id = '{case_id}' AND id > (SELECT max(id) FROM audit_log "
        f"WHERE case_id = '{case_id}' AND action = 'confirm') ORDER BY id",
    )
    assert trail[:3] == [
        ("agent", "security_event", "foreign_transaction_id", None, None),
        ("policy", "decide", None, "security.security_event", None),
        ("tool", "escalate_to_human", "security.security_event", None, "security_blocked"),
    ]
    # The dispute row and its audit row were rolled back together: no folio was kept.
    written = _query(
        schema, "SELECT count(*) FROM audit_log WHERE action IN ('register_dispute', 'block_card')"
    )
    assert written == [(0,)]


# ---- D5: a card that cannot be blocked -----------------------------------------------------


@pytest.fixture
def blocked_card(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1", product_status="Blocked")],
        _rows(),
    )
    yield from _client(database_url)


def test_without_the_card_and_without_a_block_the_customer_is_sent_to_block_it(
    blocked_card: TestClient, schema: SchemaUrls
) -> None:
    pending = _pending(blocked_card, STOLEN)
    r = _confirm(blocked_card, pending["case_id"], pending["pending_action"]["action_id"])
    assert (r["outcome"], r["actions_taken"]) == ("registered", ["register_dispute"])
    assert r["dispute_folio"] in r["reply"]
    assert "no pudimos bloquear la tarjeta" in r["reply"]
    assert "línea de bloqueo" in r["reply"]
    assert _counts(schema) == (1, 0)


# ---- the open-dispute rule reads the business date -----------------------------------------


def test_a_registered_dispute_escalates_the_next_one_of_the_customer(
    client: TestClient, schema: SchemaUrls
) -> None:
    pending = _pending(client)
    _confirm(client, pending["case_id"], pending["pending_action"]["action_id"])
    assert _query(schema, "SELECT business_at FROM disputes") == [(NOW,)]

    nxt = client.post("/chat", json={"message": CINEPOLIS}).json()
    assert nxt["outcome"] == "escalated"
    rule = _query(
        schema,
        f"SELECT result->>'rule' FROM audit_log WHERE action = 'decide' "
        f"AND case_id = '{nxt['case_id']}'",
    )
    assert rule == [("escalate.open_dispute_last_90d",)]


def test_a_dispute_from_100_business_days_ago_no_longer_counts(
    schema: SchemaUrls, database_url: str
) -> None:
    load(schema.admin, [customer("C1")], [card("P1", "C1")], _rows())
    # Registered in a case whose "now" was 100 days before this one.
    engine = create_engine(schema.admin)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, customer_id, intent, trace_id) "
                "VALUES ('OLD', 'C1', 'unrecognized_charge', 't')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO disputes (folio, customer_id, case_id, transaction_id, "
                "dispute_type, status, business_at) VALUES ('DSP-2026-00001', 'C1', 'OLD', "
                "'TX1', 'unrecognized_charge', 'opened', :at)"
            ),
            {"at": NOW - timedelta(days=100)},
        )
    engine.dispose()
    for c in _client(database_url):
        r = c.post("/chat", json={"message": CINEPOLIS}).json()
        assert r["outcome"] == "awaiting_confirmation"

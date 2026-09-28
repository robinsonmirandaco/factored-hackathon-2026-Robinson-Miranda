"""The recognition step and the choice among options through /chat (TRZ-16): the charge is shown
from the database, the two buttons, a choice accepted only among the options shown, and a twin
charge decided as a duplicate only when the customer has the card."""

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.adapters.db.session import SchemaUrls
from app.core.config import Settings
from app.main import create_app
from tests.agent_support import still_not_recognized
from tests.auth_support import customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 17, 23, 59)
NETFLIX = "No reconozco un cargo de 120 dólares en Netflix"
OXXO = "No reconozco un cargo de 35 dólares en Oxxo"
STOLEN_OXXO = "Me robaron la tarjeta y hay un cargo de 35 dólares en Oxxo que no hice"


def _netflix(**extra: object) -> dict:
    return transaction(
        "TX1",
        "C1",
        "P1",
        NOW - timedelta(hours=20),
        amount=120.0,
        currency="USD",
        merchant_name="Netflix",
        channel="Web",
        transaction_city="Bogotá",
        **extra,
    )


def _client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, llm_enabled=False, log_level="WARNING")
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        c.headers.update(customer_headers(c, "C1"))
        yield c


def _query(schema: SchemaUrls, sql: str) -> list:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        rows = [tuple(r) for r in conn.execute(text(sql))]
    engine.dispose()
    return rows


def _audit(schema: SchemaUrls, case_id: str) -> list[tuple]:
    return _query(
        schema,
        f"SELECT action, payload::text, result::text FROM audit_log "
        f"WHERE case_id = '{case_id}' ORDER BY id",
    )


@pytest.fixture
def netflix(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1", product_number_last4="4821")],
        [_netflix()],
    )
    yield from _client(database_url)


# ---- CA1 and CA5: the detail, from the database, and the two buttons ---------------------


def test_the_charge_is_shown_from_the_database_before_any_decision(
    netflix: TestClient, schema: SchemaUrls
) -> None:
    r = netflix.post("/chat", json={"message": NETFLIX})
    assert r.status_code == 200
    body = r.json()
    assert (body["outcome"], body["autonomy_level"], body["actions_taken"]) == (
        "recognizing",
        "L0",
        [],
    )
    charge = body["charge"]
    assert {k: charge[k] for k in ("merchant", "city", "channel", "last4", "status")} == {
        "merchant": "Netflix",
        "city": "Bogotá",
        "channel": "Web",
        "last4": "4821",
        "status": "Approved",
    }
    assert (charge["product_type"], charge["amount"], charge["currency"]) == (
        "credit_card",
        120.0,
        "USD",
    )
    assert charge["at"] == (NOW - timedelta(hours=20)).isoformat()
    assert [c["id"] for c in body["choices"]] == ["not_recognized", "recognized"]
    assert "tarjeta de crédito terminada en 4821" in body["reply"]
    assert "Ciudad: Bogotá" in body["reply"]

    trail = _audit(schema, body["case_id"])
    assert "decide" not in [a for a, _, _ in trail]
    # The last four digits reach the customer only, never the audit log.
    assert all("4821" not in f"{p}{res}" for _, p, res in trail)


def test_already_recognized_closes_the_case_with_nothing_done(
    netflix: TestClient, schema: SchemaUrls
) -> None:
    first = netflix.post("/chat", json={"message": NETFLIX}).json()
    r = netflix.post(
        "/chat",
        json={
            "message": "Ya lo reconozco",
            "case_id": first["case_id"],
            "recognition": "recognized",
        },
    ).json()
    assert (r["outcome"], r["autonomy_level"], r["actions_taken"]) == (
        "recognized_closed",
        "L0",
        [],
    )
    assert _query(schema, "SELECT status FROM cases") == [("recognized_closed",)]
    trail = _audit(schema, first["case_id"])
    recognize = next(res for a, _, res in trail if a == "recognize")
    assert '"choice": "recognized"' in recognize
    assert "decide" not in [a for a, _, _ in trail]
    assert _query(schema, "SELECT count(*) FROM disputes") == [(0,)]


def test_still_not_recognized_goes_to_the_policy_with_no_further_question(
    netflix: TestClient, schema: SchemaUrls
) -> None:
    first = netflix.post("/chat", json={"message": NETFLIX}).json()
    r = still_not_recognized(netflix, first["case_id"])
    assert (r["outcome"], r["autonomy_level"]) == ("awaiting_confirmation", "L1")
    assert r["charge"] is None and r["choices"] == []
    decide = next(res for a, _, res in _audit(schema, first["case_id"]) if a == "decide")
    assert "routing.unrecognized_charge.card_in_possession" in decide


def test_a_recognition_answer_with_nothing_waiting_does_nothing(
    netflix: TestClient, schema: SchemaUrls
) -> None:
    first = netflix.post("/chat", json={"message": NETFLIX}).json()
    still_not_recognized(netflix, first["case_id"])
    again = netflix.post(
        "/chat",
        json={
            "message": "Ya lo reconozco",
            "case_id": first["case_id"],
            "recognition": "recognized",
        },
    ).json()
    assert (again["outcome"], again["actions_taken"]) == ("no_pending_recognition", [])
    assert _query(schema, "SELECT status FROM cases") == [("awaiting_confirmation",)]


@pytest.mark.parametrize(
    "body",
    [
        {"message": "Ya lo reconozco", "recognition": "recognized"},
        {"message": "Ninguno", "option": "none"},
        {
            "message": "sí",
            "case_id": "CASE-X",
            "confirm_action_id": "ACT-0000000000",
            "recognition": "recognized",
        },
        {"message": "x", "case_id": "CASE-X", "recognition": "maybe"},
        {"message": "x", "case_id": "CASE-X", "option": ""},
    ],
    ids=["recognition without case", "option without case", "two answers", "unknown", "empty"],
)
def test_chat_rejects_an_invalid_answer(netflix: TestClient, body: dict) -> None:
    r = netflix.post("/chat", json=body)
    assert r.status_code == 422
    assert r.json()["error_code"] == "validation_error"


# ---- CA2, CA3 and CA4: pending, twin and earlier months, read from the database ----------


@pytest.fixture
def pending_twin(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1")],
        [
            transaction(
                "TXP",
                "C1",
                "P1",
                NOW - timedelta(hours=3),
                amount=35.0,
                currency="USD",
                merchant_name="Oxxo",
                transaction_status="Pending",
            ),
            transaction(
                "TXA",
                "C1",
                "P1",
                NOW - timedelta(days=4),
                amount=35.0,
                currency="USD",
                merchant_name="Oxxo",
            ),
            transaction(
                "TXOLD",
                "C1",
                "P1",
                datetime(2026, 4, 20, 12, 0),
                amount=12.0,
                currency="USD",
                merchant_name="Oxxo",
            ),
        ],
    )
    yield from _client(database_url)


def test_pending_twin_and_earlier_months_are_explained(pending_twin: TestClient) -> None:
    first = pending_twin.post("/chat", json={"message": OXXO}).json()
    assert first["outcome"] == "identifying"
    assert {o["transaction_id"] for o in first["options"]} == {"TXP", "TXA"}

    shown = pending_twin.post(
        "/chat", json={"message": "El pendiente", "case_id": first["case_id"], "option": "TXP"}
    ).json()
    assert shown["outcome"] == "recognizing"
    charge = shown["charge"]
    assert charge["status"] == "Pending"
    assert charge["twin"] == {"at": (NOW - timedelta(days=4)).isoformat(), "status": "Approved"}
    assert charge["earlier_months"] == ["2026-04"]
    assert "aún no se ha liquidado" in shown["reply"]
    assert "retención temporal" in shown["reply"]
    assert "Tienes cargos de este comercio en abril de 2026." in shown["reply"]


# ---- D2: an approved twin is a duplicate only when the customer has the card -------------


@pytest.fixture
def approved_twin(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1")],
        [
            transaction(
                "TXA",
                "C1",
                "P1",
                NOW - timedelta(hours=5),
                amount=35.0,
                currency="USD",
                merchant_name="Oxxo",
            ),
            transaction(
                "TXB",
                "C1",
                "P1",
                NOW - timedelta(hours=30),
                amount=35.0,
                currency="USD",
                merchant_name="Oxxo",
            ),
        ],
    )
    yield from _client(database_url)


def _to_recognition(client: TestClient, message: str) -> dict:
    first = client.post("/chat", json={"message": message}).json()
    assert first["outcome"] == "identifying"
    shown = client.post(
        "/chat", json={"message": "Ese", "case_id": first["case_id"], "option": "TXA"}
    ).json()
    assert shown["outcome"] == "recognizing"
    assert shown["charge"]["twin"]["status"] == "Approved"
    assert "puede ser un cobro duplicado" in shown["reply"]
    return shown


def test_with_the_card_an_approved_twin_is_decided_as_a_duplicate(
    approved_twin: TestClient, schema: SchemaUrls
) -> None:
    shown = _to_recognition(approved_twin, OXXO)
    r = still_not_recognized(approved_twin, shown["case_id"])
    assert (r["intent"], r["outcome"]) == ("billing_error_duplicate", "awaiting_confirmation")
    decide = next(res for a, _, res in _audit(schema, shown["case_id"]) if a == "decide")
    assert "routing.billing_error_duplicate.both_approved" in decide
    payload = next(p for a, p, _ in _audit(schema, shown["case_id"]) if a == "decide")
    assert '"rerouted_from": "unrecognized_charge"' in payload


def test_without_the_card_an_approved_twin_stays_on_the_fraud_path(
    approved_twin: TestClient, schema: SchemaUrls
) -> None:
    shown = _to_recognition(approved_twin, STOLEN_OXXO)
    r = still_not_recognized(approved_twin, shown["case_id"])
    assert (r["intent"], r["outcome"], r["autonomy_level"]) == (
        "unrecognized_charge",
        "awaiting_confirmation",
        "L2",
    )
    decide = next(res for a, _, res in _audit(schema, shown["case_id"]) if a == "decide")
    assert "routing.unrecognized_charge.card_not_in_possession" in decide


# ---- the choice among options: only an id that was shown ---------------------------------


@pytest.fixture
def options(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    load(
        schema.admin,
        [customer("C1"), customer("C2")],
        [card("P1", "C1"), card("P2", "C2")],
        [
            transaction(
                "TX1", "C1", "P1", NOW - timedelta(hours=5), amount=45.0, merchant_name="Oxxo"
            ),
            transaction(
                "TX2", "C1", "P1", NOW - timedelta(hours=30), amount=47.0, merchant_name="Oxxo"
            ),
            # The customer's own charge, but not among the options of the case.
            transaction(
                "TX3", "C1", "P1", NOW - timedelta(days=2), amount=900.0, merchant_name="Exito"
            ),
            transaction(
                "TXC2", "C2", "P2", NOW - timedelta(hours=5), amount=45.0, merchant_name="Oxxo"
            ),
        ],
    )
    yield from _client(database_url)


def _options(client: TestClient) -> str:
    first = client.post("/chat", json={"message": "No reconozco un cargo en Oxxo"}).json()
    assert first["outcome"] == "identifying"
    assert {o["transaction_id"] for o in first["options"]} == {"TX1", "TX2"}
    return first["case_id"]


def test_choosing_a_shown_option_goes_to_the_recognition_step(options: TestClient) -> None:
    case_id = _options(options)
    r = options.post(
        "/chat", json={"message": "El de 45", "case_id": case_id, "option": "TX1"}
    ).json()
    assert (r["outcome"], r["charge"]["transaction_id"]) == ("recognizing", "TX1")


@pytest.mark.parametrize("chosen", ["TX3", "TXC2", "TX-UNKNOWN"])
def test_an_id_that_was_not_shown_is_a_security_event(
    options: TestClient, schema: SchemaUrls, chosen: str
) -> None:
    case_id = _options(options)
    r = options.post("/chat", json={"message": "Ese", "case_id": case_id, "option": chosen})
    assert r.status_code == 200
    body = r.json()
    assert (body["outcome"], body["autonomy_level"], body["charge"]) == (
        "security_blocked",
        "L3",
        None,
    )
    trail = _audit(schema, case_id)
    event = next(p for a, p, _ in trail if a == "security_event")
    assert '"reason": "option_not_shown"' in event
    # From the choice on, the id chosen is not stored, and nothing of it is read or revealed.
    since = [a for a, _, _ in trail].index("choose")
    assert all(chosen not in f"{p}{res}" for _, p, res in trail[since:])
    assert chosen not in body["reply"]


def test_none_of_the_options_escalates(options: TestClient, schema: SchemaUrls) -> None:
    case_id = _options(options)
    r = options.post(
        "/chat", json={"message": "Ninguno es", "case_id": case_id, "option": "none"}
    ).json()
    assert (r["outcome"], r["autonomy_level"]) == ("escalated", "L3")
    decide = next(res for a, _, res in _audit(schema, case_id) if a == "decide")
    assert "escalate.conformal_set_empty" in decide


def test_a_choice_is_taken_once(options: TestClient) -> None:
    case_id = _options(options)
    options.post("/chat", json={"message": "Ese", "case_id": case_id, "option": "TX1"})
    again = options.post(
        "/chat", json={"message": "Ninguno", "case_id": case_id, "option": "none"}
    ).json()
    assert again["outcome"] == "no_pending_choice"
    # After the choice, the other option is no longer shown: naming it is a security event.
    late = options.post(
        "/chat", json={"message": "El otro", "case_id": case_id, "option": "TX2"}
    ).json()
    assert late["outcome"] == "security_blocked"

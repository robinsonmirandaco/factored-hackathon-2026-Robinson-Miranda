"""The customer web and the reads behind it (TRZ-34): the button door of /chat (TRZ-15 CA9),
GET /me, /me/products, /me/transactions and /me/clarifications with success, validation and a
failed dependency, the chips of what /chat read (design 10.1), and the web served from the same
origin with its security headers (CA5)."""

import dataclasses
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.adapters.db.session import SchemaUrls
from app.api.deps import get_customer_session
from app.core.config import Settings
from app.main import create_app
from tests.agent_support import agent_deps, fake_llm, llm_settings, reading, still_not_recognized
from tests.auth_support import analyst_headers, customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 17, 23, 59)
BUTTON_ES = "No lo reconozco"
BUTTON_PT = "Não reconheço"


def _tx(tx_id: str, customer_id: str, product: str, at: datetime, **extra: Any) -> dict:
    return transaction(tx_id, customer_id, product, at, **extra)


@pytest.fixture
def schema_rows(schema: SchemaUrls) -> SchemaUrls:
    load(
        schema.admin,
        [customer("C1", first_name="Ana"), customer("C2")],
        [
            card("P1", "C1", currency="USD", product_number_last4="4821", current_balance=250.0),
            card("P2", "C2", currency="USD"),
        ],
        [
            _tx(
                "TX1",
                "C1",
                "P1",
                NOW - timedelta(hours=20),
                amount=120.0,
                currency="USD",
                merchant_name="Netflix",
                channel="Web",
                transaction_city="Bogotá",
            ),
            _tx(
                "TXP",
                "C1",
                "P1",
                NOW - timedelta(days=2),
                amount=50000.0,
                merchant_name="Rappi",
                transaction_status="Pending",
            ),
            _tx("TXD", "C1", "P1", NOW - timedelta(days=3), transaction_status="Declined"),
            _tx("TXOLD", "C1", "P1", NOW - timedelta(days=200)),
            _tx("TXFUT", "C1", "P1", NOW + timedelta(days=1)),
            _tx("TXC2", "C2", "P2", NOW - timedelta(days=1), amount=90.0, currency="USD"),
        ],
    )
    engine = create_engine(schema.admin)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO exchange_rates (date, source_currency, target_currency, "
                "exchange_rate) VALUES (:d, 'USD', 'COP', 4000)"
            ),
            {"d": (NOW - timedelta(hours=20)).date()},
        )
        # Opened before the 90 days the open-dispute rule looks at, and past its deadline.
        created = NOW - timedelta(days=100)
        conn.execute(
            text(
                "INSERT INTO complaints (complaint_id, customer_id, creation_date, process_date, "
                "case_type, category, subcategory, reception_channel, has_affected_product, "
                "priority, status, sla_breached, is_repeat_complainer) VALUES ('CMP-OPEN0001', "
                "'C1', :c, :d, 'Claim', 'Transactions', 'Cargo no reconocido', 'App', true, "
                "'Medium', 'Open', false, false)"
            ),
            {"c": created, "d": created.date()},
        )
    engine.dispose()
    return schema


@pytest.fixture
def app_client(schema_rows: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, llm_enabled=False, log_level="WARNING")
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def client(app_client: TestClient) -> TestClient:
    app_client.headers.update(customer_headers(app_client, "C1"))
    return app_client


def _audit(schema: SchemaUrls, case_id: str) -> list[tuple[str, str, str]]:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT action, payload::text, result::text FROM audit_log "
                "WHERE case_id = :c ORDER BY id"
            ),
            {"c": case_id},
        ).all()
    engine.dispose()
    return [tuple(r) for r in rows]


def _broken(app_client: TestClient) -> None:
    class BrokenSession:
        info: dict[str, Any] = {}

        def get(self, *_args: object) -> None:
            raise OperationalError("SELECT", {}, Exception("connection refused"))

        execute = get

    app: FastAPI = app_client.app  # type: ignore[assignment]
    app.dependency_overrides[get_customer_session] = lambda: BrokenSession()


# ---- the button door of /chat (TRZ-15 CA9) -------------------------------------------------


def test_the_button_opens_a_case_at_the_recognition_step(
    client: TestClient, schema_rows: SchemaUrls
) -> None:
    r = client.post("/chat", json={"message": BUTTON_ES, "transaction_id": "TX1"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["intent"], body["outcome"]) == ("unrecognized_charge", "recognizing")
    assert body["charge"]["transaction_id"] == "TX1" and body["charge"]["last4"] == "4821"
    assert [c["id"] for c in body["choices"]] == ["not_recognized", "recognized"]
    trail = _audit(schema_rows, body["case_id"])
    actions = [a for a, _, _ in trail]
    # No comprehension: the charge arrived chosen, and no LLM was asked.
    assert "comprehend" not in actions and actions[0] == "button_press"
    identify = next(res for a, p, res in trail if a == "identify_transaction")
    assert '"conformal_set": ["TX1"]' in identify
    door = next(p for a, p, _ in trail if a == "identify_transaction")
    assert '"door": "button"' in door


def test_the_button_on_a_pending_charge_goes_on_like_a_conversation(client: TestClient) -> None:
    first = client.post("/chat", json={"message": "¿Qué es esto?", "transaction_id": "TXP"}).json()
    assert first["outcome"] == "recognizing" and first["charge"]["status"] == "Pending"

    after = still_not_recognized(client, first["case_id"])

    assert after["case_id"] == first["case_id"] and after["outcome"] != "recognizing"


def test_the_button_label_sets_the_language_of_the_case(
    client: TestClient, schema_rows: SchemaUrls
) -> None:
    body = client.post("/chat", json={"message": BUTTON_PT, "transaction_id": "TX1"}).json()

    choices = {c["id"]: c["label"] for c in body["choices"]}
    assert choices["not_recognized"] != "Sigo sin reconocerlo"
    engine = create_engine(schema_rows.admin)
    with engine.connect() as conn:
        language = conn.execute(
            text("SELECT language FROM cases WHERE id = :c"), {"c": body["case_id"]}
        ).scalar()
    engine.dispose()
    assert language == "pt"


def test_the_button_on_another_customers_charge_is_a_security_event(
    client: TestClient, schema_rows: SchemaUrls
) -> None:
    r = client.post("/chat", json={"message": BUTTON_ES, "transaction_id": "TXC2"})

    body = r.json()
    assert (body["outcome"], body["charge"]) == ("security_blocked", None)
    trail = _audit(schema_rows, body["case_id"])
    assert any(a == "security_event" and "foreign_transaction_id" in p for a, p, _ in trail)
    # The foreign id is not stored in this customer's trail.
    assert all("TXC2" not in f"{p}{res}" for _, p, res in trail)


@pytest.mark.parametrize("tx_id", ["TXOLD", "TXD"])
def test_the_button_on_an_own_charge_that_is_not_disputable_finds_no_charge(
    client: TestClient, schema_rows: SchemaUrls, tx_id: str
) -> None:
    body = client.post("/chat", json={"message": BUTTON_ES, "transaction_id": tx_id}).json()

    assert body["outcome"] == "escalated"
    decide = next(res for a, _, res in _audit(schema_rows, body["case_id"]) if a == "decide")
    assert "escalate.conformal_set_empty" in decide


@pytest.mark.parametrize(
    "extra",
    [{"case_id": "CASE-1"}, {"option": "TX1"}, {"recognition": "recognized", "case_id": "C"}],
)
def test_the_button_goes_alone(client: TestClient, extra: dict[str, str]) -> None:
    r = client.post("/chat", json={"message": BUTTON_ES, "transaction_id": "TX1", **extra})
    assert r.status_code == 422 and r.json()["error_code"] == "validation_error"


# ---- GET /me ---------------------------------------------------------------------------------


def test_me_returns_the_customer_and_the_simulated_now(client: TestClient) -> None:
    r = client.get("/me")

    assert r.status_code == 200
    assert r.json() == {
        "first_name": "Ana",
        "country_code": "CO",
        "local_currency": "COP",
        "now": "2026-06-17T23:59:00",
        "demo": True,
    }


def test_me_needs_a_customer_session(app_client: TestClient) -> None:
    assert app_client.get("/me").status_code == 401
    assert app_client.get("/me", headers=analyst_headers(app_client)).status_code == 403


def test_me_with_the_database_down_is_503(client: TestClient) -> None:
    _broken(client)
    r = client.get("/me")
    client.app.dependency_overrides.clear()  # type: ignore[attr-defined]
    assert r.status_code == 503 and r.json()["error_code"] == "db_unavailable"


# ---- GET /me/products ------------------------------------------------------------------------


def test_products_are_only_the_session_customers(client: TestClient) -> None:
    r = client.get("/me/products")

    assert r.status_code == 200
    assert [(p["product_id"], p["last4"], p["current_balance"]) for p in r.json()] == [
        ("P1", "4821", 250.0)
    ]


def test_products_need_a_customer_session(app_client: TestClient) -> None:
    r = app_client.get("/me/products", headers=analyst_headers(app_client))
    assert r.status_code == 403


def test_products_with_the_database_down_are_503(client: TestClient) -> None:
    _broken(client)
    r = client.get("/me/products")
    client.app.dependency_overrides.clear()  # type: ignore[attr-defined]
    assert r.status_code == 503 and r.json()["error_code"] == "db_unavailable"


# ---- GET /me/transactions --------------------------------------------------------------------


def test_transactions_are_paged_newest_first_up_to_the_simulated_now(client: TestClient) -> None:
    first = client.get("/me/transactions", params={"limit": 2}).json()
    rest = client.get(
        "/me/transactions", params={"limit": 10, "before": first["next_before"]}
    ).json()

    ids = [m["transaction_id"] for m in first["items"] + rest["items"]]
    # TXFUT lies after the simulated now; TXC2 belongs to someone else.
    assert ids == ["TX1", "TXP", "TXD", "TXOLD"]
    assert first["next_before"] == "TXP" and rest["next_before"] is None


def test_transactions_say_which_can_be_disputed_and_the_local_amount(client: TestClient) -> None:
    items = {m["transaction_id"]: m for m in client.get("/me/transactions").json()["items"]}

    assert {k: v["disputable"] for k, v in items.items()} == {
        "TX1": True,
        "TXP": True,
        "TXD": False,
        "TXOLD": False,
    }
    netflix = items["TX1"]
    assert (netflix["amount"], netflix["currency"]) == (120.0, "USD")
    assert (netflix["converted_amount"], netflix["converted_currency"]) == (480000.0, "COP")
    assert netflix["converted_label"]
    assert items["TXP"]["converted_amount"] is None


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"before": ""}])
def test_transactions_reject_an_invalid_page(client: TestClient, params: dict) -> None:
    r = client.get("/me/transactions", params=params)
    assert r.status_code == 422 and r.json()["error_code"] == "validation_error"


def test_a_cursor_of_someone_else_is_invalid(client: TestClient) -> None:
    r = client.get("/me/transactions", params={"before": "TXC2"})
    assert r.status_code == 422 and r.json()["error_code"] == "invalid_cursor"


def test_transactions_with_the_database_down_are_503(client: TestClient) -> None:
    _broken(client)
    r = client.get("/me/transactions")
    client.app.dependency_overrides.clear()  # type: ignore[attr-defined]
    assert r.status_code == 503 and r.json()["error_code"] == "db_unavailable"


# ---- GET /me/clarifications ------------------------------------------------------------------


def test_clarifications_show_cases_with_a_person_and_the_banks_open_claims(
    client: TestClient,
) -> None:
    client.post("/chat", json={"message": BUTTON_ES, "transaction_id": "TXOLD"})

    items = client.get("/me/clarifications").json()

    escalated = next(i for i in items if i["source"] == "cases")
    assert escalated["status"] == "escalated" and escalated["due_date"] is None
    claim = next(i for i in items if i["source"] == "complaints")
    assert claim["id"] == "CMP-OPEN0001" and claim["due_date"] and claim["passage_id"]
    assert claim["overdue"] is True and claim["due_date"] < "2026-06-17"


def test_a_registered_dispute_shows_its_folio_and_deadline(client: TestClient) -> None:
    case_id = client.post("/chat", json={"message": BUTTON_ES, "transaction_id": "TX1"}).json()[
        "case_id"
    ]
    offered = still_not_recognized(client, case_id)
    assert offered["outcome"] == "awaiting_confirmation", offered
    done = client.post(
        "/chat",
        json={
            "message": "Sí",
            "case_id": case_id,
            "confirm_action_id": offered["pending_action"]["action_id"],
        },
    ).json()
    assert done["dispute_folio"]

    items = client.get("/me/clarifications").json()

    mine = next(i for i in items if i["case_id"] == case_id)
    assert (mine["source"], mine["status"], mine["id"], mine["merchant"]) == (
        "disputes",
        "registered",
        done["dispute_folio"],
        "Netflix",
    )
    assert mine["due_date"] and mine["opened_on"] == "2026-06-17" and mine["overdue"] is False


def test_clarifications_leave_no_audit_row(client: TestClient, schema_rows: SchemaUrls) -> None:
    client.get("/me/clarifications")
    engine = create_engine(schema_rows.admin)
    with engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM audit_log WHERE action = 'get_open_claims'"))
        count = n.scalar()
    engine.dispose()
    assert count == 0


def test_clarifications_with_the_database_down_are_503(client: TestClient) -> None:
    _broken(client)
    r = client.get("/me/clarifications")
    client.app.dependency_overrides.clear()  # type: ignore[attr-defined]
    assert r.status_code == 503 and r.json()["error_code"] == "db_unavailable"


# ---- the web, from the same origin (CA5) -----------------------------------------------------


def test_the_web_is_served_with_its_security_policy(app_client: TestClient) -> None:
    page = app_client.get("/")
    script = app_client.get("/assets/customer.js")

    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    policy = page.headers["content-security-policy"]
    assert "script-src 'self'" in policy and "unsafe-inline" not in policy
    assert page.headers["x-content-type-options"] == "nosniff"
    assert "/assets/customer.js" in page.text
    assert script.status_code == 200
    # The API keeps answering JSON next to the web.
    assert app_client.get("/health").headers["content-type"].startswith("application/json")


# ---- the chips of what was read (design 10.1) ------------------------------------------------

READ_NETFLIX = "No reconozco 120 dólares en Netflix, todavía tengo la tarjeta"


def _read(message: str) -> dict[str, Any]:
    return reading(
        "unrecognized_charge",
        "es-CO",
        amount={"value": 120, "currency": "USD", "approximate": False, "evidence": "120 dólares"},
        merchant_hint={"value": "Netflix", "evidence": "Netflix"},
        card_in_possession={"value": True, "evidence": "todavía tengo la tarjeta"},
    )


READ_LAST_WEEK = "No reconozco un cargo de Netflix de la semana pasada"


def _read_last_week(message: str) -> dict[str, Any]:
    return reading(
        "unrecognized_charge",
        "es-CO",
        merchant_hint={"value": "Netflix", "evidence": "Netflix"},
        date={
            "expression": "la semana pasada",
            "kind": "last_week",
            "count": None,
            "day": None,
            "month": None,
            "year": None,
            "evidence": "de la semana pasada",
        },
    )


@pytest.fixture
def llm_client(schema_rows: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    settings = llm_settings(database_url, log_level="WARNING")
    app = create_app(settings)
    deps = agent_deps(settings, fake_llm(settings, _read))
    app.state.runtime = dataclasses.replace(app.state.runtime, agent=deps)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers.update(customer_headers(c, "C1"))
        yield c


def test_chat_shows_what_it_read_with_the_literal_fragment(llm_client: TestClient) -> None:
    body = llm_client.post("/chat", json={"message": READ_NETFLIX}).json()

    no_window = {"window_from": None, "window_to": None}
    assert body["clues"] == [
        {"field": "amount", "value": "120 USD", "evidence": "120 dólares", **no_window},
        {"field": "merchant_hint", "value": "Netflix", "evidence": "Netflix", **no_window},
        {
            "field": "card_in_possession",
            "value": "yes",
            "evidence": "todavía tengo la tarjeta",
            **no_window,
        },
    ]
    assert all(c["evidence"] in READ_NETFLIX for c in body["clues"])
    # The charge found in the database is shown apart; no chip carries its data.
    assert body["charge"]["last4"] == "4821"
    assert all("4821" not in c["value"] + c["evidence"] for c in body["clues"])
    assert all("Bogotá" not in c["value"] + c["evidence"] for c in body["clues"])


def test_a_button_reads_nothing(llm_client: TestClient) -> None:
    first = llm_client.post("/chat", json={"message": BUTTON_ES, "transaction_id": "TX1"}).json()
    after = still_not_recognized(llm_client, first["case_id"])

    assert first["clues"] == [] and after["clues"] == []


# ---- a registered dispute is final (QA finding 1) --------------------------------------------


def _register_tx1(client: TestClient) -> tuple[str, str]:
    case_id = client.post("/chat", json={"message": BUTTON_ES, "transaction_id": "TX1"}).json()[
        "case_id"
    ]
    offered = still_not_recognized(client, case_id)
    done = client.post(
        "/chat",
        json={
            "message": "Sí",
            "case_id": case_id,
            "confirm_action_id": offered["pending_action"]["action_id"],
        },
    ).json()
    assert done["outcome"] == "registered_verified", done
    return case_id, done["dispute_folio"]


def _case_row(schema: SchemaUrls, case_id: str) -> tuple[str, str | None]:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, transaction_id FROM cases WHERE id = :c"), {"c": case_id}
        ).one()
    engine.dispose()
    return row[0], row[1]


def test_a_new_charge_after_a_registration_opens_a_new_case(
    client: TestClient, schema_rows: SchemaUrls
) -> None:
    case_id, folio = _register_tx1(client)

    later = client.post(
        "/chat", json={"message": "Tampoco reconozco un cargo en Rappi", "case_id": case_id}
    ).json()

    assert later["case_id"] != case_id
    assert _case_row(schema_rows, case_id) == ("registered_verified", "TX1")
    items = client.get("/me/clarifications").json()
    dispute = next(i for i in items if i["folio"] == folio)
    assert (dispute["merchant"], dispute["amount"], dispute["currency"]) == (
        "Netflix",
        120.0,
        "USD",
    )
    assert dispute["case_id"] == case_id


def test_a_dispute_shows_the_charge_it_was_registered_on(
    client: TestClient, schema_rows: SchemaUrls
) -> None:
    case_id, folio = _register_tx1(client)
    # Even if a case row were changed afterwards, the dispute keeps its own charge.
    engine = create_engine(schema_rows.admin)
    with engine.begin() as conn:
        conn.execute(text("UPDATE cases SET transaction_id = 'TXP' WHERE id = :c"), {"c": case_id})
    engine.dispose()

    items = client.get("/me/clarifications").json()

    mine = [i for i in items if i["folio"] == folio]
    assert len(mine) == 1 and mine[0]["merchant"] == "Netflix" and mine[0]["source"] == "disputes"


# ---- the offered block is its own action, and every offer can be declined (QA finding 2) -----


def _offer(client: TestClient) -> dict[str, Any]:
    case_id = client.post("/chat", json={"message": BUTTON_ES, "transaction_id": "TX1"}).json()[
        "case_id"
    ]
    offered = still_not_recognized(client, case_id)
    assert offered["pending_action"]["action"] == "register_and_offer_block", offered
    return offered


def _confirm(client: TestClient, turn: dict[str, Any]) -> dict[str, Any]:
    return client.post(
        "/chat",
        json={
            "message": "Sí",
            "case_id": turn["case_id"],
            "confirm_action_id": turn["pending_action"]["action_id"],
        },
    ).json()


def _decline(client: TestClient, turn: dict[str, Any]) -> dict[str, Any]:
    r = client.post(
        "/chat",
        json={
            "message": "No, gracias",
            "case_id": turn["case_id"],
            "decline_action_id": turn["pending_action"]["action_id"],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _counts(schema: SchemaUrls) -> tuple[int, int, str]:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        disputes = conn.execute(text("SELECT count(*) FROM disputes")).scalar()
        blocks = conn.execute(text("SELECT count(*) FROM card_blocks")).scalar()
        status = conn.execute(
            text("SELECT product_status FROM products WHERE product_id = 'P1'")
        ).scalar()
    engine.dispose()
    return int(disputes or 0), int(blocks or 0), str(status)


def test_the_question_names_the_charge_it_registers(client: TestClient) -> None:
    offered = _offer(client)

    pending = offered["pending_action"]
    assert (pending["merchant"], pending["amount"], pending["currency"]) == (
        "Netflix",
        120.0,
        "USD",
    )


def test_registering_offers_the_block_as_its_own_action(
    client: TestClient, schema_rows: SchemaUrls
) -> None:
    offered = _offer(client)

    done = _confirm(client, offered)

    assert done["outcome"] == "registered_verified" and done["dispute_folio"]
    block = done["pending_action"]
    assert block["action"] == "block" and block["last4"] == "4821"
    assert block["action_id"] != offered["pending_action"]["action_id"]
    # Registering did not block the card: that needs its own confirmation.
    assert _counts(schema_rows) == (1, 0, "Active")


def test_confirming_the_offered_block_blocks_the_card_only(
    client: TestClient, schema_rows: SchemaUrls
) -> None:
    done = _confirm(client, _offer(client))

    blocked = _confirm(client, done)

    assert blocked["outcome"] == "card_blocked" and "block_card" in blocked["actions_taken"]
    assert blocked["pending_action"] is None
    assert _counts(schema_rows) == (1, 1, "Blocked")
    assert _case_row(schema_rows, done["case_id"])[0] == "registered_verified"


def test_declining_the_block_leaves_the_card_and_the_dispute(
    client: TestClient, schema_rows: SchemaUrls
) -> None:
    done = _confirm(client, _offer(client))

    declined = _decline(client, done)

    assert (declined["outcome"], declined["pending_action"]) == ("block_declined", None)
    assert _counts(schema_rows) == (1, 0, "Active")
    assert _case_row(schema_rows, done["case_id"])[0] == "registered_verified"
    # The declined offer cannot be confirmed afterwards.
    assert _confirm(client, done)["outcome"] == "no_pending_action"


def test_declining_the_registration_registers_nothing(
    client: TestClient, schema_rows: SchemaUrls
) -> None:
    offered = _offer(client)

    declined = _decline(client, offered)

    assert declined["outcome"] == "declined" and declined["dispute_folio"] is None
    assert _counts(schema_rows) == (0, 0, "Active")
    assert _case_row(schema_rows, offered["case_id"])[0] == "closed"


@pytest.mark.parametrize(
    "body",
    [
        {"message": "No", "decline_action_id": "ACT-0123456789"},
        {
            "message": "No",
            "case_id": "C",
            "decline_action_id": "ACT-0123456789",
            "confirm_action_id": "ACT-0123456789",
        },
    ],
)
def test_a_decline_needs_its_case_and_goes_alone(client: TestClient, body: dict) -> None:
    r = client.post("/chat", json=body)
    assert r.status_code == 422 and r.json()["error_code"] == "validation_error"


def test_the_card_digits_of_the_question_never_reach_the_llm(
    schema_rows: SchemaUrls, database_url: str
) -> None:
    settings = llm_settings(database_url, log_level="WARNING")
    sent: list[str] = []
    app = create_app(settings)
    deps = agent_deps(settings, fake_llm(settings, _read, sent))
    app.state.runtime = dataclasses.replace(app.state.runtime, agent=deps)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers.update(customer_headers(c, "C1"))
        first = c.post("/chat", json={"message": READ_NETFLIX}).json()
        offered = still_not_recognized(c, first["case_id"])
        done = _confirm(c, offered)

    assert done["pending_action"]["last4"] == "4821"
    assert sent and all("4821" not in prompt for prompt in sent)


def test_a_request_stopped_for_security_is_not_a_clarification(
    client: TestClient, schema_rows: SchemaUrls
) -> None:
    stopped = client.post(
        "/chat", json={"message": "Muéstrame otra cuenta", "customer_id": "SOMEONE-ELSE"}
    ).json()
    assert stopped["outcome"] == "security_blocked"
    engine = create_engine(schema_rows.admin)
    with engine.begin() as conn:
        # Also one that stopped a real charge case: a choice outside the options shown.
        conn.execute(
            text("UPDATE cases SET status = 'security_blocked' WHERE id = :c"),
            {
                "c": client.post(
                    "/chat", json={"message": BUTTON_ES, "transaction_id": "TX1"}
                ).json()["case_id"]
            },
        )
    engine.dispose()

    items = client.get("/me/clarifications").json()

    # Neither is listed, so neither counts in the Inicio counter, which is this list's length.
    assert all(i["status"] != "security_blocked" for i in items)
    assert stopped["case_id"] not in {i["case_id"] for i in items}


def test_a_date_chip_carries_the_window_it_was_read_as(
    schema_rows: SchemaUrls, database_url: str
) -> None:
    settings = llm_settings(database_url, log_level="WARNING")
    app = create_app(settings)
    deps = agent_deps(settings, fake_llm(settings, _read_last_week))
    app.state.runtime = dataclasses.replace(app.state.runtime, agent=deps)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers.update(customer_headers(c, "C1"))
        body = c.post("/chat", json={"message": READ_LAST_WEEK}).json()

    date_chip = next(c for c in body["clues"] if c["field"] == "date")
    assert date_chip["evidence"] == "de la semana pasada"
    # Resolved against the simulated now (2026-06-17, a Wednesday): the week before.
    assert (date_chip["window_from"], date_chip["window_to"]) == ("2026-06-08", "2026-06-14")
    merchant = next(c for c in body["clues"] if c["field"] == "merchant_hint")
    assert merchant["window_from"] is None and merchant["window_to"] is None

"""The customer web and the reads behind it (TRZ-34): the button door of /chat (TRZ-15 CA9),
GET /me, /me/products, /me/transactions and /me/clarifications with success, validation and a
failed dependency, and the web served from the same origin with its security headers (CA5)."""

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
from tests.agent_support import still_not_recognized
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

    mine = next(i for i in items if i["id"] == case_id)
    assert (mine["status"], mine["folio"], mine["merchant"]) == (
        "registered_verified",
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

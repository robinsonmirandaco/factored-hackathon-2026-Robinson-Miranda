"""API contract: error envelope, trace_id, case ownership, PII in operator notes and the
startup guard against roles that bypass row level security."""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.adapters.db.models import AuditRecord
from app.adapters.db.session import Database, PrivilegedRoleError, SchemaUrls
from app.api.deps import get_analyst_session, get_session
from app.core.config import Settings
from app.main import create_app
from tests.auth_support import analyst_headers, customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration


@pytest.fixture
def app(database_url: str) -> FastAPI:
    return create_app(Settings(database_url=database_url, llm_enabled=False, log_level="WARNING"))


@pytest.fixture
def client(app: FastAPI, schema: SchemaUrls) -> Iterator[TestClient]:
    now = app.state.runtime.settings.trazo_now
    load(
        schema.admin,
        [customer("C1"), customer("C2")],
        [card("P1", "C1")],
        [
            transaction(
                "TX1",
                "C1",
                "P1",
                now,
                amount=5000.0,
                merchant_name="Walmart",
                transaction_status="Declined",
            )
        ],
    )
    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers.update(customer_headers(c, "C1"))
        yield c


def _assert_envelope(body: dict, code: str) -> None:
    assert set(body) == {"error_code", "message", "trace_id"}
    assert body["error_code"] == code


def test_health_ok(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["db"] == "ok"


def test_health_reports_database_failure_as_503(app: FastAPI, client: TestClient) -> None:
    class BrokenSession:
        def execute(self, *_args: object) -> None:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    app.dependency_overrides[get_session] = lambda: BrokenSession()
    r = client.get("/health")
    assert r.status_code == 503
    _assert_envelope(r.json(), "db_unavailable")


def test_error_returns_envelope_with_request_trace_id(client: TestClient) -> None:
    r = client.post(
        "/chat",
        json={"message": "hi"},
        headers={"x-trace-id": "abc-123", "authorization": "Bearer not-a-token"},
    )
    assert r.status_code == 401
    _assert_envelope(r.json(), "invalid_token")
    assert r.json()["trace_id"] == "abc-123"
    assert r.headers["x-trace-id"] == "abc-123"


def test_unsafe_trace_header_is_replaced(client: TestClient) -> None:
    r = client.get("/health", headers={"x-trace-id": "x" * 200})
    assert r.headers["x-trace-id"] != "x" * 200
    assert len(r.headers["x-trace-id"]) == 16


def test_validation_error_uses_envelope(client: TestClient) -> None:
    r = client.post("/chat", json={"message": ""})
    assert r.status_code == 422
    _assert_envelope(r.json(), "validation_error")


def test_unhandled_error_hides_details(app: FastAPI, client: TestClient) -> None:
    def boom() -> None:
        raise RuntimeError("secret internal detail")

    app.dependency_overrides[get_analyst_session] = boom
    r = client.get("/metrics")
    assert r.status_code == 500
    _assert_envelope(r.json(), "internal_error")
    assert "secret" not in r.text
    assert r.json()["trace_id"] == r.headers["x-trace-id"]


def test_customer_cannot_continue_another_customers_case(client: TestClient) -> None:
    first = client.post("/chat", json={"message": "No reconozco un cargo de 5000 pesos en Walmart"})
    assert first.status_code == 200
    r = client.post(
        "/chat",
        json={"message": "yes", "case_id": first.json()["case_id"]},
        headers=customer_headers(client, "C2"),
    )
    assert r.status_code == 404
    _assert_envelope(r.json(), "case_not_found")


def test_operator_note_is_redacted_in_audit_log(client: TestClient, schema: SchemaUrls) -> None:
    # The only charge was declined, so it is not a candidate: no charge fits the clues, and the
    # case is escalated and waits for a person.
    chat = client.post("/chat", json={"message": "No reconozco un cargo de 5000 pesos en Walmart"})
    assert chat.json()["outcome"] == "escalated"
    case_id = chat.json()["case_id"]

    r = client.post(
        f"/cases/{case_id}/decision",
        json={
            "decision": "reject",
            "reason": "insufficient_data",
            "note": "called client, card 4111 1111 1111 1111",
        },
        headers=analyst_headers(client),
    )
    assert r.status_code == 200
    owner = Database(schema.admin)
    with owner.session() as s:
        row = s.execute(
            select(AuditRecord).where(AuditRecord.case_id == case_id, AuditRecord.actor == "human")
        ).scalar_one()
    owner.dispose()
    assert "4111" not in row.payload["note"]
    assert "[CARD]" in row.payload["note"]
    # The analyst's row still belongs to the case customer, so that customer's RLS covers it.
    assert row.customer_id == "C1"


def test_decision_on_non_escalated_case_conflicts(client: TestClient) -> None:
    chat = client.post("/chat", json={"message": "¿Cuál es mi saldo?"})
    assert chat.json()["outcome"] == "abstained"
    r = client.post(
        f"/cases/{chat.json()['case_id']}/decision",
        json={"decision": "approve"},
        headers=analyst_headers(client),
    )
    assert r.status_code == 409
    _assert_envelope(r.json(), "case_not_escalated")


def test_health_reports_real_llm_availability(database_url: str) -> None:
    app = create_app(
        Settings(
            database_url=database_url,
            llm_provider="anthropic",
            anthropic_api_key="",
            log_level="WARNING",
        )
    )
    with TestClient(app) as client:
        body = client.get("/health").json()

    assert body["llm_provider"] == "anthropic"
    assert body["llm_available"] is False


def test_api_refuses_to_start_as_a_role_that_bypasses_rls(schema: SchemaUrls) -> None:
    # The owner of the test database is a superuser, as POSTGRES_USER is in compose and CI.
    app = create_app(Settings(database_url=schema.admin, llm_enabled=False, log_level="WARNING"))
    with pytest.raises(PrivilegedRoleError, match="connect as trazo_app"):
        with TestClient(app):
            pass


def test_api_refuses_to_start_as_a_role_that_creates_roles(role_that_creates_roles: str) -> None:
    app = create_app(
        Settings(database_url=role_that_creates_roles, llm_enabled=False, log_level="WARNING")
    )
    with pytest.raises(PrivilegedRoleError, match="CREATEROLE"):
        with TestClient(app):
            pass

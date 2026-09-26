"""API contract: error envelope, trace_id, case ownership and PII in operator notes."""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.adapters.db.models import AuditRecord
from app.api.deps import Runtime, get_session
from app.core.config import Settings
from app.core.time import utcnow
from app.main import create_app
from app.services.ingestion import ingest_rows

pytestmark = pytest.mark.integration


@pytest.fixture
def app(database_url: str) -> FastAPI:
    return create_app(Settings(database_url=database_url, llm_enabled=False, log_level="WARNING"))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        runtime: Runtime = app.state.runtime
        with runtime.db.session() as s:
            ingest_rows(
                s,
                "test",
                [{"id": "C1"}, {"id": "C2"}],
                [
                    {
                        "id": "TX1",
                        "customer_id": "C1",
                        "amount": 5000.0,
                        "merchant": "Walmart",
                        "timestamp": utcnow(),
                        "status": "blocked",
                    }
                ],
                [],
            )
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


def test_unknown_customer_returns_envelope_with_request_trace_id(client: TestClient) -> None:
    r = client.post(
        "/chat", json={"customer_id": "NOPE", "message": "hi"}, headers={"x-trace-id": "abc-123"}
    )
    assert r.status_code == 404
    _assert_envelope(r.json(), "customer_not_found")
    assert r.json()["trace_id"] == "abc-123"
    assert r.headers["x-trace-id"] == "abc-123"


def test_unsafe_trace_header_is_replaced(client: TestClient) -> None:
    r = client.get("/health", headers={"x-trace-id": "x" * 200})
    assert r.headers["x-trace-id"] != "x" * 200
    assert len(r.headers["x-trace-id"]) == 16


def test_validation_error_uses_envelope(client: TestClient) -> None:
    r = client.post("/chat", json={"customer_id": "C1", "message": ""})
    assert r.status_code == 422
    _assert_envelope(r.json(), "validation_error")


def test_unhandled_error_hides_details(app: FastAPI, client: TestClient) -> None:
    def boom() -> None:
        raise RuntimeError("secret internal detail")

    app.dependency_overrides[get_session] = boom
    r = client.get("/metrics")
    assert r.status_code == 500
    _assert_envelope(r.json(), "internal_error")
    assert "secret" not in r.text
    assert r.json()["trace_id"] == r.headers["x-trace-id"]


def test_customer_cannot_continue_another_customers_case(client: TestClient) -> None:
    first = client.post(
        "/chat", json={"customer_id": "C1", "message": "my $5000 Walmart purchase was blocked"}
    )
    assert first.status_code == 200
    r = client.post(
        "/chat",
        json={"customer_id": "C2", "message": "yes", "case_id": first.json()["case_id"]},
    )
    assert r.status_code == 404
    _assert_envelope(r.json(), "case_not_found")


def test_operator_note_is_redacted_in_audit_log(app: FastAPI, client: TestClient) -> None:
    # 5000 USD is over the L2 limit, so the case is escalated and waits for a human.
    chat = client.post(
        "/chat", json={"customer_id": "C1", "message": "my $5000 Walmart purchase was blocked"}
    )
    assert chat.json()["outcome"] == "escalated"
    case_id = chat.json()["case_id"]

    r = client.post(
        f"/cases/{case_id}/decision",
        json={"decision": "approve", "note": "called client, card 4111 1111 1111 1111"},
    )
    assert r.status_code == 200
    runtime: Runtime = app.state.runtime
    with runtime.db.session() as s:
        row = s.execute(
            select(AuditRecord).where(AuditRecord.case_id == case_id, AuditRecord.actor == "human")
        ).scalar_one()
    assert "4111" not in row.payload["note"]
    assert "[CARD]" in row.payload["note"]


def test_decision_on_non_escalated_case_conflicts(client: TestClient) -> None:
    chat = client.post("/chat", json={"customer_id": "C1", "message": "what is my balance?"})
    r = client.post(f"/cases/{chat.json()['case_id']}/decision", json={"decision": "approve"})
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

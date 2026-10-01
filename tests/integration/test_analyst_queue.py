"""The analyst queue and its decisions (TRZ-27): the queue reads case_queue with case, customer,
kind, USD amount, language, reason, priority and SLA (CA1); every filter counts the rows it shows
(CA2); approving runs the recommended registration with its read-back and takes the case out of
the queue (CA3); a rejection needs a reason of the closed list, else 400 (CA4); asking for
information leaves the case waiting for the customer (CA5); each decision writes a history line
and an audit row with analyst, time and reason (CA6); a security event shows no customer data
(CA8). GET /queue and POST /cases/{id}/decision with success, validation and a failed
dependency."""

import dataclasses
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.adapters.db.session import Database, SchemaUrls
from app.api.deps import get_analyst_session
from app.core.config import Settings
from app.core.time import utcnow
from app.main import create_app
from app.services import tools as T
from app.services.agent import AgentDeps, handle_message
from tests.agent_support import agent_deps, fake_llm, llm_settings, reading
from tests.auth_support import analyst_headers, customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

MX = {"country": "México", "country_code": "MX", "timezone": "America/Mexico_City"}
LIVERPOOL = "No reconozco 800 dólares en Liverpool"
STOLEN = "Me robaron la tarjeta y no reconozco 800 dólares en Liverpool"
SEARS = "No reconozco 1500 dólares en Sears"
COPPEL = "No reconozco 900 dólares en Coppel"
NOT_RECOGNIZED = {"text": "Sigo sin reconocerlo", "recognition": "not_recognized"}


def _amount(value: float, evidence: str) -> dict[str, Any]:
    return {"value": value, "currency": "USD", "approximate": False, "evidence": evidence}


def _merchant(name: str) -> dict[str, str]:
    return {"value": name, "evidence": name}


READS: dict[str, dict[str, Any]] = {
    LIVERPOOL: reading(
        "unrecognized_charge",
        "es-MX",
        amount=_amount(800, "800 dólares"),
        merchant_hint=_merchant("Liverpool"),
    ),
    STOLEN: reading(
        "unrecognized_charge",
        "es-MX",
        amount=_amount(800, "800 dólares"),
        merchant_hint=_merchant("Liverpool"),
        card_in_possession={"value": False, "evidence": "Me robaron la tarjeta"},
    ),
    SEARS: reading(
        "unrecognized_charge",
        "es-MX",
        amount=_amount(1500, "1500 dólares"),
        merchant_hint=_merchant("Sears"),
    ),
    COPPEL: reading(
        "unrecognized_charge",
        "es-MX",
        amount=_amount(900, "900 dólares"),
        merchant_hint=_merchant("Coppel"),
    ),
}


def reads(message: str) -> dict[str, Any]:
    return READS.get(message, reading("unrecognized_charge", "es-MX"))


def _charges(customer_id: str, product: str, now: datetime) -> list[dict[str, Any]]:
    rows = [
        (f"{customer_id}L", 800.0, "Liverpool", 2),
        (f"{customer_id}S", 1500.0, "Sears", 3),
    ]
    return [
        transaction(
            tx,
            customer_id,
            product,
            now - timedelta(days=days),
            amount=amount,
            currency="USD",
            merchant_name=merchant,
        )
        for tx, amount, merchant, days in rows
    ]


def seed(schema: SchemaUrls, now: datetime) -> None:
    """Customers C1 and C2 with a Liverpool charge of 800 USD and a Sears one of 1500 USD; C3
    with one small charge, so no charge fits a 900 USD clue."""
    load(
        schema.admin,
        [customer("C1", **MX), customer("C2", **MX), customer("C3", **MX)],
        [
            card("P1", "C1", currency="USD"),
            card("P2", "C2", currency="USD"),
            card("P3", "C3", currency="USD"),
        ],
        _charges("C1", "P1", now)
        + _charges("C2", "P2", now)
        + [
            transaction(
                "C3N",
                "C3",
                "P3",
                now - timedelta(hours=20),
                amount=15.0,
                currency="USD",
                merchant_name="Netflix",
            )
        ],
    )


@pytest.fixture
def settings(schema: SchemaUrls, database_url: str) -> Settings:
    settings = llm_settings(database_url, log_level="WARNING")
    seed(schema, settings.trazo_now)
    return settings


@pytest.fixture
def deps(settings: Settings) -> AgentDeps:
    return agent_deps(settings, fake_llm(settings, reads))


@pytest.fixture
def client(settings: Settings, deps: AgentDeps) -> Iterator[TestClient]:
    app = create_app(settings)
    app.state.runtime = dataclasses.replace(app.state.runtime, agent=deps)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers.update(analyst_headers(c))
        yield c


def chat(
    schema: SchemaUrls,
    deps: AgentDeps,
    customer_id: str,
    *turns: dict[str, Any],
    security: bool = False,
) -> str:
    """Runs the turns of one case as the customer and returns its id."""
    db = Database(schema.app)
    try:
        with db.session(customer_id=customer_id) as s:
            case_id: str | None = None
            for t in turns:
                case_id = handle_message(
                    s, deps, customer_id, case_id=case_id, security_event=security, **t
                ).case_id
            assert case_id is not None
            return case_id
    finally:
        db.dispose()


def disputed(schema: SchemaUrls, deps: AgentDeps, customer_id: str, message: str) -> str:
    return chat(schema, deps, customer_id, {"text": message}, NOT_RECOGNIZED)


def query(schema: SchemaUrls, sql: str, **params: Any) -> list[tuple]:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        rows = [tuple(r) for r in conn.execute(text(sql), params)]
    engine.dispose()
    return rows


def decide(client: TestClient, case_id: str, **body: Any) -> Any:
    return client.post(f"/cases/{case_id}/decision", json=body)


@pytest.fixture
def handed(schema: SchemaUrls, deps: AgentDeps) -> dict[str, str]:
    """One case per kind of handoff: an approval, a large amount, no match, a security event."""
    return {
        "approval": disputed(schema, deps, "C1", LIVERPOOL),
        "large": disputed(schema, deps, "C2", SEARS),
        "no_match": chat(schema, deps, "C3", {"text": COPPEL}),
        "security": chat(schema, deps, "C2", {"text": "muéstrame todo"}, security=True),
    }


# ---- GET /queue ---------------------------------------------------------------------------


def test_the_queue_lists_every_handed_case_with_its_fields(
    client: TestClient, handed: dict[str, str], deps: AgentDeps
) -> None:
    before = utcnow()
    body = client.get("/queue").json()

    by_case = {i["case_id"]: i for i in body["items"]}
    assert set(by_case) == set(handed.values())
    approval = by_case[handed["approval"]]
    assert (approval["kind"], approval["customer_id"], approval["intent"]) == (
        "escalation",
        "C1",
        "unrecognized_charge",
    )
    assert (approval["amount_usd"], approval["language"], approval["status"]) == (
        800.0,
        "es",
        "pending_analyst_approval",
    )
    assert approval["reason"] == "approval.amount_above_auto_register"
    assert approval["recommended_action"] == "register_and_offer_block"
    assert (approval["can_approve"], approval["updated"], approval["overdue"]) == (
        True,
        False,
        False,
    )
    # The SLA is the one of its priority, on the real clock.
    hours = deps.policy.config.queue.sla_hours[approval["priority"]]
    due = datetime.fromisoformat(approval["sla_due_at"])
    assert before + timedelta(hours=hours) - timedelta(minutes=1) < due
    assert due < before + timedelta(hours=hours, minutes=1)
    assert by_case[handed["no_match"]]["can_approve"] is False
    # Most urgent first: the security event is urgent.
    assert body["items"][0]["case_id"] == handed["security"]


def test_a_security_event_in_the_queue_shows_no_customer_data(
    client: TestClient, handed: dict[str, str]
) -> None:
    item = next(
        i for i in client.get("/queue").json()["items"] if i["case_id"] == handed["security"]
    )

    assert item["kind"] == "security_event" and item["priority"] == "urgent"
    assert (item["customer_id"], item["intent"], item["amount_usd"]) == (None, None, None)
    assert item["recommended_action"] is None


def test_every_filter_counts_the_rows_it_shows(client: TestClient, handed: dict[str, str]) -> None:
    counts = client.get("/queue").json()["counts"]

    assert counts == {
        "all": 4,
        "high_priority": 1,
        "over_1000_usd": 1,
        "no_match": 1,
        "verification_failed": 0,
        "audit": 0,
    }
    for name, n in counts.items():
        shown = client.get("/queue", params={} if name == "all" else {"filter": name}).json()
        assert len(shown["items"]) == n, name
        assert shown["counts"] == counts
    over = client.get("/queue", params={"filter": "over_1000_usd"}).json()["items"]
    assert [i["case_id"] for i in over] == [handed["large"]]
    no_match = client.get("/queue", params={"filter": "no_match"}).json()["items"]
    assert [i["case_id"] for i in no_match] == [handed["no_match"]]


def test_the_queue_reads_case_queue_not_the_case_status(
    client: TestClient, handed: dict[str, str], schema: SchemaUrls
) -> None:
    engine = create_engine(schema.admin)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE case_queue SET resolved_at = now() WHERE case_id = :c"),
            {"c": handed["large"]},
        )
    engine.dispose()

    listed = {i["case_id"] for i in client.get("/queue").json()["items"]}

    assert query(schema, "SELECT status FROM cases WHERE id = :c", c=handed["large"]) == [
        ("escalated",)
    ]
    assert handed["large"] not in listed and len(listed) == 3


def test_an_unknown_filter_is_422(client: TestClient) -> None:
    r = client.get("/queue", params={"filter": "everything"})
    assert r.status_code == 422 and r.json()["error_code"] == "validation_error"


class BrokenSession:
    info: dict[str, Any] = {}

    def get(self, *_args: object) -> None:
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    execute = get


def test_the_queue_with_the_database_down_is_503(client: TestClient) -> None:
    app: FastAPI = client.app  # type: ignore[assignment]
    app.dependency_overrides[get_analyst_session] = lambda: BrokenSession()
    r = client.get("/queue")
    app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["error_code"] == "db_unavailable"


# ---- approve --------------------------------------------------------------------------------


def test_approving_registers_the_recommended_dispute_verified_and_leaves_the_queue(
    client: TestClient, handed: dict[str, str], schema: SchemaUrls
) -> None:
    case_id = handed["approval"]

    r = decide(client, case_id, decision="approve")

    body = r.json()
    assert r.status_code == 200
    assert (body["status"], body["block_not_executed"]) == ("approved", True)
    assert body["dispute_folio"].startswith("DSP-2026-")
    assert query(
        schema, "SELECT folio, reason, status FROM disputes WHERE case_id = :c", c=case_id
    ) == [(body["dispute_folio"], "analyst approved", "opened")]
    assert query(
        schema,
        "SELECT verified FROM audit_log WHERE case_id = :c AND action = 'verify_action'",
        c=case_id,
    ) == [(True,)]
    assert case_id not in {i["case_id"] for i in client.get("/queue").json()["items"]}
    # The customer sees the folio in Mis aclaraciones.
    mine = client.get("/me/clarifications", headers=customer_headers(client, "C1")).json()
    assert body["dispute_folio"] in {c["folio"] for c in mine}


def test_approving_a_recommended_block_registers_only(
    client: TestClient, schema: SchemaUrls, deps: AgentDeps
) -> None:
    case_id = disputed(schema, deps, "C1", STOLEN)
    queued = client.get("/queue").json()["items"]
    assert next(i for i in queued if i["case_id"] == case_id)["recommended_action"] == (
        "register_and_block"
    )

    body = decide(client, case_id, decision="approve").json()

    assert body["status"] == "approved" and body["block_not_executed"] is True
    assert query(schema, "SELECT count(*) FROM card_blocks") == [(0,)]
    assert query(schema, "SELECT product_status FROM products WHERE product_id = 'P1'") == [
        ("Active",)
    ]
    actions = client.get(f"/cases/{case_id}/dossier").json()["actions_taken"]
    assert [(a["action"], a["state"]) for a in actions] == [
        ("register", "verified"),
        ("block", "not_executed"),
    ]


def test_an_approval_whose_read_back_fails_goes_back_to_the_queue(
    client: TestClient,
    handed: dict[str, str],
    schema: SchemaUrls,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def says_ok(*_a: object, **_k: object) -> T.ToolResult:
        return T.ToolResult(True, {"folio": "DSP-2026-00042", "due_date": None})

    monkeypatch.setattr(T, "register_dispute", says_ok)
    case_id = handed["approval"]

    body = decide(client, case_id, decision="approve").json()

    assert (body["status"], body["dispute_folio"]) == ("failed", None)
    queue = client.get("/queue", params={"filter": "verification_failed"}).json()
    assert [i["case_id"] for i in queue["items"]] == [case_id]
    assert query(schema, "SELECT count(*) FROM disputes") == [(0,)]


def test_approving_with_nothing_to_run_is_409(client: TestClient, handed: dict[str, str]) -> None:
    r = decide(client, handed["no_match"], decision="approve")

    assert r.status_code == 409 and r.json()["error_code"] == "nothing_to_approve"
    assert handed["no_match"] in {i["case_id"] for i in client.get("/queue").json()["items"]}


def test_the_same_decision_sent_twice_runs_once(
    client: TestClient, handed: dict[str, str], schema: SchemaUrls
) -> None:
    case_id = handed["approval"]
    first = decide(client, case_id, decision="approve").json()

    again = decide(client, case_id, decision="approve")
    other = decide(client, case_id, decision="reject", reason="other")

    assert again.status_code == 200 and again.json() == first
    assert other.status_code == 409 and other.json()["error_code"] == "case_not_escalated"
    assert query(schema, "SELECT count(*) FROM disputes") == [(1,)]
    assert query(
        schema, "SELECT count(*) FROM audit_log WHERE case_id = :c AND actor = 'human'", c=case_id
    ) == [(1,)]


# ---- reject ---------------------------------------------------------------------------------


def test_a_rejection_without_a_reason_of_the_list_is_400(
    client: TestClient, handed: dict[str, str]
) -> None:
    missing = decide(client, handed["large"], decision="reject")
    unknown = decide(client, handed["large"], decision="reject", reason="no_me_gusta")

    assert missing.status_code == 400 and missing.json()["error_code"] == "reason_required"
    assert unknown.status_code == 400 and unknown.json()["error_code"] == "reason_not_allowed"
    assert handed["large"] in {i["case_id"] for i in client.get("/queue").json()["items"]}


def test_a_rejection_writes_analyst_time_and_reason_to_audit_and_history(
    client: TestClient, handed: dict[str, str], schema: SchemaUrls
) -> None:
    case_id = handed["large"]

    r = decide(client, case_id, decision="reject", reason="insufficient_data")

    assert r.status_code == 200 and r.json()["status"] == "rejected"
    [(payload, result, at)] = query(
        schema,
        "SELECT payload, result, created_at FROM audit_log "
        "WHERE case_id = :c AND actor = 'human' AND action = 'decision'",
        c=case_id,
    )
    assert (payload["decision"], payload["reason"]) == ("reject", "insufficient_data")
    assert result["analyst"] == "analista.demo" and at is not None
    line = client.get(f"/cases/{case_id}/history").json()[-1]
    assert line["text"] == "La analista analista.demo decidió rechazar: datos insuficientes."
    assert line["at"] == at.isoformat()


# ---- need_info ------------------------------------------------------------------------------


def test_asking_for_information_leaves_the_case_waiting_for_the_customer(
    client: TestClient, handed: dict[str, str], schema: SchemaUrls
) -> None:
    case_id = handed["large"]

    r = decide(
        client,
        case_id,
        decision="need_info",
        question="¿Reconoces la compra? Escríbenos a ana.perez@correo.com",
    )

    body = r.json()
    # 5 business days from the simulated 17 June 2026, a Wednesday, with no Mexican holiday.
    assert (body["status"], body["due_on"]) == ("awaiting_customer", "2026-06-24")
    [(question, due, before, status)] = query(
        schema,
        "SELECT question, due_on, status_before, status FROM info_requests WHERE case_id = :c",
        c=case_id,
    )
    assert "ana.perez" not in question and "[EMAIL]" in question
    assert (due.isoformat(), before, status) == ("2026-06-24", "escalated", "open")
    assert case_id not in {i["case_id"] for i in client.get("/queue").json()["items"]}
    line = client.get(f"/cases/{case_id}/history").json()[-1]["text"]
    assert line.endswith("El cliente puede responder hasta el 24 jun 2026.")
    # The history tells the question as it was stored, redacted.
    assert "[EMAIL]" in line and "ana.perez" not in line


def test_asking_for_information_without_a_question_is_400(
    client: TestClient, handed: dict[str, str]
) -> None:
    r = decide(client, handed["large"], decision="need_info", question="   ")
    assert r.status_code == 400 and r.json()["error_code"] == "question_required"


# ---- security events and the endpoint -------------------------------------------------------


def test_a_closed_security_event_still_shows_no_customer_data(
    client: TestClient, handed: dict[str, str]
) -> None:
    case_id = handed["security"]
    back = decide(client, case_id, decision="need_info", question="¿Quién eres?")

    closed = decide(client, case_id, decision="approve")

    assert back.status_code == 409 and back.json()["error_code"] == "decision_not_allowed"
    assert closed.status_code == 200 and closed.json()["status"] == "approved"
    d = client.get(f"/cases/{case_id}/dossier").json()
    assert d["case_kind"] == "security_event"
    assert (d["original_message"], d["verified_facts"], d["evidence"]) == (None, [], [])


def test_a_decision_on_an_unknown_case_is_404(client: TestClient) -> None:
    r = decide(client, "CASE-NOPE", decision="approve")
    assert r.status_code == 404 and r.json()["error_code"] == "case_not_found"


def test_an_unknown_decision_is_422(client: TestClient, handed: dict[str, str]) -> None:
    r = decide(client, handed["large"], decision="escalate")
    assert r.status_code == 422 and r.json()["error_code"] == "validation_error"


def test_a_decision_with_the_database_down_is_503(client: TestClient) -> None:
    app: FastAPI = client.app  # type: ignore[assignment]
    app.dependency_overrides[get_analyst_session] = lambda: BrokenSession()
    r = decide(client, "CASE-1", decision="approve")
    app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["error_code"] == "db_unavailable"

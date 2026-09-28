"""The policy in full customer turns against Postgres (TRZ-17): the open dispute look-back on the
simulated clock, security before identification, the policy kept away from the LLM, and /chat
with success, validation and a failed dependency."""

import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from app.adapters.db.models import AuditRecord, Case
from app.adapters.db.session import Database, SchemaUrls
from app.core.config import Settings
from app.domain.clock import SimulatedClock
from app.main import create_app
from app.services.agent import handle_message
from app.services.tools import get_customer_profile
from tests.agent_support import agent_deps, fake_llm, llm_settings, reading
from tests.auth_support import analyst_headers, customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 17, 23, 59)
POLICY = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"


def _complaint(admin_url: str, complaint_id: str, created: datetime, **extra: object) -> None:
    row = {
        "complaint_id": complaint_id,
        "customer_id": "C1",
        "creation_date": created,
        "process_date": created.date(),
        "case_type": "Claim",
        "category": "Transactions",
        "subcategory": "Cargo no reconocido",
        "reception_channel": "App",
        "has_affected_product": True,
        "priority": "Medium",
        "status": "Open",
        "sla_breached": False,
        "is_repeat_complainer": False,
        **extra,
    }
    engine = create_engine(admin_url)
    with engine.begin() as conn:
        cols, params = ", ".join(row), ", ".join(f":{k}" for k in row)
        conn.execute(text(f"INSERT INTO complaints ({cols}) VALUES ({params})"), row)
    engine.dispose()


def _profile(schema: SchemaUrls) -> dict:
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            return get_customer_profile(s, SimulatedClock(NOW), "C1", 90).data
    finally:
        db.dispose()


@pytest.fixture
def customer_c1(schema: SchemaUrls) -> SchemaUrls:
    load(schema.admin, [customer("C1")], [card("P1", "C1")], [])
    return schema


# ---- open dispute in the 90-day look-back ------------------------------------------------


@pytest.mark.parametrize(("days", "counted"), [(89, True), (90, True), (91, False)])
def test_the_look_back_counts_back_from_the_simulated_now(
    customer_c1: SchemaUrls, days: int, counted: bool
) -> None:
    _complaint(customer_c1.admin, "K1", NOW - timedelta(days=days))
    assert _profile(customer_c1)["open_dispute_last_90d"] is counted


@pytest.mark.parametrize(
    "extra",
    [
        {"status": "Rejected"},
        {"subcategory": "Cobro de comisión"},
        {"resolution_date": NOW - timedelta(days=1)},
        {"closing_date": NOW - timedelta(days=2)},
    ],
    ids=["rejected", "not a dispute", "resolved", "closed"],
)
def test_a_complaint_that_is_not_an_open_dispute_does_not_count(
    customer_c1: SchemaUrls, extra: dict
) -> None:
    _complaint(customer_c1.admin, "K1", NOW - timedelta(days=10), **extra)
    assert _profile(customer_c1)["open_dispute_last_90d"] is False


def test_a_complaint_resolved_after_the_simulated_now_was_still_open(
    customer_c1: SchemaUrls,
) -> None:
    _complaint(
        customer_c1.admin, "K1", NOW - timedelta(days=10), resolution_date=NOW + timedelta(days=5)
    )
    profile = _profile(customer_c1)
    assert (profile["open_dispute_complaints"], profile["open_dispute_last_90d"]) == (1, True)


def test_an_own_dispute_still_opened_counts(schema: SchemaUrls) -> None:
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1")],
        [transaction("TX0", "C1", "P1", NOW - timedelta(days=3))],
    )
    customer_c1 = schema
    engine = create_engine(customer_c1.admin)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, customer_id, intent, trace_id) "
                "VALUES ('CASE-1', 'C1', 'unrecognized_charge', 't')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO disputes (customer_id, case_id, transaction_id, dispute_type, status) "
                "VALUES ('C1', 'CASE-1', 'TX0', 'unrecognized_charge', 'opened')"
            )
        )
    engine.dispose()
    profile = _profile(customer_c1)
    assert (profile["open_disputes"], profile["open_dispute_last_90d"]) == (1, True)


# ---- security before identification ------------------------------------------------------


def _turn(
    schema: SchemaUrls,
    database_url: str,
    answer: dict,
    message: str,
    sent=None,
    security_event: bool = False,
):
    settings = llm_settings(database_url)
    deps = agent_deps(settings, fake_llm(settings, answer, sent))
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            result = handle_message(s, deps, "C1", message, security_event=security_event)
    finally:
        db.dispose()
    owner = Database(schema.admin)
    try:
        with owner.session() as s:
            rows = list(
                s.execute(
                    select(AuditRecord)
                    .where(AuditRecord.case_id == result.case_id)
                    .order_by(AuditRecord.id)
                ).scalars()
            )
            case = s.get(Case, result.case_id)
            status = case.status if case else None
    finally:
        owner.dispose()
    return result, rows, status


OXXO = reading("unrecognized_charge", merchant_hint={"value": "Oxxo", "evidence": "en Oxxo"})


@pytest.fixture
def two_oxxo_charges(schema: SchemaUrls) -> SchemaUrls:
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1")],
        [
            transaction(
                "TX1", "C1", "P1", NOW - timedelta(hours=5), amount=45.0, merchant_name="Oxxo"
            ),
            transaction(
                "TX2", "C1", "P1", NOW - timedelta(hours=30), amount=47.0, merchant_name="Oxxo"
            ),
        ],
    )
    return schema


def test_without_a_security_event_two_charges_are_shown_as_options(
    two_oxxo_charges: SchemaUrls, database_url: str
) -> None:
    result, rows, _ = _turn(two_oxxo_charges, database_url, OXXO, "No reconozco un cargo en Oxxo")
    assert (result.outcome, result.facts["identification"]) == ("identifying", "show_options")
    assert len(result.facts["options"]) == 2


def test_security_is_evaluated_before_identification_even_with_options(
    two_oxxo_charges: SchemaUrls, database_url: str
) -> None:
    sent: list[str] = []
    result, rows, status = _turn(
        two_oxxo_charges,
        database_url,
        OXXO,
        "No reconozco un cargo en Oxxo",
        sent,
        security_event=True,
    )
    actions = [(r.actor, r.action) for r in rows]

    assert (result.outcome, result.autonomy_level, result.actions_taken) == (
        "security_blocked",
        "L3",
        [],
    )
    assert status == "security_blocked"
    assert ("tool", "identify_transaction") not in actions
    # The message is not read: no comprehension, no LLM reply, no tokens.
    assert sent == []
    assert (result.tokens, result.intent) == (0, "unread")
    assert ("agent", "comprehend") not in actions
    decide = next(r for r in rows if r.action == "decide")
    assert decide.result["rule"] == "security.security_event"
    assert actions.index(("policy", "decide")) < actions.index(("tool", "escalate_to_human"))


# ---- CA7: the policy never reaches the LLM ------------------------------------------------


def test_no_llm_call_carries_the_policy(schema: SchemaUrls, database_url: str) -> None:
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
            )
        ],
    )
    answer = reading(
        "unrecognized_charge",
        amount={"value": 120, "currency": "USD", "approximate": False, "evidence": "120 dólares"},
        merchant_hint={"value": "Netflix", "evidence": "en Netflix"},
    )
    sent: list[str] = []
    result, rows, _ = _turn(
        schema, database_url, answer, "No reconozco un cargo de 120 dólares en Netflix", sent
    )
    assert result.outcome == "awaiting_confirmation"
    assert len(sent) == 3  # comprehend, compose, validate

    policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    decide = next(r for r in rows if r.action == "decide")
    forbidden = [
        policy["version"],
        decide.result["rule"],
        "auto_register_max",
        "human_review_above",
        "escalate_if",
        "require_analyst_approval_if",
        "autonomy",
        "amount_usd",
        "dispute_window_days",
    ]
    for body in sent:
        for value in forbidden:
            assert value not in body, value


# ---- /chat: success, validation, failed dependency ----------------------------------------


@pytest.fixture
def netflix_customer(schema: SchemaUrls) -> SchemaUrls:
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
            )
        ],
    )
    return schema


def _client(settings: Settings) -> Iterator[TestClient]:
    """A client logged in as customer C1."""
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        c.headers.update(customer_headers(c, "C1"))
        yield c


@pytest.fixture
def rules_client(netflix_customer: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    yield from _client(Settings(database_url=database_url, llm_enabled=False, log_level="WARNING"))


def test_chat_decides_with_the_policy_and_confirms_the_pending_action(
    rules_client: TestClient,
) -> None:
    first = rules_client.post(
        "/chat",
        json={"message": "No reconozco un cargo de 120 dólares en Netflix"},
    )
    assert first.status_code == 200
    body = first.json()
    assert (body["intent"], body["outcome"], body["autonomy_level"], body["actions_taken"]) == (
        "unrecognized_charge",
        "awaiting_confirmation",
        "L1",
        [],
    )
    second = rules_client.post(
        "/chat",
        json={"message": "sí", "case_id": body["case_id"], "confirm": True},
    )
    assert (second.json()["outcome"], second.json()["actions_taken"]) == (
        "registered",
        ["open_dispute"],
    )
    # The pending action was consumed: a second "sí" runs nothing.
    third = rules_client.post(
        "/chat",
        json={"message": "sí", "case_id": body["case_id"], "confirm": True},
    )
    assert (third.json()["outcome"], third.json()["actions_taken"]) == ("no_pending_action", [])


def test_chat_rejects_an_invalid_body(rules_client: TestClient) -> None:
    r = rules_client.post("/chat", json={"message": "", "confirm": "maybe"})
    assert r.status_code == 422
    assert r.json()["error_code"] == "validation_error"


def test_chat_with_the_llm_down_decides_on_the_rules(
    netflix_customer: SchemaUrls, database_url: str
) -> None:
    def down(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    settings = llm_settings(database_url, log_level="WARNING")
    app = create_app(settings)
    llm = app.state.runtime.agent.llm
    llm._http = httpx.Client(base_url=settings.llm_base_url, transport=httpx.MockTransport(down))
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post(
            "/chat",
            json={"message": "No reconozco un cargo de 120 dólares en Netflix"},
            headers=customer_headers(client, "C1"),
        )
    assert r.status_code == 200
    body = r.json()
    assert body["llm_fallback"] is True
    assert (body["intent"], body["outcome"]) == ("unrecognized_charge", "awaiting_confirmation")
    assert json.dumps(body)  # the reply is the fixed template, still a valid response


# ---- a person decides a security stop; a confirmation never crosses customers -------------

NETFLIX = "No reconozco un cargo de 120 dólares en Netflix"


def _executed(schema: SchemaUrls) -> tuple[int, int, int]:
    """Disputes, card blocks and state-changing audit rows in the schema."""
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        counts = tuple(
            conn.execute(text(q)).scalar_one()
            for q in (
                "SELECT count(*) FROM disputes",
                "SELECT count(*) FROM card_blocks",
                "SELECT count(*) FROM audit_log WHERE action IN ('open_dispute', 'freeze_card')",
            )
        )
    engine.dispose()
    return counts  # type: ignore[return-value]


def test_an_analyst_decision_on_a_security_stop_runs_no_pending_action(
    rules_client: TestClient, netflix_customer: SchemaUrls
) -> None:
    first = rules_client.post("/chat", json={"message": NETFLIX}).json()
    assert first["outcome"] == "awaiting_confirmation"
    case_id = first["case_id"]

    # The next turn of the same case names another customer while the registration is pending.
    stopped = rules_client.post(
        "/chat", json={"customer_id": "C2", "message": NETFLIX, "case_id": case_id}
    ).json()
    assert stopped["outcome"] == "security_blocked"
    analyst = analyst_headers(rules_client)
    case = rules_client.get(f"/cases/{case_id}", headers=analyst).json()
    assert (case["status"], case["recommended_action"]) == ("security_blocked", None)

    back = rules_client.post(
        f"/cases/{case_id}/decision", json={"decision": "need_info"}, headers=analyst
    )
    assert back.status_code == 409
    assert back.json()["error_code"] == "decision_not_allowed"
    closed = rules_client.post(
        f"/cases/{case_id}/decision", json={"decision": "approve"}, headers=analyst
    )
    assert closed.status_code == 200 and closed.json()["status"] == "approved"

    late = rules_client.post(
        "/chat", json={"message": "sí", "case_id": case_id, "confirm": True}
    ).json()
    assert (late["outcome"], late["actions_taken"]) == ("no_pending_action", [])
    assert _executed(netflix_customer) == (0, 0, 0)


@pytest.fixture
def two_customers(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    load(
        schema.admin,
        [customer("C1"), customer("C2")],
        [card("P1", "C1"), card("P2", "C2")],
        [
            transaction(
                "TX1",
                "C1",
                "P1",
                NOW - timedelta(hours=20),
                amount=120.0,
                currency="USD",
                merchant_name="Netflix",
            )
        ],
    )
    yield from _client(Settings(database_url=database_url, llm_enabled=False, log_level="WARNING"))


def test_confirming_another_customers_case_runs_nothing_and_reveals_nothing(
    two_customers: TestClient, schema: SchemaUrls
) -> None:
    first = two_customers.post("/chat", json={"message": NETFLIX}).json()
    assert first["outcome"] == "awaiting_confirmation"
    case_id = first["case_id"]

    r = two_customers.post(
        "/chat",
        json={"message": "sí", "case_id": case_id, "confirm": True},
        headers=customer_headers(two_customers, "C2"),
    )
    assert r.status_code == 404
    body = r.json()
    assert set(body) == {"error_code", "message", "trace_id"}
    assert body["error_code"] == "case_not_found"
    # The same words as for a case that does not exist, so a case id cannot be probed.
    assert body["message"] == f"Case {case_id} not found."
    assert _executed(schema) == (0, 0, 0)

    # The owner's pending action is intact: only C1 can confirm it.
    own = two_customers.post(
        "/chat", json={"message": "sí", "case_id": case_id, "confirm": True}
    ).json()
    assert (own["outcome"], own["actions_taken"]) == ("registered", ["open_dispute"])

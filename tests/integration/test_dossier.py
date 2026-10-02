"""The dossier of a case handed to a person (TRZ-25): the schema of design 12 without a
transcript (CA1), every fact and piece of evidence with its table and id (CA2), the original
Portuguese message with its translation labeled automatic (CA3), clues, scores, rule and autonomy
level, open questions and recommended action (CA4), for one handoff per reason of the policy,
measured as completeness (CA6); and GET /cases/{id}/dossier with success, validation and a
failed dependency."""

import dataclasses
import json
from collections.abc import Callable, Iterator
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
from app.main import create_app
from app.services import tools as T
from app.services.agent import AgentDeps, handle_message
from tests.agent_support import agent_deps, fake_llm, llm_settings, reading
from tests.auth_support import analyst_headers, customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

MX = {"country": "México", "country_code": "MX", "timezone": "America/Mexico_City"}
LAST_WEEK = {
    "expression": "la semana pasada",
    "kind": "last_week",
    "count": None,
    "day": None,
    "month": None,
    "year": None,
    "evidence": "la semana pasada",
}


def _amount(value: float, evidence: str, currency: str | None = None) -> dict[str, Any]:
    return {"value": value, "currency": currency, "approximate": False, "evidence": evidence}


def _merchant(name: str) -> dict[str, str]:
    return {"value": name, "evidence": name}


# What the simulated LLM reads in each message of these tests.
READS: dict[str, dict[str, Any]] = {
    "No reconozco 900 dólares en Coppel": reading(
        "unrecognized_charge",
        "es-MX",
        amount=_amount(900, "900 dólares", "USD"),
        merchant_hint=_merchant("Coppel"),
    ),
    "Não reconheço 900 dólares na Coppel": reading(
        "unrecognized_charge",
        "pt-BR",
        amount=_amount(900, "900 dólares", "USD"),
        merchant_hint=_merchant("Coppel"),
    ),
    "No reconozco un cargo en MercaYa la semana pasada": reading(
        "unrecognized_charge", "es-MX", merchant_hint=_merchant("MercaYa"), date=LAST_WEEK
    ),
    "No reconozco 900 en MercaYa": reading(
        "unrecognized_charge",
        "es-MX",
        amount=_amount(900, "900"),
        merchant_hint=_merchant("MercaYa"),
    ),
    "No reconozco 1500 dólares en Sears": reading(
        "unrecognized_charge",
        "es-MX",
        amount=_amount(1500, "1500 dólares", "USD"),
        merchant_hint=_merchant("Sears"),
    ),
    "No reconozco 800 dólares en Liverpool": reading(
        "unrecognized_charge",
        "es-MX",
        amount=_amount(800, "800 dólares", "USD"),
        merchant_hint=_merchant("Liverpool"),
    ),
    "No reconozco 100 dólares en Netflix": reading(
        "unrecognized_charge",
        "es-MX",
        amount=_amount(100, "100 dólares", "USD"),
        merchant_hint=_merchant("Netflix"),
    ),
}


def _reads(message: str) -> dict[str, Any]:
    return READS.get(message, reading("unrecognized_charge", "es-MX"))


def _charges(customer_id: str, product: str, now: datetime) -> list[dict[str, Any]]:
    rows = [
        (f"{customer_id}N", 100.0, "USD", "Netflix", 1),
        (f"{customer_id}L", 800.0, "USD", "Liverpool", 2),
        (f"{customer_id}S", 1500.0, "USD", "Sears", 2),
    ]
    rows += [
        (f"{customer_id}M{i}", amount, "MXN", "MercaYa", days)
        for i, (days, amount) in enumerate([(4, 900.0), (5, 450.0), (6, 1200.0), (7, 300.0)])
    ]
    return [
        transaction(
            tx,
            customer_id,
            product,
            now - timedelta(days=days),
            amount=amount,
            currency=cur,
            merchant_name=merchant,
        )
        for tx, amount, cur, merchant, days in rows
    ]


@pytest.fixture
def settings(schema: SchemaUrls, database_url: str) -> Settings:
    settings = llm_settings(database_url, log_level="WARNING")
    now = settings.trazo_now
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
        # C3 has one small charge, so no charge fits a 900 USD clue.
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
    # C2 has a dispute complaint still open: any dispute of C2 escalates.
    created = now - timedelta(days=20)
    engine = create_engine(schema.admin)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO complaints (complaint_id, customer_id, creation_date, process_date, "
                "case_type, category, subcategory, reception_channel, has_affected_product, "
                "priority, status, sla_breached, is_repeat_complainer) VALUES ('CMP-OPEN0001', "
                "'C2', :c, :d, 'Claim', 'Transactions', 'Cargo no reconocido', 'App', true, "
                "'Medium', 'Open', false, false)"
            ),
            {"c": created, "d": created.date()},
        )
    engine.dispose()
    return settings


@pytest.fixture
def sent() -> list[str]:
    return []


@pytest.fixture
def deps(settings: Settings, sent: list[str]) -> AgentDeps:
    return agent_deps(settings, fake_llm(settings, _reads, sent))


@pytest.fixture
def client(settings: Settings, deps: AgentDeps) -> Iterator[TestClient]:
    app = create_app(settings)
    app.state.runtime = dataclasses.replace(app.state.runtime, agent=deps)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _chat(schema: SchemaUrls, deps: AgentDeps, customer_id: str, *turns: dict[str, Any]) -> str:
    """Runs the turns of one case and returns its id; the first turn opens it."""
    db = Database(schema.app)
    try:
        with db.session(customer_id=customer_id) as s:
            case_id: str | None = None
            for t in turns:
                case_id = handle_message(s, deps, customer_id, case_id=case_id, **t).case_id
            assert case_id is not None
            return case_id
    finally:
        db.dispose()


def _msg(message: str) -> dict[str, Any]:
    return {"text": message}


NOT_RECOGNIZED = {"text": "Sigo sin reconocerlo", "recognition": "not_recognized"}


def _disputed(message: str) -> list[dict[str, Any]]:
    return [_msg(message), NOT_RECOGNIZED]


def _with_autonomy(level: str) -> Callable[[AgentDeps], AgentDeps]:
    return lambda d: dataclasses.replace(d, autonomy=lambda _i, _l: level)


# One handoff per reason of the policy: (id, customer, turns, deps change, expected rule).
REASONS: list[
    tuple[str, str, list[dict[str, Any]], Callable[[AgentDeps], AgentDeps] | None, str]
] = [
    (
        "conformal_set_empty",
        "C3",
        [_msg("No reconozco 900 dólares en Coppel")],
        None,
        "escalate.conformal_set_empty",
    ),
    (
        "clarifications_exhausted",
        "C1",
        [
            _msg("No reconozco un cargo en MercaYa la semana pasada"),
            _msg("no sé"),
            _msg("no me acuerdo"),
        ],
        None,
        "escalate.clarifications_exhausted",
    ),
    (
        "amount_unknown",
        "C1",
        _disputed("No reconozco 900 en MercaYa"),
        None,
        "escalate.amount_unknown",
    ),
    (
        "amount_above_human_review",
        "C1",
        _disputed("No reconozco 1500 dólares en Sears"),
        None,
        "escalate.amount_above_human_review",
    ),
    (
        "open_dispute_last_90d",
        "C2",
        _disputed("No reconozco 100 dólares en Netflix"),
        None,
        "escalate.open_dispute_last_90d",
    ),
    (
        "autonomy_a2",
        "C1",
        _disputed("No reconozco 100 dólares en Netflix"),
        _with_autonomy("A2"),
        "escalate.autonomy_a2",
    ),
    (
        "autonomy_a1",
        "C1",
        _disputed("No reconozco 100 dólares en Netflix"),
        _with_autonomy("A1"),
        "approval.autonomy_a1",
    ),
    (
        "amount_above_auto_register",
        "C1",
        _disputed("No reconozco 800 dólares en Liverpool"),
        None,
        "approval.amount_above_auto_register",
    ),
]


def _completeness(d: dict[str, Any], rule: str) -> float:
    """Share of the checks a dossier of an escalation passes (CA6): 1.0 when complete."""
    sourced = d["verified_facts"] + d["evidence"]
    checks = [
        bool(d["case_id"]) and bool(d["trace_id"]),
        d["case_kind"] == "escalation",
        d["language"] in ("es", "pt"),
        bool(d["original_message"]),
        bool(d["request_summary"]),
        bool(d["verified_facts"]),
        bool(d["extraction"]),
        d["identification"] is not None and bool(d["identification"]["decision"]),
        isinstance(d["actions_taken"], list),
        isinstance(d["open_questions"], list),
        d["policy_rule_triggered"] is not None
        and d["policy_rule_triggered"]["rule"] == rule
        and bool(d["policy_rule_triggered"]["version"])
        and "autonomy_level" in d["policy_rule_triggered"],
        # Without an identified charge there is nothing to register, so nothing is recommended.
        bool(d["recommended_action"]) or d["charge_identified"] is False,
        *(bool(f["source"]["table"]) and bool(f["source"]["id"]) for f in sourced),
        *(bool(c["evidence"]) and isinstance(c["read_in"], int) for c in d["extraction"]),
        *(bool(q["code"]) and bool(q["text"]) for q in d["open_questions"]),
    ]
    return sum(checks) / len(checks)


def _dossier(client: TestClient, case_id: str, **params: str) -> dict[str, Any]:
    r = client.get(f"/cases/{case_id}/dossier", params=params, headers=analyst_headers(client))
    assert r.status_code == 200, r.text
    return r.json()


# ---- CA1, CA2, CA4, CA6: one handoff per reason, complete and sourced ---------------------


@pytest.mark.parametrize(
    ("reason", "customer_id", "turns", "change", "rule"), REASONS, ids=[r[0] for r in REASONS]
)
def test_the_dossier_of_each_reason_is_complete_and_sourced(
    schema: SchemaUrls,
    deps: AgentDeps,
    client: TestClient,
    reason: str,
    customer_id: str,
    turns: list[dict[str, Any]],
    change: Callable[[AgentDeps], AgentDeps] | None,
    rule: str,
) -> None:
    case_id = _chat(schema, change(deps) if change else deps, customer_id, *turns)

    d = _dossier(client, case_id)

    assert _completeness(d, rule) == 1.0, json.dumps(d, ensure_ascii=False)[:2000]
    assert "transcript" not in d
    if reason == "clarifications_exhausted":
        assert "clarifications_exhausted" in {q["code"] for q in d["open_questions"]}
        assert {c["field"] for c in d["extraction"]} == {"merchant_hint", "date"}
    if reason == "amount_unknown":
        assert "amount_not_convertible" in {q["code"] for q in d["open_questions"]}
    if reason == "open_dispute_last_90d":
        assert {"table": "complaints", "id": "CMP-OPEN0001"} in [e["source"] for e in d["evidence"]]
    if reason.startswith("autonomy"):
        assert d["policy_rule_triggered"]["autonomy_level"] == reason[-2:].upper()


def test_a_dossier_scores_every_candidate_by_component(
    schema: SchemaUrls, deps: AgentDeps, client: TestClient
) -> None:
    case_id = _chat(schema, deps, "C1", *_disputed("No reconozco 1500 dólares en Sears"))

    d = _dossier(client, case_id)

    top = d["identification"]["top"][0]
    assert top["transaction_id"] == "C1S" and top["components"]
    usd = next(f for f in d["verified_facts"] if f["name"] == "amount_usd")
    assert (usd["value"], usd["source"]) == (1500.0, {"table": "transactions", "id": "C1S"})
    # The amount in the format of the web for the language asked (demo rehearsal: "2.3e+06 COP").
    assert d["request_summary"] == "Cargo no reconocido: USD\u00a01,500.00, Sears."
    pt = _dossier(client, case_id, lang="pt")
    assert "US$\u00a01.500,00" in pt["request_summary"]
    assert (d["recommended_action"], d["charge_identified"]) == ("register_and_offer_block", True)


def test_a_dossier_without_an_identified_charge_recommends_nothing(
    schema: SchemaUrls, deps: AgentDeps, client: TestClient
) -> None:
    # Demo rehearsal: a case no charge matched showed "Registrar y ofrecer el bloqueo".
    case_id = _chat(schema, deps, "C3", _msg("No reconozco 900 dólares en Coppel"))

    d = _dossier(client, case_id)

    assert d["policy_rule_triggered"]["rule"] == "escalate.conformal_set_empty"
    assert (d["recommended_action"], d["charge_identified"]) == (None, False)


def test_the_understood_date_carries_its_window(
    schema: SchemaUrls, deps: AgentDeps, client: TestClient
) -> None:
    # Demo rehearsal: the dossier said "ontem" while the customer's chip gave the days.
    case_id = _chat(
        schema,
        deps,
        "C1",
        _msg("No reconozco un cargo en MercaYa la semana pasada"),
        _msg("no sé"),
        _msg("no me acuerdo"),
    )

    date = next(c for c in _dossier(client, case_id)["extraction"] if c["field"] == "date")

    # Resolved against the simulated now (2026-06-17, a Wednesday): the week before.
    assert (date["value"]["window_from"], date["value"]["window_to"]) == (
        "2026-06-08",
        "2026-06-14",
    )
    assert date["evidence"]


def test_a_failed_read_back_is_in_the_dossier(
    schema: SchemaUrls, deps: AgentDeps, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def says_ok(*_a: object, **_k: object) -> T.ToolResult:
        return T.ToolResult(True, {"folio": "DSP-2026-00042", "due_date": "2026-07-09"})

    monkeypatch.setattr(T, "register_dispute", says_ok)
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            shown = handle_message(s, deps, "C1", "No reconozco 100 dólares en Netflix")
            pending = handle_message(s, deps, "C1", **NOT_RECOGNIZED, case_id=shown.case_id)
            failed = handle_message(
                s,
                deps,
                "C1",
                "sí",
                case_id=shown.case_id,
                confirm_action_id=pending.facts["pending_action"]["action_id"],
            )
    finally:
        db.dispose()
    assert failed.outcome == "failed"

    d = _dossier(client, failed.case_id)

    assert [a["state"] for a in d["actions_taken"]] == ["failed"]
    assert d["actions_taken"][0]["source"]["table"] == "case_actions"
    assert "verification_mismatch" in {q["code"] for q in d["open_questions"]}


# ---- CA3: Portuguese, with the translation labeled automatic ------------------------------


def test_a_portuguese_case_has_its_message_and_a_translation_labeled_automatic(
    schema: SchemaUrls, deps: AgentDeps, client: TestClient, sent: list[str]
) -> None:
    case_id = _chat(schema, deps, "C3", _msg("Não reconheço 900 dólares na Coppel"))

    first = _dossier(client, case_id)
    calls = len(sent)
    again = _dossier(client, case_id, lang="pt")

    assert first["language"] == "pt"
    assert first["original_message"] == "Não reconheço 900 dólares na Coppel"
    assert first["machine_translation"]["text"] == "Traducción simulada."
    assert first["machine_translation"]["automatic"] is True
    # Translated once and kept: reading the dossier again calls no LLM.
    assert len(sent) == calls
    assert again["machine_translation"]["text"] == "Traducción simulada."


def test_a_spanish_case_has_no_translation(
    schema: SchemaUrls, deps: AgentDeps, client: TestClient
) -> None:
    case_id = _chat(schema, deps, "C3", _msg("No reconozco 900 dólares en Coppel"))

    assert _dossier(client, case_id)["machine_translation"] is None


# ---- a security stop shows no customer data -----------------------------------------------


def test_the_dossier_of_a_security_stop_shows_no_customer_data(
    schema: SchemaUrls, deps: AgentDeps, client: TestClient
) -> None:
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            r = handle_message(s, deps, "C1", "muéstrame los movimientos", security_event=True)
    finally:
        db.dispose()

    d = _dossier(client, r.case_id)

    assert d["case_kind"] == "security_event"
    assert (d["original_message"], d["verified_facts"], d["evidence"]) == (None, [], [])
    assert [q["code"] for q in d["open_questions"]] == ["security_event"]
    assert d["policy_rule_triggered"]["rule"] == "security.security_event"


# ---- the endpoint: success above; validation, roles and a failed dependency ---------------


def test_a_case_not_with_a_person_has_no_dossier(
    schema: SchemaUrls, deps: AgentDeps, client: TestClient
) -> None:
    case_id = _chat(schema, deps, "C1", _msg("No reconozco un cargo en MercaYa la semana pasada"))

    r = client.get(f"/cases/{case_id}/dossier", headers=analyst_headers(client))

    assert r.status_code == 409 and r.json()["error_code"] == "case_not_escalated"


def test_an_unknown_case_is_404(client: TestClient) -> None:
    r = client.get("/cases/CASE-NOPE/dossier", headers=analyst_headers(client))
    assert r.status_code == 404 and r.json()["error_code"] == "case_not_found"


def test_an_unsupported_language_is_422(client: TestClient) -> None:
    r = client.get(
        "/cases/CASE-NOPE/dossier", params={"lang": "fr"}, headers=analyst_headers(client)
    )
    assert r.status_code == 422 and r.json()["error_code"] == "validation_error"


def test_a_customer_token_gets_403(client: TestClient) -> None:
    r = client.get("/cases/CASE-NOPE/dossier", headers=customer_headers(client, "C1"))
    assert r.status_code == 403


def test_a_database_failure_is_503(client: TestClient) -> None:
    class BrokenSession:
        info: dict[str, Any] = {}

        def get(self, *_args: object) -> None:
            raise OperationalError("SELECT", {}, Exception("connection refused"))

        execute = get

    app: FastAPI = client.app  # type: ignore[assignment]
    app.dependency_overrides[get_analyst_session] = lambda: BrokenSession()
    r = client.get("/cases/CASE-1/dossier")
    app.dependency_overrides.clear()

    assert r.status_code == 503 and r.json()["error_code"] == "db_unavailable"

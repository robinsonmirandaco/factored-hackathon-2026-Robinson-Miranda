"""A message on a case already with a person (TRZ-25): in each of the four handoff statuses it
is not read by the LLM nor decided again, the case keeps its status and its single queue item,
the message is added to the dossier redacted, and the customer is told in their language that a
person has the case, with its number."""

import dataclasses
import json
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.adapters.db.session import Database, SchemaUrls
from app.core.config import Settings
from app.main import create_app
from app.services import tools as T
from app.services.agent import AgentDeps, handle_message
from app.services.autonomy import CellStatus
from tests.agent_support import agent_deps, fake_llm, llm_settings, reading
from tests.auth_support import analyst_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

REAL = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "llm" / "short_answers.json").read_text("utf-8")
)["readings"]
MERCAYA = "No reconozco un cargo en MercaYa la semana pasada"
NETFLIX = "No reconozco 100 dólares en Netflix"
NOTE = "no sé, escríbeme a ana.perez@example.com"
LAST_WEEK = {
    "expression": "la semana pasada",
    "kind": "last_week",
    "count": None,
    "day": None,
    "month": None,
    "year": None,
    "evidence": "la semana pasada",
}
READS = {
    MERCAYA: reading(
        "unrecognized_charge",
        "es-MX",
        merchant_hint={"value": "MercaYa", "evidence": "MercaYa"},
        date=LAST_WEEK,
    ),
    NETFLIX: reading(
        "unrecognized_charge",
        "es-MX",
        amount={"value": 100, "currency": "USD", "approximate": False, "evidence": "100 dólares"},
        merchant_hint={"value": "Netflix", "evidence": "Netflix"},
    ),
}
WITH_PERSON = {
    "es": "Tu caso ya está con una persona. Agregamos tu mensaje a su expediente para que lo "
    "tenga en cuenta. Tu número de caso es {case_id}.",
    "pt": "O seu caso já está com uma pessoa. Incluímos a sua mensagem no dossiê para que ela a "
    "considere. O número do seu caso é {case_id}.",
}


def _reads(message: str) -> dict[str, Any]:
    return READS.get(message) or REAL.get(message) or REAL["no sé"]


@pytest.fixture
def settings(schema: SchemaUrls, database_url: str) -> Settings:
    settings = llm_settings(database_url, log_level="WARNING")
    now = settings.trazo_now
    mx = {"country": "México", "country_code": "MX", "timezone": "America/Mexico_City"}
    charges = [
        transaction(
            f"TXM{i}",
            "C1",
            "P1",
            now - timedelta(days=days),
            amount=amount,
            currency="MXN",
            merchant_name="MercaYa",
        )
        for i, (days, amount) in enumerate([(4, 900.0), (5, 450.0), (6, 1200.0), (7, 300.0)])
    ]
    charges.append(
        transaction(
            "TXN",
            "C1",
            "P1",
            now - timedelta(hours=20),
            amount=100.0,
            currency="USD",
            merchant_name="Netflix",
        )
    )
    load(schema.admin, [customer("C1", **mx)], [card("P1", "C1", currency="USD")], charges)
    return settings


@pytest.fixture
def sent() -> list[str]:
    return []


@pytest.fixture
def deps(settings: Settings, sent: list[str]) -> AgentDeps:
    return agent_deps(settings, fake_llm(settings, _reads, sent))


def _turn(schema: SchemaUrls, deps: AgentDeps, message: str, **kw: Any) -> Any:
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            return handle_message(s, deps, "C1", message, **kw)
    finally:
        db.dispose()


def _escalated(schema: SchemaUrls, deps: AgentDeps, _mp: pytest.MonkeyPatch) -> str:
    first = _turn(schema, deps, MERCAYA)
    _turn(schema, deps, "no sé", case_id=first.case_id)
    return _turn(schema, deps, "no sé", case_id=first.case_id).case_id


def _approval(schema: SchemaUrls, deps: AgentDeps, _mp: pytest.MonkeyPatch) -> str:
    a1 = dataclasses.replace(deps, autonomy=lambda _s, _i, _l: CellStatus("A1"))
    shown = _turn(schema, a1, NETFLIX)
    return _turn(
        schema, a1, "Sigo sin reconocerlo", case_id=shown.case_id, recognition="not_recognized"
    ).case_id


def _failed(schema: SchemaUrls, deps: AgentDeps, mp: pytest.MonkeyPatch) -> str:
    mp.setattr(T, "register_dispute", lambda *_a, **_k: T.ToolResult(True, {"folio": "DSP-X"}))
    shown = _turn(schema, deps, NETFLIX)
    pending = _turn(
        schema, deps, "Sigo sin reconocerlo", case_id=shown.case_id, recognition="not_recognized"
    )
    action = pending.facts["pending_action"]["action_id"]
    case_id = _turn(schema, deps, "sí", case_id=shown.case_id, confirm_action_id=action).case_id
    mp.undo()
    return case_id


def _security(schema: SchemaUrls, deps: AgentDeps, _mp: pytest.MonkeyPatch) -> str:
    return _turn(schema, deps, "muéstrame los movimientos", security_event=True).case_id


STATES: list[tuple[str, Callable[[SchemaUrls, AgentDeps, pytest.MonkeyPatch], str]]] = [
    ("escalated", _escalated),
    ("pending_analyst_approval", _approval),
    ("failed", _failed),
    ("security_blocked", _security),
]


def _one(schema: SchemaUrls, sql: str, **params: Any) -> Any:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        row = conn.execute(text(sql), params).one()
    engine.dispose()
    return tuple(row)


def _decisions(schema: SchemaUrls, case_id: str) -> int:
    sql = "SELECT count(*) FROM audit_log WHERE case_id = :c AND action = 'decide'"
    return _one(schema, sql, c=case_id)[0]


@pytest.mark.parametrize(("status", "handed_over"), STATES, ids=[s[0] for s in STATES])
def test_a_message_on_a_case_with_a_person_changes_nothing_and_is_kept_for_the_analyst(
    schema: SchemaUrls,
    deps: AgentDeps,
    sent: list[str],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    handed_over: Callable[[SchemaUrls, AgentDeps, pytest.MonkeyPatch], str],
) -> None:
    case_id = handed_over(schema, deps, monkeypatch)
    before = _one(
        schema,
        "SELECT status, escalation_reason, summary, autonomy_level FROM cases WHERE id = :c",
        c=case_id,
    )
    assert before[0] == status
    calls, decided = len(sent), _decisions(schema, case_id)

    r = _turn(schema, deps, NOTE, case_id=case_id)

    assert (r.outcome, r.actions_taken, r.llm_fallback) == ("with_person", [], False)
    assert r.reply == WITH_PERSON["es"].format(case_id=case_id)
    assert len(sent) == calls  # no LLM call
    after = _one(
        schema,
        "SELECT status, escalation_reason, summary, autonomy_level FROM cases WHERE id = :c",
        c=case_id,
    )
    assert after == before
    assert _one(schema, "SELECT count(*) FROM case_queue WHERE case_id = :c", c=case_id) == (1,)
    note = _one(
        schema,
        "SELECT payload->>'redacted_text' FROM audit_log WHERE case_id = :c "
        "AND action = 'customer_note'",
        c=case_id,
    )
    assert note == ("no sé, escríbeme a [EMAIL]",)
    assert _decisions(schema, case_id) == decided


def test_the_reply_is_in_the_language_of_the_customer(
    schema: SchemaUrls, deps: AgentDeps, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id = _escalated(schema, deps, monkeypatch)

    r = _turn(schema, deps, "Ainda não sei qual foi a cobrança", case_id=case_id)

    assert r.reply == WITH_PERSON["pt"].format(case_id=case_id)


@pytest.fixture
def client(settings: Settings, deps: AgentDeps) -> Iterator[TestClient]:
    app = create_app(settings)
    app.state.runtime = dataclasses.replace(app.state.runtime, agent=deps)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_the_dossier_shows_the_later_messages_redacted(
    schema: SchemaUrls, deps: AgentDeps, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id = _escalated(schema, deps, monkeypatch)
    _turn(schema, deps, NOTE, case_id=case_id)

    r = client.get(f"/cases/{case_id}/dossier", headers=analyst_headers(client))

    later = r.json()["later_messages"]
    assert [m["text"] for m in later] == ["no sé, escríbeme a [EMAIL]"]
    assert later[0]["source"]["table"] == "audit_log"

"""Clarifications and escalation in full customer turns against Postgres (TRZ-25): the answer to
a question adds its clues to the earlier ones, a claim question or a request out of scope is
served with its own intent, a third question hands the case to a person (design 5.1), and every
handoff gets one queue item with its priority and SLA and tells the customer the case number
(CA5).

The short answers are read as the real model read them (tests/fixtures/llm/short_answers.json):
without the question, it reads "fueron 900" and "fue el martes" as out of scope, with their clue.
"""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from app.adapters.db.session import Database, SchemaUrls
from app.core.config import Settings
from app.core.time import utcnow
from app.services import tools as T
from app.services.agent import AgentDeps, AgentResponse, handle_message
from tests.agent_support import agent_deps, fake_llm, llm_settings, reading
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

FIRST = "No reconozco un cargo en MercaYa la semana pasada"
MERCAYA = {"value": "MercaYa", "evidence": "MercaYa"}
LAST_WEEK = {
    "expression": "la semana pasada",
    "kind": "last_week",
    "count": None,
    "day": None,
    "month": None,
    "year": None,
    "evidence": "la semana pasada",
}


REAL = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "llm" / "short_answers.json").read_text("utf-8")
)["readings"]
# Simulated readings, where no real one was captured: out of scope with no clue.
SIMULATED = {
    FIRST: reading("unrecognized_charge", "es-MX", merchant_hint=MERCAYA, date=LAST_WEEK),
    "¿Cómo va mi reclamo?": reading("claim_status", "es-MX"),
    "Quiero un préstamo": reading("out_of_scope", "es-MX"),
    "el cargo fue de 900": reading("out_of_scope", "es-MX"),
}


def _reads(message: str) -> dict[str, Any]:
    """What the LLM reads in each message of these tests: the real reading when captured."""
    return REAL.get(message) or SIMULATED[message]


@pytest.fixture
def settings(schema: SchemaUrls, database_url: str) -> Settings:
    settings = llm_settings(database_url)
    now = settings.trazo_now
    mx = {"country": "México", "country_code": "MX", "timezone": "America/Mexico_City"}
    load(
        schema.admin,
        [customer("C1", **mx)],
        [card("P1", "C1", currency="MXN")],
        [
            transaction(
                f"TX{i}",
                "C1",
                "P1",
                now - timedelta(days=days),
                amount=amount,
                currency="MXN",
                merchant_name="MercaYa",
            )
            for i, (days, amount) in enumerate([(4, 900.0), (5, 450.0), (6, 1200.0), (7, 300.0)])
        ],
    )
    return settings


@pytest.fixture
def deps(settings: Settings) -> AgentDeps:
    return agent_deps(settings, fake_llm(settings, _reads, reply="Revisemos el cargo."))


def _turns(schema: SchemaUrls, deps: AgentDeps, *messages: str) -> list[AgentResponse]:
    """Sends the messages in one case, the first one opening it."""
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            out = [handle_message(s, deps, "C1", messages[0])]
            for m in messages[1:]:
                out.append(handle_message(s, deps, "C1", m, case_id=out[0].case_id))
            return out
    finally:
        db.dispose()


def _rows(schema: SchemaUrls, sql: str, **params: Any) -> list[Any]:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        rows = list(conn.execute(text(sql), params))
    engine.dispose()
    return rows


def _actions(schema: SchemaUrls, case_id: str) -> list[str]:
    rows = _rows(
        schema,
        "SELECT actor || '/' || action FROM audit_log WHERE case_id = :c ORDER BY id",
        c=case_id,
    )
    return [r[0] for r in rows]


# ---- the answer to a question adds to the earlier clues ------------------------------------


def test_an_answer_adds_its_clue_to_the_earlier_ones_and_identifies_the_charge(
    schema: SchemaUrls, deps: AgentDeps
) -> None:
    asked, answered = _turns(schema, deps, FIRST, "fueron 900")

    assert REAL["fueron 900"]["intent"] == "out_of_scope"
    assert asked.outcome == "identifying"
    assert (answered.intent, answered.outcome) == ("unrecognized_charge", "recognizing")
    assert answered.facts["charge"].transaction_id == "TX0"
    merge = _rows(
        schema,
        "SELECT result FROM audit_log WHERE case_id = :c AND action = 'merge_clues'",
        c=answered.case_id,
    )
    assert len(merge) == 1 and merge[0][0]["merged"] is True
    assert _read_by(schema, answered.case_id) == ["llm"]
    clues, sources = merge[0][0]["clues"], merge[0][0]["sources"]
    assert (clues["amount"]["value"], clues["merchant_hint"]["value"]) == (900, "MercaYa")
    assert clues["date"]["expression"] == "la semana pasada"
    # Each clue names the comprehension row of the message it was read in.
    reads = _rows(
        schema,
        "SELECT id FROM audit_log WHERE case_id = :c AND action = 'comprehend' ORDER BY id",
        c=answered.case_id,
    )
    first, second = (r[0] for r in reads)
    assert sources == {"merchant_hint": first, "date": first, "amount": second}


def _read_by(schema: SchemaUrls, case_id: str) -> list[str]:
    rows = _rows(
        schema,
        "SELECT payload->>'read_by' FROM audit_log WHERE case_id = :c AND action = 'merge_clues' "
        "ORDER BY id",
        c=case_id,
    )
    return [r[0] for r in rows]


def _merged(schema: SchemaUrls, case_id: str) -> dict[str, Any]:
    return _rows(
        schema,
        "SELECT result->'clues' FROM audit_log WHERE case_id = :c AND action = 'merge_clues' "
        "ORDER BY id DESC LIMIT 1",
        c=case_id,
    )[0][0]


def test_a_date_read_as_out_of_scope_replaces_the_earlier_date(
    schema: SchemaUrls, deps: AgentDeps
) -> None:
    _, answered = _turns(schema, deps, FIRST, "fue el martes")

    assert _read_by(schema, answered.case_id) == ["llm"]
    clues = _merged(schema, answered.case_id)
    assert (clues["date"]["evidence"], clues["merchant_hint"]["value"]) == (
        "fue el martes",
        "MercaYa",
    )


def test_an_answer_in_portuguese_adds_its_amount_and_currency(
    schema: SchemaUrls, deps: AgentDeps
) -> None:
    _, answered = _turns(schema, deps, FIRST, "foram 900 reais")

    assert _read_by(schema, answered.case_id) == ["llm"]
    amount = _merged(schema, answered.case_id)["amount"]
    assert (amount["value"], amount["currency"], amount["evidence"]) == (900, "BRL", "900 reais")


def test_the_rules_read_a_clue_the_model_left_out(schema: SchemaUrls, deps: AgentDeps) -> None:
    _, answered = _turns(schema, deps, FIRST, "el cargo fue de 900")

    assert _read_by(schema, answered.case_id) == ["rules"]
    amount = _merged(schema, answered.case_id)["amount"]
    assert amount["value"] == 900 and amount["evidence"] in "el cargo fue de 900"


def test_an_answer_with_no_clue_counts_as_a_clarification(
    schema: SchemaUrls, deps: AgentDeps
) -> None:
    asked, empty = _turns(schema, deps, FIRST, "no sé")

    assert REAL["no sé"]["intent"] == "out_of_scope"
    assert (asked.outcome, empty.outcome, empty.intent) == (
        "identifying",
        "identifying",
        "unrecognized_charge",
    )
    assert _read_by(schema, empty.case_id) == ["none"]
    (count,) = _rows(schema, "SELECT clarifications FROM cases WHERE id = :c", c=empty.case_id)[0]
    assert count == 2


def test_the_comprehension_row_keeps_the_structured_clues(
    schema: SchemaUrls, deps: AgentDeps
) -> None:
    (asked,) = _turns(schema, deps, FIRST)

    (result,) = _rows(
        schema,
        "SELECT result FROM audit_log WHERE case_id = :c AND action = 'comprehend'",
        c=asked.case_id,
    )[0]
    assert result["merchant_hint"] == MERCAYA
    assert result["date"]["expression"] == "la semana pasada"
    assert result["date"]["window_days"] and result["date"]["evidence"] == "la semana pasada"


@pytest.mark.parametrize(
    ("message", "intent", "outcome"),
    [
        ("¿Cómo va mi reclamo?", "claim_status", "informed"),
        ("Quiero un préstamo", "out_of_scope", "abstained"),
    ],
)
def test_a_claim_question_or_a_request_out_of_scope_is_served_with_its_own_intent(
    schema: SchemaUrls, deps: AgentDeps, message: str, intent: str, outcome: str
) -> None:
    asked, other = _turns(schema, deps, FIRST, message)

    assert asked.outcome == "identifying"
    assert (other.intent, other.outcome) == (intent, outcome)
    assert "agent/merge_clues" not in _actions(schema, other.case_id)


# ---- two clarifications, then a person ----------------------------------------------------


def test_a_third_question_hands_the_case_to_a_person(schema: SchemaUrls, deps: AgentDeps) -> None:
    first, second, third = _turns(schema, deps, FIRST, "no sé", "no sé")

    assert (first.outcome, second.outcome) == ("identifying", "identifying")
    assert (third.outcome, third.autonomy_level) == ("escalated", "L3")
    (case,) = _rows(
        schema,
        "SELECT clarifications, status, escalation_reason FROM cases WHERE id = :c",
        c=third.case_id,
    )
    assert tuple(case) == (2, "escalated", "escalate.clarifications_exhausted")
    assert f"Tu número de caso es {third.case_id}." in third.reply
    assert third.reply.startswith("No logramos identificar el cargo con certeza.")


# ---- one queue item with priority and SLA -------------------------------------------------


def test_a_handoff_gets_one_queue_item_with_its_priority_and_sla(
    schema: SchemaUrls, deps: AgentDeps
) -> None:
    before = utcnow()
    *_, third = _turns(schema, deps, FIRST, "no sé", "no sé")
    after = utcnow()

    items = _rows(
        schema,
        "SELECT kind, priority, reason, sla_due_at, created_at FROM case_queue WHERE case_id = :c",
        c=third.case_id,
    )
    assert len(items) == 1
    kind, priority, reason, due, _ = items[0]
    assert (kind, priority, reason) == ("escalation", "normal", "escalate.clarifications_exhausted")
    # SLA of a normal case: 24 hours on the real clock.
    assert before + timedelta(hours=24) <= due <= after + timedelta(hours=24)


def test_a_customer_without_the_card_is_queued_with_high_priority(
    schema: SchemaUrls, settings: Settings
) -> None:
    lost = {"value": False, "evidence": "me robaron la tarjeta"}
    amount = {"value": 900, "currency": None, "approximate": False, "evidence": "900"}
    answer = reading(
        "unrecognized_charge",
        "es-MX",
        merchant_hint=MERCAYA,
        amount=amount,
        card_in_possession=lost,
    )
    deps = agent_deps(settings, fake_llm(settings, answer))
    # No MXN rate in the fixture: the amount is not convertible and the policy escalates it.
    shown, escalated = _still_not_recognized(
        schema, deps, "Me robaron la tarjeta y hay 900 en MercaYa"
    )

    assert shown.outcome == "recognizing" and escalated.outcome == "escalated"
    (item,) = _rows(
        schema, "SELECT priority FROM case_queue WHERE case_id = :c", c=escalated.case_id
    )
    assert item[0] == "high"


def test_a_case_is_queued_once_however_often_it_is_handed_over(
    schema: SchemaUrls, deps: AgentDeps
) -> None:
    *_, third = _turns(schema, deps, FIRST, "no sé", "no sé")
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            again = T.escalate_to_human(s, third.case_id, "escalate.conformal_set_empty", 24)
    finally:
        db.dispose()

    assert again.message == "already escalated (idempotent)"
    items = _rows(schema, "SELECT reason FROM case_queue WHERE case_id = :c", c=third.case_id)
    assert [r[0] for r in items] == ["escalate.clarifications_exhausted"]


def _still_not_recognized(
    schema: SchemaUrls, deps: AgentDeps, message: str
) -> tuple[AgentResponse, AgentResponse]:
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            shown = handle_message(s, deps, "C1", message)
            decided = handle_message(
                s,
                deps,
                "C1",
                "Sigo sin reconocerlo",
                case_id=shown.case_id,
                recognition="not_recognized",
            )
            return shown, decided
    finally:
        db.dispose()

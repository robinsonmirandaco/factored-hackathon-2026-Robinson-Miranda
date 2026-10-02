"""The fact checker in full customer turns with a simulated LLM (TRZ-20): a reply with an
unsupported element is replaced by the fixed one and the element is audited (CA3, CA4, CA5); the
ablation lets it through and counts it (CA6); without a backing passage no deadline is stated and
a person is offered, in the customer's language (CA7); the card digits a reply may state are read
from products through the charge. The receipt of a registration is written by code since the
walkthrough of TRZ-40, so the checked replies are the ones the LLM still writes."""

import dataclasses
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from app.adapters.db.session import Database, SchemaUrls
from app.adapters.llm import template_reply
from app.core.config import Settings
from app.domain.fact_check import unsupported
from app.services.agent import AgentDeps, AgentResponse, handle_message
from app.services.replies import verified_facts
from tests.agent_support import agent_deps, fake_llm, llm_settings, reading
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

MESSAGES = {
    "es": "No reconozco un cargo de 120 dólares en Netflix",
    "pt": "Não reconheço essa cobrança de 120 dólares da Netflix",
}
NOT_RECOGNIZED = {"es": "Sigo sin reconocerlo", "pt": "Continuo sem reconhecer"}
YES = {"es": "sí", "pt": "sim"}
PERSON = {"es": "te comunicamos con una persona", "pt": "contato com uma pessoa"}


@pytest.fixture
def settings(schema: SchemaUrls, database_url: str) -> Settings:
    settings = llm_settings(database_url)
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1")],
        [
            transaction(
                "TX1",
                "C1",
                "P1",
                settings.trazo_now - timedelta(hours=20),
                amount=120.0,
                currency="USD",
                merchant_name="Netflix",
            ),
            transaction(
                "TX2",
                "C1",
                "P1",
                settings.trazo_now - timedelta(hours=30),
                amount=400.0,
                currency="USD",
                merchant_name="Cinépolis",
            ),
        ],
    )
    return settings


def _deps(settings: Settings, writes: str, language: str = "es") -> AgentDeps:
    """Agent dependencies whose LLM writes `writes` when the customer recognizes the charge, the
    reply the LLM still writes after the receipt became code-written (walkthrough of TRZ-40), and
    a sentence with no figure for any other turn."""
    answer = reading(
        "unrecognized_charge",
        "es-CO" if language == "es" else "pt-BR",
        amount={"value": 120, "currency": "USD", "approximate": False, "evidence": "120 dólares"},
        merchant_hint={"value": "Netflix", "evidence": "Netflix"},
    )

    def reply(facts: dict[str, Any]) -> str:
        if facts["outcome"] == "recognized_closed":
            return writes
        return "Revisaremos el cargo." if language == "es" else "Vamos revisar a cobrança."

    return agent_deps(settings, fake_llm(settings, answer, reply=reply))


def _session(schema: SchemaUrls) -> Database:
    return Database(schema.app)


def _recognize(schema: SchemaUrls, deps: AgentDeps) -> AgentResponse:
    db = _session(schema)
    try:
        with db.session(customer_id="C1") as s:
            shown = handle_message(s, deps, "C1", MESSAGES["es"])
            return handle_message(
                s, deps, "C1", "Ya lo reconozco", case_id=shown.case_id, recognition="recognized"
            )
    finally:
        db.dispose()


def _register(schema: SchemaUrls, deps: AgentDeps, language: str = "es") -> AgentResponse:
    db = _session(schema)
    try:
        with db.session(customer_id="C1") as s:
            shown = handle_message(s, deps, "C1", MESSAGES[language])
            pending = handle_message(
                s,
                deps,
                "C1",
                NOT_RECOGNIZED[language],
                case_id=shown.case_id,
                recognition="not_recognized",
            )
            return handle_message(
                s,
                deps,
                "C1",
                YES[language],
                case_id=shown.case_id,
                confirm_action_id=pending.facts["pending_action"]["action_id"],
            )
    finally:
        db.dispose()


def _checks(schema: SchemaUrls, case_id: str) -> list[tuple]:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT payload->>'mode', result->'passed', result->'sent', result->'unsupported' "
                "FROM audit_log WHERE case_id = :case_id AND action = 'fact_check' ORDER BY id"
            ),
            {"case_id": case_id},
        ).all()
    engine.dispose()
    return [tuple(r) for r in rows]


CLOSED = "Listo, cerramos tu caso sin cambios en tu cuenta."


def test_a_supported_reply_is_sent_and_its_check_is_audited(
    schema: SchemaUrls, settings: Settings
) -> None:
    r = _recognize(schema, _deps(settings, CLOSED))

    assert (r.outcome, r.llm_fallback, r.reply) == ("recognized_closed", False, CLOSED)
    assert _checks(schema, r.case_id)[-1] == ("enforce", True, True, [])


@pytest.mark.parametrize(
    ("writes", "unsupported"),
    [
        (CLOSED + " Te responderemos en 5 días hábiles.", {"kind": "deadline", "value": "5 d"}),
        (CLOSED + " El cargo fue de 1,200.00 USD.", {"kind": "amount", "value": "1200.00"}),
        (CLOSED + " Tu folio es DSP-2026-99999.", {"kind": "folio", "value": "DSP-2026-99999"}),
        (
            CLOSED + " Confírmanos tu contraseña.",
            {"kind": "forbidden_request", "value": "contraseña"},
        ),
        (
            CLOSED + " También bloqueamos tu tarjeta.",
            {"kind": "action_claim", "value": "block_card"},
        ),
    ],
    ids=["invented deadline", "other amount", "invented folio", "forbidden request", "undone"],
)
def test_an_unsupported_element_sends_the_fixed_reply_and_is_audited(
    schema: SchemaUrls,
    settings: Settings,
    writes: str,
    unsupported: dict[str, str],
) -> None:
    r = _recognize(schema, _deps(settings, writes))

    assert (r.outcome, r.llm_fallback) == ("recognized_closed", True)
    assert r.reply == template_reply(r.facts, "es")
    assert _checks(schema, r.case_id)[-1] == ("enforce", False, False, [unsupported])


def test_with_the_checker_off_the_unsupported_claim_reaches_the_customer_and_is_counted(
    schema: SchemaUrls, settings: Settings
) -> None:
    writes = CLOSED + " Te responderemos en 5 días hábiles."
    deps = dataclasses.replace(_deps(settings, writes), fact_check=False)
    r = _recognize(schema, deps)

    assert "5 días hábiles" in r.reply and not r.llm_fallback
    assert _checks(schema, r.case_id)[-1] == (
        "observe",
        False,
        True,
        [{"kind": "deadline", "value": "5 d"}],
    )


def test_the_receipt_is_written_by_code_with_the_cited_deadline(
    schema: SchemaUrls, settings: Settings
) -> None:
    # The model would state its own deadline; it is never asked to write the receipt.
    r = _register(schema, _deps(settings, CLOSED + " Responderemos en 5 días hábiles."))

    assert (r.outcome, r.llm_fallback) == ("registered_verified", False)
    assert r.reply.startswith(template_reply(r.facts, "es") + " Plazo de respuesta:")
    assert "(15 días hábiles).\n[simulado] §2.1 · política de demostración, no del banco" in r.reply
    assert "Responderemos" not in r.reply


@pytest.mark.parametrize("language", ["es", "pt"])
def test_without_a_backing_passage_no_deadline_is_stated_and_a_person_is_offered(
    schema: SchemaUrls, settings: Settings, language: str
) -> None:
    deps = dataclasses.replace(_deps(settings, CLOSED, language), passages={})
    r = _register(schema, deps, language)

    assert r.outcome == "registered_verified"
    assert r.facts["dispute"]["due_date"] is None
    assert r.reply == f"{template_reply(r.facts, language)} " + (
        "No tenemos un plazo de respuesta respaldado por la política para esta aclaración. "
        "Si quieres, te comunicamos con una persona."
        if language == "es"
        else "Não temos um prazo de resposta respaldado pela política para esta contestação. "
        "Se quiser, colocamos você em contato com uma pessoa."
    )
    assert PERSON[language] in r.reply
    assert "hábiles" not in r.reply and "úteis" not in r.reply


def test_the_card_digits_are_read_from_products_through_the_charge(
    schema: SchemaUrls, database_url: str
) -> None:
    settings = llm_settings(database_url)
    at = settings.trazo_now - timedelta(hours=20)
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1", product_number_last4="4821")],
        [transaction("TX1", "C1", "P1", at, amount=120.0, currency="USD")],
    )
    charge = {"transaction_id": "TX1", "amount": 120.0, "date": at.date().isoformat()}
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            facts = verified_facts(s, "C1", {"transaction": charge}, {})
    finally:
        db.dispose()

    assert facts.last4 == frozenset({"4821"})
    assert unsupported("Es el cargo de tu tarjeta terminada en 4821.", facts) == []
    assert [c.value for c in unsupported("Tu tarjeta terminada en 1111.", facts)] == ["1111"]

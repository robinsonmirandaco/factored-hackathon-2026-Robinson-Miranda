"""The fact checker in full customer turns with a simulated LLM (TRZ-20): a reply with an
unsupported element is replaced by the fixed one and the element is audited (CA3, CA4, CA5); the
ablation lets it through and counts it (CA6); without a backing passage no deadline is stated and
a person is offered, in the customer's language (CA7)."""

import dataclasses
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from app.adapters.db.session import Database, SchemaUrls
from app.adapters.llm import template_reply
from app.core.config import Settings
from app.services.agent import AgentDeps, AgentResponse, handle_message
from tests.agent_support import agent_deps, fake_llm, llm_settings, reading
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

MESSAGES = {
    "es": "No reconozco un cargo de 120 dólares en Netflix",
    "pt": "Não reconheço essa cobrança de 120 dólares da Netflix",
}
NOT_RECOGNIZED = {"es": "Sigo sin reconocerlo", "pt": "Continuo sem reconhecer"}
YES = {"es": "sí", "pt": "sim"}
PERSON = {"es": "te comunico con una persona", "pt": "contato com uma pessoa"}


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


def _deps(settings: Settings, writes: Callable[[str], str], language: str = "es") -> AgentDeps:
    """Agent dependencies whose LLM writes `writes(folio)` for the receipt of a registration,
    and a sentence with no figure for any other turn."""
    answer = reading(
        "unrecognized_charge",
        "es-CO" if language == "es" else "pt-BR",
        amount={"value": 120, "currency": "USD", "approximate": False, "evidence": "120 dólares"},
        merchant_hint={"value": "Netflix", "evidence": "Netflix"},
    )

    def reply(facts: dict[str, Any]) -> str:
        if facts["outcome"] == "registered_verified":
            return writes(facts["dispute"]["folio"])
        return "Revisaremos el cargo." if language == "es" else "Vamos revisar a cobrança."

    return agent_deps(settings, fake_llm(settings, answer, reply=reply))


def _register(schema: SchemaUrls, deps: AgentDeps, language: str = "es") -> AgentResponse:
    db = Database(schema.app)
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


def _last_check(schema: SchemaUrls, case_id: str) -> tuple:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT payload->>'mode', result->'passed', result->'sent', result->'unsupported' "
                "FROM audit_log WHERE case_id = :case_id AND action = 'fact_check' "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"case_id": case_id},
        ).one()
    engine.dispose()
    return tuple(row)


def _receipt(folio: str) -> str:
    return f"Registramos tu aclaración del cargo de 120.00 USD en Netflix con el folio {folio}."


def test_a_correct_receipt_is_sent_with_the_cited_deadline(
    schema: SchemaUrls, settings: Settings
) -> None:
    r = _register(schema, _deps(settings, _receipt))

    assert (r.outcome, r.llm_fallback) == ("registered_verified", False)
    assert r.reply.startswith(_receipt(r.facts["dispute"]["folio"]))
    assert "15 días hábiles según el §2.1 (política de demostración, no del banco)" in r.reply
    assert _last_check(schema, r.case_id) == ("enforce", True, True, [])


@pytest.mark.parametrize(
    ("writes", "unsupported"),
    [
        (
            lambda folio: _receipt(folio) + " Te responderemos en 5 días hábiles.",
            {"kind": "deadline", "value": "5 d"},
        ),
        (
            lambda folio: _receipt(folio).replace("120.00 USD", "1,200.00 USD"),
            {"kind": "amount", "value": "1200.00"},
        ),
        (
            lambda _folio: _receipt("DSP-2026-99999"),
            {"kind": "folio", "value": "DSP-2026-99999"},
        ),
        (
            lambda folio: _receipt(folio) + " Confírmanos tu contraseña.",
            {"kind": "forbidden_request", "value": "contraseña"},
        ),
        (
            lambda folio: _receipt(folio) + " También bloqueamos tu tarjeta.",
            {"kind": "action_claim", "value": "block_card"},
        ),
    ],
    ids=["invented deadline", "other amount", "invented folio", "forbidden request", "undone"],
)
def test_an_unsupported_element_sends_the_fixed_reply_and_is_audited(
    schema: SchemaUrls,
    settings: Settings,
    writes: Callable[[str], str],
    unsupported: dict[str, str],
) -> None:
    r = _register(schema, _deps(settings, writes))

    assert (r.outcome, r.llm_fallback) == ("registered_verified", True)
    fixed = template_reply(r.facts, "es")
    assert r.reply.startswith(fixed + " Plazo de respuesta:")
    assert _last_check(schema, r.case_id) == ("enforce", False, False, [unsupported])


def test_with_the_checker_off_the_unsupported_claim_reaches_the_customer_and_is_counted(
    schema: SchemaUrls, settings: Settings
) -> None:
    writes = lambda folio: _receipt(folio) + " Te responderemos en 5 días hábiles."  # noqa: E731
    deps = dataclasses.replace(_deps(settings, writes), fact_check=False)
    r = _register(schema, deps)

    assert "5 días hábiles" in r.reply and not r.llm_fallback
    assert _last_check(schema, r.case_id) == (
        "observe",
        False,
        True,
        [{"kind": "deadline", "value": "5 d"}],
    )


@pytest.mark.parametrize("language", ["es", "pt"])
def test_without_a_backing_passage_no_deadline_is_stated_and_a_person_is_offered(
    schema: SchemaUrls, settings: Settings, language: str
) -> None:
    def writes(folio: str) -> str:
        # The model tries to state the usual deadline anyway.
        if language == "es":
            return (
                f"Registramos tu aclaración con el folio {folio}. Responderemos en 15 días hábiles."
            )
        return (
            f"Registramos a sua contestação com o protocolo {folio}. "
            "Responderemos em 15 dias úteis."
        )

    deps = dataclasses.replace(_deps(settings, writes, language), passages={})
    r = _register(schema, deps, language)

    assert r.outcome == "registered_verified"
    assert r.facts["dispute"]["due_date"] is None
    assert r.reply == f"{template_reply(r.facts, language)} " + (
        "No tengo un plazo de respuesta respaldado por la política para esta aclaración. "
        "Si quieres, te comunico con una persona."
        if language == "es"
        else "Não tenho um prazo de resposta respaldado pela política para esta contestação. "
        "Se quiser, eu coloco você em contato com uma pessoa."
    )
    assert PERSON[language] in r.reply
    assert "hábiles" not in r.reply and "úteis" not in r.reply
    assert _last_check(schema, r.case_id)[3] == [{"kind": "deadline", "value": "15 d"}]

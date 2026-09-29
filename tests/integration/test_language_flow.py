"""The language of each turn in full conversations (TRZ-11 CA6): the reply follows the language
the customer writes in, also when it changes in the middle of a case."""

import re
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters.db.models import AuditRecord, Case
from app.adapters.db.session import Database, SchemaUrls
from app.adapters.llm import _REPLIES
from app.core.config import Settings
from app.main import create_app
from tests.auth_support import customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 17, 23, 59)
ES = "No reconozco un cargo de 120 dólares en Netflix"
PT = "Não reconheço essa cobrança de 120 dólares da Netflix no meu cartão"


@pytest.fixture
def client(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
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
    app = create_app(Settings(database_url=database_url, llm_enabled=False, log_level="WARNING"))
    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers.update(customer_headers(c, "C1"))
        yield c


def _in(reply: str, language: str) -> bool:
    """Whether the reply is one of the fixed replies of that language (the LLM is off), with the
    deadline note of a registration or the case number of a handoff, or the recognition step,
    which code always writes."""
    intro = {"es": "Este es el cargo", "pt": "Esta é a cobrança"}[language]
    note = {"es": "Plazo de respuesta", "pt": "Prazo de resposta"}[language]
    number = {"es": "Tu número de caso es", "pt": "O número do seu caso é"}[language]
    fixed = (
        re.escape(t).replace(re.escape("{folio}"), r"DSP-\d{4}-\d{5}")
        + rf"(?: {note}: .+)?(?: {number} CASE-[0-9A-F]+\.)?"
        for t in _REPLIES[language].values()
    )
    return any(re.fullmatch(f, reply) for f in fixed) or reply.startswith(intro)


def _case(schema: SchemaUrls, case_id: str) -> tuple[str | None, list[dict]]:
    owner = Database(schema.admin)
    with owner.session() as s:
        case = s.get(Case, case_id)
        assert case is not None
        decisions = [
            r.result["language_decision"]
            for r in s.scalars(
                select(AuditRecord)
                .where(
                    AuditRecord.case_id == case_id,
                    AuditRecord.action.in_(["comprehend", "recognize", "confirm"]),
                )
                .order_by(AuditRecord.id)
            )
        ]
        language = case.language
    owner.dispose()
    return language, decisions


@pytest.mark.parametrize(("message", "language"), [(ES, "es"), (PT, "pt")])
def test_the_reply_is_in_the_language_of_the_message(
    client: TestClient, schema: SchemaUrls, message: str, language: str
) -> None:
    body = client.post("/chat", json={"message": message}).json()
    assert body["outcome"] == "recognizing"
    assert _in(body["reply"], language)
    stored, decisions = _case(schema, body["case_id"])
    assert stored == language
    # Without the LLM, Spanish takes the customer's country (CO) and Portuguese is BR.
    assert decisions == [
        {
            "language": language,
            "variant": "es-CO" if language == "es" else "pt-BR",
            "mixed": False,
            "source": "detector",
        }
    ]


def test_a_customer_who_switches_language_is_answered_in_the_new_one(
    client: TestClient, schema: SchemaUrls
) -> None:
    first = client.post("/chat", json={"message": ES}).json()
    assert _in(first["reply"], "es")
    case_id = first["case_id"]

    second = client.post("/chat", json={"message": PT, "case_id": case_id}).json()
    assert second["outcome"] == "recognizing"
    assert _in(second["reply"], "pt")
    assert [c["label"] for c in second["choices"]] == ["Continuo sem reconhecer", "Já reconheço"]

    pending = client.post(
        "/chat",
        json={
            "message": "Continuo sem reconhecer",
            "case_id": case_id,
            "recognition": "not_recognized",
        },
    ).json()
    assert pending["outcome"] == "awaiting_confirmation"
    assert _in(pending["reply"], "pt")

    # "sim" is too short to count on: it keeps the language the case has now.
    third = client.post(
        "/chat",
        json={
            "message": "sim",
            "case_id": case_id,
            "confirm_action_id": pending["pending_action"]["action_id"],
        },
    ).json()
    assert third["outcome"] == "registered_verified"
    assert _in(third["reply"], "pt")

    stored, decisions = _case(schema, case_id)
    assert stored == "pt"
    assert [(d["language"], d["source"]) for d in decisions] == [
        ("es", "detector"),
        ("pt", "detector"),
        # A button label is short too: it keeps the language of the case.
        ("pt", "previous"),
        ("pt", "previous"),
    ]


def test_a_security_stop_answers_in_the_detected_language_without_the_llm(
    client: TestClient, schema: SchemaUrls
) -> None:
    body = client.post("/chat", json={"customer_id": "C2", "message": PT}).json()
    assert (body["outcome"], body["tokens"]) == ("security_blocked", 0)
    assert _in(body["reply"], "pt")
    owner = Database(schema.admin)
    with owner.session() as s:
        row = s.scalars(
            select(AuditRecord).where(
                AuditRecord.case_id == body["case_id"], AuditRecord.action == "security_event"
            )
        ).one()
        decision = row.result["language_decision"]
        language = s.get(Case, body["case_id"]).language
    owner.dispose()
    assert (decision["language"], decision["source"], language) == ("pt", "detector", "pt")

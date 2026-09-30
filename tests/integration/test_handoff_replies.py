"""What the customer is told when a case goes to a person (QA of TRZ-34, trace 1a38d6255719445f):
the reason in the customer's words, without internal rule names, when the review happens, and
neutral language. Code writes it; the LLM's text never reaches the customer on a handoff."""

import dataclasses
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.adapters.db.session import SchemaUrls
from app.main import create_app
from tests.agent_support import agent_deps, fake_llm, llm_settings, reading
from tests.auth_support import customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 17, 23, 59)
# What the LLM wrote in the trace: a doubt the customer never voiced, no reason, a contact
# promise and a gendered pronoun.
LLM_TEXT = (
    "Entendo que ainda tem dúvidas sobre essa transação. Vou encaminhar seu caso para nossa "
    "equipe especializada. Eles entrarão em contato para ajudá-lo."
)
BUTTON = {
    "es": "No reconozco el cargo de Netflix del 16 jun",
    "pt": "Não reconheço a cobrança de Netflix do dia 16 de jun",
}
STILL = {"es": "Sigo sin reconocerlo", "pt": "Continuo sem reconhecer"}
REASON = {
    "es": "Ya tienes una aclaración en curso",
    "pt": "Você já tem uma contestação em andamento",
}
REVIEW = {"es": "24 horas", "pt": "24 horas"}


@pytest.fixture
def client(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1", currency="USD")],
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
    # An open dispute complaint of 20 days ago: any new dispute goes to a person.
    engine = create_engine(schema.admin)
    created = NOW - timedelta(days=20)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO complaints (complaint_id, customer_id, creation_date, process_date, "
                "case_type, category, subcategory, reception_channel, has_affected_product, "
                "priority, status, sla_breached, is_repeat_complainer) VALUES ('CMP-OPEN0001', "
                "'C1', :c, :d, 'Claim', 'Transactions', 'Cargo no reconocido', 'App', true, "
                "'Medium', 'Open', false, false)"
            ),
            {"c": created, "d": created.date()},
        )
    engine.dispose()
    settings = llm_settings(database_url, log_level="WARNING")
    app = create_app(settings)
    llm = fake_llm(settings, reading("unrecognized_charge", "pt-BR"), reply=LLM_TEXT)
    app.state.runtime = dataclasses.replace(app.state.runtime, agent=agent_deps(settings, llm))
    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers.update(customer_headers(c, "C1"))
        yield c


def _escalate(client: TestClient, language: str) -> dict:
    first = client.post("/chat", json={"message": BUTTON[language], "transaction_id": "TX1"}).json()
    r = client.post(
        "/chat",
        json={
            "message": STILL[language],
            "case_id": first["case_id"],
            "recognition": "not_recognized",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "escalated", body
    return body


@pytest.mark.parametrize("language", ["es", "pt"])
def test_an_open_dispute_handoff_says_why_and_when_in_the_customers_words(
    client: TestClient, schema: SchemaUrls, language: str
) -> None:
    body = _escalate(client, language)

    reply = body["reply"]
    assert REASON[language] in reply and REVIEW[language] in reply
    assert body["case_id"] in reply
    # Nothing the customer never said, no rule names, no contact promise, no gendered pronoun.
    # (A case id is random hex, so the look-back is searched with its unit.)
    wrong_words = ("dúvidas", "ajudá-lo", "entrarão em contato", "open_dispute", "90 d", "regla")
    for wrong in wrong_words:
        assert wrong not in reply, wrong
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        rule = conn.execute(
            text("SELECT escalation_reason FROM cases WHERE id = :c"), {"c": body["case_id"]}
        ).scalar()
    engine.dispose()
    assert rule == "escalate.open_dispute_last_90d"


def test_the_review_time_is_labeled_as_demo_policy(client: TestClient) -> None:
    reply = _escalate(client, "es")["reply"]
    assert "\n[simulado]" in reply


def test_the_llm_text_never_reaches_the_customer_on_a_handoff(client: TestClient) -> None:
    assert LLM_TEXT not in _escalate(client, "pt")["reply"]

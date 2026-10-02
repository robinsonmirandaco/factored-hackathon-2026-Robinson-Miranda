"""The receipt of a registration (walkthrough of TRZ-40, CASE-3EE8075C10, trace
7f109c453e9e4b2d, and CASE-316DDEF80F): code writes it, folio and deadline with its citation,
in Spanish and Portuguese. The LLM wrote contact promises the fact checker cannot see, so it is
not asked to write the receipt at all."""

import dataclasses
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.adapters.db.session import SchemaUrls
from app.adapters.llm import _REPLIES
from app.main import create_app
from tests.agent_support import agent_deps, fake_llm, llm_settings, reading
from tests.auth_support import customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 17, 23, 59)
# What the LLM wrote on the public URL: a promise of news and of a reply nobody checks.
LLM_TEXT = (
    "Te mantenemos informado del avance. Você receberá uma resposta conforme os prazos "
    "estabelecidos. Ficamos à disposição."
)
BUTTON = {
    "es": "No reconozco el cargo de Netflix del 16 jun",
    "pt": "Não reconheço a cobrança de Netflix do dia 16 de jun",
}
STILL = {"es": "Sigo sin reconocerlo", "pt": "Continuo sem reconhecer"}
YES = {"es": "Sí", "pt": "Sim"}
NOTE = {"es": "Plazo de respuesta", "pt": "Prazo de resposta"}


@pytest.fixture
def sent() -> list[str]:
    return []


@pytest.fixture
def client(schema: SchemaUrls, database_url: str, sent: list[str]) -> Iterator[TestClient]:
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
    settings = llm_settings(database_url, log_level="WARNING")
    app = create_app(settings)
    llm = fake_llm(settings, reading("unrecognized_charge"), sent=sent, reply=LLM_TEXT)
    app.state.runtime = dataclasses.replace(app.state.runtime, agent=agent_deps(settings, llm))
    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers.update(customer_headers(c, "C1"))
        yield c


def _register(client: TestClient, language: str) -> dict:
    first = client.post("/chat", json={"message": BUTTON[language], "transaction_id": "TX1"}).json()
    pending = client.post(
        "/chat",
        json={
            "message": STILL[language],
            "case_id": first["case_id"],
            "recognition": "not_recognized",
        },
    ).json()
    assert pending["outcome"] == "awaiting_confirmation", pending
    r = client.post(
        "/chat",
        json={
            "message": YES[language],
            "case_id": first["case_id"],
            "confirm_action_id": pending["pending_action"]["action_id"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "registered_verified", body
    return body


@pytest.mark.parametrize("language", ["es", "pt"])
def test_the_receipt_is_the_fixed_text_with_folio_and_deadline(
    client: TestClient, language: str
) -> None:
    body = _register(client, language)

    reply = body["reply"]
    folio = body["dispute_folio"]
    receipt = _REPLIES[language]["registered_verified"].format(folio=folio)
    assert reply.startswith(receipt + " " + NOTE[language]), reply
    for promise in ("informado", "receberá", "disposição", "contacto", "contato"):
        assert promise not in reply, promise


def test_the_llm_is_not_asked_to_write_the_receipt(client: TestClient, sent: list[str]) -> None:
    _register(client, "es")

    # The fixed replies before the receipt are code-written too, so no reply request at all.
    assert not [b for b in sent if "Facts (JSON)" in b]

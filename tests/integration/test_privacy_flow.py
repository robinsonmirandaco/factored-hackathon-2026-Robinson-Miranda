"""One full customer turn against Postgres with a simulated LLM: no personal data of the message
or of the customer record reaches a prompt or the audit log."""

import json

import httpx
import pytest
from sqlalchemy import select

from app.adapters.db.models import AuditRecord
from app.adapters.db.session import Database, SchemaUrls
from app.adapters.llm import EXTRACT_SYSTEM, VALIDATE_SYSTEM, LLMClient
from app.core.config import Settings
from app.domain.clock import SimulatedClock
from app.domain.policy import PolicyEngine
from app.services.agent import AgentDeps, handle_message
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

FIRST_NAME = "Valentina"
DOCUMENT_NUMBER = "1023456789"
MESSAGE = (
    "Hola, mi cédula es 1.023.456.789. No reconozco el cargo de 5000 en Walmart "
    "con la tarjeta 4111 1111 1111 1111. Escríbanme a valentina.r@mail.com o al "
    "+57 300 123 4567."
)
MESSAGE_PII = [
    "1.023.456.789",
    "4111 1111 1111 1111",
    "4111111111111111",
    "valentina.r@mail.com",
    "+57 300 123 4567",
    "300 123 4567",
]


def _fake_llm(settings: Settings, sent: list[str]) -> LLMClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(json.dumps(body, ensure_ascii=False))
        system = body["messages"][0]["content"]
        if system.startswith(EXTRACT_SYSTEM[:40]):
            content = json.dumps(
                {
                    "intent": "unrecognized_charge",
                    "amount": 5000,
                    "merchant": "Walmart",
                    "language": "es",
                    "customer_claims_legitimate": False,
                    "confidence": 0.9,
                }
            )
        elif system.startswith(VALIDATE_SYSTEM[:40]):
            content = '{"ok": true, "reason": ""}'
        else:
            content = "Revisaremos el cargo y te avisaremos."
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    http = httpx.Client(base_url=settings.llm_base_url, transport=httpx.MockTransport(handler))
    return LLMClient(settings, http_client=http)


def test_turn_sends_no_pii_to_llm_and_audits_redacted_input(
    schema: SchemaUrls, database_url: str
) -> None:
    settings = Settings(
        database_url=database_url,
        llm_enabled=True,
        llm_provider="local",
        llm_base_url="http://llm.test/v1",
        llm_model_primary="test-model",
        anthropic_api_key="",
    )
    load(
        schema.admin,
        [customer("C1", first_name=FIRST_NAME, document_number=DOCUMENT_NUMBER)],
        [card("P1", "C1")],
        [
            transaction(
                "TX1", "C1", "P1", settings.trazo_now, amount=5000.0, merchant_name="Walmart"
            )
        ],
    )
    sent: list[str] = []
    deps = AgentDeps(
        policy=PolicyEngine.from_file(settings.policy_path),
        llm=_fake_llm(settings, sent),
        clock=SimulatedClock(settings.trazo_now),
    )
    app_db = Database(schema.app)
    try:
        with app_db.session(customer_id="C1") as s:
            result = handle_message(s, deps, "C1", MESSAGE)
    finally:
        app_db.dispose()

    assert not result.llm_fallback
    assert len(sent) == 3  # extract, compose, validate
    for body in sent:
        assert FIRST_NAME not in body
        assert DOCUMENT_NUMBER not in body
        for value in MESSAGE_PII:
            assert value not in body

    owner = Database(schema.admin)
    try:
        with owner.session() as s:
            rows = (
                s.execute(select(AuditRecord).where(AuditRecord.case_id == result.case_id))
                .scalars()
                .all()
            )
            logged = [json.dumps([r.payload, r.result], ensure_ascii=False) for r in rows]
            extract_input = next(r.payload for r in rows if r.action == "extract")
    finally:
        owner.dispose()

    assert rows
    for row in logged:
        assert DOCUMENT_NUMBER not in row
        for value in MESSAGE_PII:
            assert value not in row
    assert extract_input["redacted_text"].startswith(
        "Hola, mi cédula es [DOCUMENTO]. No reconozco el cargo de 5000 en Walmart "
        "con la tarjeta [TARJETA]. Escríbanme a [CORREO] o al [TELEFONO]."
    )
    assert extract_input["pii"] == {
        "[DOCUMENTO]": 1,
        "[TARJETA]": 1,
        "[CORREO]": 1,
        "[TELEFONO]": 1,
    }

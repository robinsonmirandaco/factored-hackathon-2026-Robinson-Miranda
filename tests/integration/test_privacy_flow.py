"""One full customer turn against Postgres with a simulated LLM: no personal data of the message
or of the customer record reaches a prompt or the audit log."""

import json

import pytest
from sqlalchemy import select, text

from app.adapters.db.models import AuditRecord
from app.adapters.db.session import Database, SchemaUrls
from app.services.agent import handle_message
from tests.agent_support import agent_deps, fake_llm, llm_settings, reading
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

FIRST_NAME = "Valentina"
DOCUMENT_NUMBER = "1023456789"
MESSAGE = (
    "Hola, soy Valentina, mi cédula es 1.023.456.789. No reconozco el cargo de 5000 en Walmart "
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


def test_turn_sends_no_pii_to_llm_and_audits_redacted_input(
    schema: SchemaUrls, database_url: str
) -> None:
    settings = llm_settings(database_url)
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
    # A rate for the day, so the charge has a USD amount and is shown for recognition rather
    # than handed to a person.
    owner_db = Database(schema.admin)
    with owner_db.session() as s:
        s.execute(
            text(
                "INSERT INTO exchange_rates (date, source_currency, target_currency, "
                "exchange_rate) VALUES (:d, 'COP', 'USD', 0.00025)"
            ),
            {"d": settings.trazo_now.date()},
        )
    owner_db.dispose()
    sent: list[str] = []
    answer = reading(
        "unrecognized_charge",
        amount={"value": 5000, "currency": None, "approximate": False, "evidence": "5000"},
        merchant_hint={"value": "Walmart", "evidence": "en Walmart"},
    )
    deps = agent_deps(settings, fake_llm(settings, answer, sent))
    app_db = Database(schema.app)
    try:
        with app_db.session(customer_id="C1") as s:
            shown = handle_message(s, deps, "C1", MESSAGE)
            assert shown.outcome == "recognizing"
            # The close of a recognized charge is a reply the LLM still writes; the receipt of
            # a registration is written by code since the walkthrough of TRZ-40.
            result = handle_message(
                s,
                deps,
                "C1",
                "Ya lo reconozco",
                case_id=shown.case_id,
                recognition="recognized",
            )
    finally:
        app_db.dispose()

    assert (result.outcome, result.llm_fallback) == ("recognized_closed", False)
    # comprehend; the recognition step is written by code; then the compose of the close
    assert len(sent) == 2
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
            extract_input = next(r.payload for r in rows if r.action == "comprehend")
    finally:
        owner.dispose()

    assert rows
    for row in logged:
        assert FIRST_NAME not in row
        assert DOCUMENT_NUMBER not in row
        for value in MESSAGE_PII:
            assert value not in row
    assert extract_input["redacted_text"].startswith(
        "Hola, soy [NAME], mi cédula es [DOCUMENT]. No reconozco el cargo de 5000 en Walmart "
        "con la tarjeta [CARD]. Escríbanme a [EMAIL] o al [PHONE]."
    )
    assert extract_input["pii"] == {
        "[DOCUMENT]": 1,
        "[CARD]": 1,
        "[EMAIL]": 1,
        "[PHONE]": 1,
        "[NAME]": 1,
    }

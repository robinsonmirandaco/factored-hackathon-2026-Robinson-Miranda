"""Abstention out of scope in full customer turns (TRZ-23): the reply names where to go in the
customer's language (CA1), no tool is called and the LLM does not write it (CA2), for ten
requests in Spanish and ten in Portuguese (CA3)."""

import pytest
from sqlalchemy import create_engine, text

from app.adapters.db.session import Database, SchemaUrls
from app.adapters.llm import template_reply
from app.services.agent import handle_message
from tests.agent_support import agent_deps, fake_llm, llm_settings, reading
from tests.out_of_scope_data import PHRASES
from tests.serving_data import card, customer, load

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(("language", "topic", "message"), PHRASES)
def test_out_of_scope_calls_no_tool_and_redirects_in_the_customer_language(
    schema: SchemaUrls, database_url: str, language: str, topic: str, message: str
) -> None:
    load(schema.admin, [customer("C1")], [card("P1", "C1")], [])
    settings = llm_settings(database_url)
    sent: list[str] = []
    answer = reading("out_of_scope", "es-CO" if language == "es" else "pt-BR")
    deps = agent_deps(settings, fake_llm(settings, answer, sent))
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            r = handle_message(s, deps, "C1", message)
    finally:
        db.dispose()

    assert (r.intent, r.outcome, r.autonomy_level) == ("out_of_scope", "abstained", "L0")
    assert r.actions_taken == [] and r.llm_fallback is False
    assert r.reply == template_reply({"outcome": "abstained", "topic": topic}, language)
    # One LLM call, the comprehension: the redirect is not written by the LLM.
    assert len(sent) == 1
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        tools = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE case_id = :c AND actor = 'tool'"),
            {"c": r.case_id},
        ).scalar_one()
    engine.dispose()
    assert tools == 0

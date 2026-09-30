"""The customer's answer to a request for information (TRZ-28): the customer sees the question
in Mis aclaraciones (CA1, without the notification of TRZ-32); the answer is redacted, added to
the dossier and the case goes back to the queue marked updated (CA2); the full cycle of asking,
answering and coming back to the queue (CA4). POST /me/clarifications/{case_id}/reply with
success, validation and a failed dependency."""

import dataclasses
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.db.session import SchemaUrls
from app.api.deps import get_customer_session
from app.core.config import Settings
from app.main import create_app
from app.services.agent import AgentDeps
from tests.agent_support import agent_deps, fake_llm, llm_settings
from tests.auth_support import analyst_headers, customer_headers
from tests.integration.test_analyst_queue import (
    SEARS,
    BrokenSession,
    decide,
    disputed,
    query,
    reads,
    seed,
)

pytestmark = pytest.mark.integration

QUESTION = "¿Hiciste alguna compra en Sears ese día?"


@pytest.fixture
def settings(schema: SchemaUrls, database_url: str) -> Settings:
    settings = llm_settings(database_url, log_level="WARNING")
    seed(schema, settings.trazo_now)
    return settings


@pytest.fixture
def deps(settings: Settings) -> AgentDeps:
    return agent_deps(settings, fake_llm(settings, reads))


@pytest.fixture
def client(settings: Settings, deps: AgentDeps) -> Iterator[TestClient]:
    app = create_app(settings)
    app.state.runtime = dataclasses.replace(app.state.runtime, agent=deps)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def asked(client: TestClient, schema: SchemaUrls, deps: AgentDeps) -> str:
    """A large-amount case of C1 on which the analyst asked a question."""
    case_id = disputed(schema, deps, "C1", SEARS)
    client.headers.update(analyst_headers(client))
    assert decide(client, case_id, decision="need_info", question=QUESTION).status_code == 200
    client.headers.update(customer_headers(client, "C1"))
    return case_id


def _reply(client: TestClient, case_id: str, text: str, **headers: str) -> Any:
    return client.post(f"/me/clarifications/{case_id}/reply", json={"text": text}, headers=headers)


def test_ask_answer_and_back_to_the_queue(
    client: TestClient, asked: str, schema: SchemaUrls
) -> None:
    # CA1: the customer sees the question and its deadline in Mis aclaraciones.
    mine = next(c for c in client.get("/me/clarifications").json() if c["case_id"] == asked)
    assert mine["status"] == "awaiting_customer"
    request = mine["info_request"]
    assert (request["question"], request["due_on"], request["status"]) == (
        QUESTION,
        "2026-06-24",
        "open",
    )
    assert request["overdue"] is False

    # CA2: the answer is redacted and the case goes back with a person.
    r = _reply(client, asked, "No, y mi correo es ana.perez@correo.com")
    assert r.status_code == 200
    assert r.json()["status"] == "escalated"
    [(answer, status)] = query(
        schema, "SELECT answer, status FROM info_requests WHERE case_id = :c", c=asked
    )
    assert "ana.perez" not in answer and "[EMAIL]" in answer and status == "answered"
    [(payload,)] = query(
        schema,
        "SELECT payload FROM audit_log WHERE case_id = :c AND action = 'info_reply'",
        c=asked,
    )
    assert "ana.perez" not in str(payload)

    # CA4: back in the queue, marked updated, with a new SLA; the answer is in the dossier.
    analyst = analyst_headers(client)
    item = next(
        i for i in client.get("/queue", headers=analyst).json()["items"] if i["case_id"] == asked
    )
    assert (item["updated"], item["status"], item["reason"]) == (
        True,
        "escalated",
        "escalate.amount_above_human_review",
    )
    dossier = client.get(f"/cases/{asked}/dossier", headers=analyst).json()
    [exchange] = dossier["info_exchanges"]
    assert (exchange["question"], exchange["answer"], exchange["status"]) == (
        QUESTION,
        answer,
        "answered",
    )
    # The answer is told with its question, not again among the later messages.
    assert dossier["later_messages"] == []
    history = [h["text"] for h in client.get(f"/cases/{asked}/history", headers=analyst).json()]
    assert history[-1] == (
        f"El cliente respondió a la pregunta de la analista: «{answer}»; el caso volvió a la cola."
    )
    after = next(c for c in client.get("/me/clarifications").json() if c["case_id"] == asked)
    assert after["info_request"]["status"] == "answered"
    # QA 5: the customer sees the answer as it was stored, redacted.
    assert after["info_request"]["answer"] == answer

    # The analyst can decide again on the case that came back.
    r = client.post(
        f"/cases/{asked}/decision",
        json={"decision": "reject", "reason": "should_not_act"},
        headers=analyst,
    )
    assert r.status_code == 200 and r.json()["status"] == "rejected"


def test_an_answer_sent_twice_is_stored_once(
    client: TestClient, asked: str, schema: SchemaUrls
) -> None:
    first = _reply(client, asked, "No la hice")
    second = _reply(client, asked, "No la hice")

    assert second.status_code == 200 and second.json() == first.json()
    assert query(
        schema, "SELECT count(*) FROM case_queue WHERE case_id = :c AND updated", c=asked
    ) == [(1,)]


def test_another_customer_cannot_answer(client: TestClient, asked: str) -> None:
    r = _reply(client, asked, "Yo respondo", **customer_headers(client, "C2"))
    assert r.status_code == 404 and r.json()["error_code"] == "case_not_found"


def test_nothing_to_answer_is_409(client: TestClient, schema: SchemaUrls, deps: AgentDeps) -> None:
    case_id = disputed(schema, deps, "C1", SEARS)
    r = _reply(client, case_id, "Hola", **customer_headers(client, "C1"))
    assert r.status_code == 409 and r.json()["error_code"] == "no_open_request"


def test_an_empty_answer_is_422(client: TestClient, asked: str) -> None:
    r = _reply(client, asked, "")
    assert r.status_code == 422 and r.json()["error_code"] == "validation_error"


def test_an_analyst_token_cannot_answer(client: TestClient, asked: str) -> None:
    r = _reply(client, asked, "No", **analyst_headers(client))
    assert r.status_code == 403 and r.json()["error_code"] == "forbidden"


def test_an_answer_with_the_database_down_is_503(client: TestClient, asked: str) -> None:
    app: FastAPI = client.app  # type: ignore[assignment]
    app.dependency_overrides[get_customer_session] = lambda: BrokenSession()
    r = _reply(client, asked, "No")
    app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["error_code"] == "db_unavailable"


def test_a_chat_message_on_a_waiting_case_is_not_the_answer(
    client: TestClient, asked: str, schema: SchemaUrls
) -> None:
    r = client.post("/chat", json={"message": "No reconozco otro cargo", "case_id": asked})

    assert r.status_code == 200 and r.json()["case_id"] != asked
    assert query(schema, "SELECT status FROM cases WHERE id = :c", c=asked) == [
        ("awaiting_customer",)
    ]
    assert query(schema, "SELECT status FROM info_requests WHERE case_id = :c", c=asked) == [
        ("open",)
    ]


def test_two_questions_are_shown_each_with_its_answer_in_order(
    client: TestClient, asked: str
) -> None:
    analyst = analyst_headers(client)
    assert _reply(client, asked, "No la hice").status_code == 200
    second = "¿Tienes todavía la tarjeta?"
    r = client.post(
        f"/cases/{asked}/decision",
        json={"decision": "need_info", "question": second},
        headers=analyst,
    )
    assert r.status_code == 200
    assert _reply(client, asked, "Sí, la tengo").status_code == 200

    dossier = client.get(f"/cases/{asked}/dossier", headers=analyst).json()
    assert [(e["question"], e["answer"]) for e in dossier["info_exchanges"]] == [
        (QUESTION, "No la hice"),
        (second, "Sí, la tengo"),
    ]
    assert [e["source"]["table"] for e in dossier["info_exchanges"]] == ["info_requests"] * 2
    history = [h["text"] for h in client.get(f"/cases/{asked}/history", headers=analyst).json()]
    told = [h for h in history if "pedir información" in h or "respondió" in h]
    assert [("«" + QUESTION + "»") in told[0], "«No la hice»" in told[1]] == [True, True]
    assert [("«" + second + "»") in told[2], "«Sí, la tengo»" in told[3]] == [True, True]
    # Regression 1: the customer sees both, in order, each with its answer.
    mine = next(c for c in client.get("/me/clarifications").json() if c["case_id"] == asked)
    assert [(r["question"], r["answer"]) for r in mine["info_requests"]] == [
        (QUESTION, "No la hice"),
        (second, "Sí, la tengo"),
    ]

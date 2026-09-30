"""Bounded retries and safe fallbacks (TRZ-36, design 11.5): one test per row of the table that
has code today, the retry budget of a turn (CA3), the audit rows of each failure (CA4), and the
warm-up call at startup.

Rows of design 11.5 without code yet, declared in the pull request: mail outbox (TRZ-33),
degradation of a cell (TRZ-30) and the global switch (TRZ-35).
"""

import dataclasses
import json
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.adapters.llm import LLMClient
from app.api.deps import get_customer_session
from app.core.config import Settings
from app.main import create_app
from app.services import tools as T
from tests.agent_support import agent_deps, llm_settings, reading, still_not_recognized
from tests.auth_support import customer_headers
from tests.llm_support import anthropic_http, api_error, message, request_parts
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 17, 23, 59)
NETFLIX = "No reconozco un cargo de 120 dólares en Netflix"
# Nothing the rules recognize: without the LLM, no intent is known.
VAGUE = "hola, necesito que me ayuden con algo que pasó"
NETFLIX_READING = reading(
    "unrecognized_charge",
    amount={"value": 120, "currency": "USD", "approximate": False, "evidence": "120 dólares"},
    merchant_hint={"value": "Netflix", "evidence": "Netflix"},
)

Reply = Callable[[str, str], httpx.Response]


@pytest.fixture
def settings(schema: Any, database_url: str) -> Settings:
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
    return llm_settings(database_url, log_level="WARNING", llm_timeout_seconds=0.3)


class Llm:
    """A simulated Anthropic API that answers comprehension and reply calls apart and counts
    every request, retries included."""

    def __init__(self, settings: Settings, comprehend: Reply, compose: Reply) -> None:
        self.requests: list[str] = []
        self.client: LLMClient

        def handler(request: httpx.Request) -> httpx.Response:
            system, user, _ = request_parts(request)
            kind = (
                "comprehend"
                if system.startswith(self.client.comprehension_prompt.system[:40])
                else "compose"
            )
            self.requests.append(kind)
            return (comprehend if kind == "comprehend" else compose)(system, user)

        self.client = LLMClient(settings, http_client=anthropic_http(handler))


def _ok(content: str) -> Reply:
    return lambda _s, _u: message(content)


def _down(_s: str, _u: str) -> httpx.Response:
    return api_error(503)


def _slow(_s: str, _u: str) -> httpx.Response:
    time.sleep(1.0)  # past the 0.3 s deadline of each attempt
    return message(json.dumps(NETFLIX_READING))


@pytest.fixture
def make_client(settings: Settings) -> Iterator[Callable[[Llm], TestClient]]:
    opened: list[TestClient] = []

    def make(llm: Llm) -> TestClient:
        app = create_app(settings)
        runtime = app.state.runtime
        deps = agent_deps(settings, llm.client)
        app.state.runtime = dataclasses.replace(runtime, agent=deps)
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        client.headers.update(customer_headers(client, "C1"))
        opened.append(client)
        return client

    yield make
    for c in opened:
        c.__exit__(None, None, None)


def _rows(schema: Any, sql: str, **params: Any) -> list[tuple]:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        rows = [tuple(r) for r in conn.execute(text(sql), params)]
    engine.dispose()
    return rows


def _audit(schema: Any, case_id: str, action: str) -> dict[str, Any]:
    rows = _rows(
        schema,
        "SELECT result FROM audit_log WHERE case_id = :c AND action = :a ORDER BY id DESC LIMIT 1",
        c=case_id,
        a=action,
    )
    return rows[0][0]


# ---- row 1: LLM slow ------------------------------------------------------------------------


def test_a_slow_llm_is_retried_once_then_the_rules_and_the_fixed_reply_answer(
    make_client: Callable[[Llm], TestClient], settings: Settings, schema: Any
) -> None:
    llm = Llm(settings, _slow, _ok("Revisemos el cargo."))
    client = make_client(llm)

    t0 = time.perf_counter()
    body = client.post("/chat", json={"message": NETFLIX}).json()
    elapsed = time.perf_counter() - t0

    assert (body["intent"], body["outcome"], body["llm_fallback"]) == (
        "unrecognized_charge",
        "recognizing",
        True,
    )
    # One attempt and one retry of the comprehension; the recognition step needs no reply call.
    assert llm.requests == ["comprehend", "comprehend"]
    assert elapsed < 3.0
    read = _audit(schema, body["case_id"], "comprehend")
    assert (read["fallback"], read["attempts"], read["error"]) == (True, 2, "TimeoutError")


# ---- row 1 and CA3: the reply is not retried after the comprehension spent the retry --------


def test_after_the_comprehension_spent_the_retry_the_reply_gets_no_second_attempt(
    make_client: Callable[[Llm], TestClient], settings: Settings, schema: Any
) -> None:
    coppel = reading(
        "unrecognized_charge",
        amount={"value": 900, "currency": "USD", "approximate": False, "evidence": "900 dólares"},
        merchant_hint={"value": "Coppel", "evidence": "Coppel"},
    )
    attempts = iter([api_error(503), message(json.dumps(coppel))])
    llm = Llm(settings, lambda _s, _u: next(attempts), _down)
    client = make_client(llm)

    # No charge fits: the case is escalated, and that reply is written by the LLM.
    body = client.post("/chat", json={"message": "No reconozco 900 dólares en Coppel"}).json()

    assert body["outcome"] == "escalated"
    # comprehension: one failure and its retry; reply: one attempt, no retry left.
    assert llm.requests == ["comprehend", "comprehend", "compose"]
    wrote = _audit(schema, body["case_id"], "compose")
    assert (wrote["fallback"], wrote["attempts"]) == (True, 1)
    assert body["reply"].startswith("Pasamos tu caso a una analista")


# ---- row 1: LLM down; a case that needs comprehension escalates ----------------------------


def test_with_the_llm_down_a_message_the_rules_cannot_read_goes_to_a_person(
    make_client: Callable[[Llm], TestClient], settings: Settings, schema: Any
) -> None:
    llm = Llm(settings, _down, _down)
    client = make_client(llm)

    body = client.post("/chat", json={"message": VAGUE}).json()

    assert (body["outcome"], body["autonomy_level"]) == ("escalated", "L3")
    assert body["reply"].startswith("Pasamos tu caso a una analista")
    assert f"Tu número de caso es {body['case_id']}." in body["reply"]
    # Two attempts of the comprehension, and no reply call once the LLM is down in the turn.
    assert llm.requests == ["comprehend", "comprehend"]
    read = _audit(schema, body["case_id"], "comprehend")
    assert (read["fallback"], read["comprehension_unavailable"]) == (True, True)
    decide = _audit(schema, body["case_id"], "decide")
    assert decide["rule"] == "escalate.comprehension_unavailable"
    wrote = _audit(schema, body["case_id"], "compose")
    assert wrote["fallback"] is True


def test_with_the_llm_down_a_message_the_rules_read_is_decided_on_the_rules(
    make_client: Callable[[Llm], TestClient], settings: Settings
) -> None:
    client = make_client(Llm(settings, _down, _down))

    body = client.post("/chat", json={"message": NETFLIX}).json()

    assert (body["intent"], body["outcome"]) == ("unrecognized_charge", "recognizing")


def test_with_the_llm_down_a_lost_card_still_gets_the_urgent_block_redirect(
    make_client: Callable[[Llm], TestClient], settings: Settings
) -> None:
    client = make_client(Llm(settings, _down, _down))

    body = client.post("/chat", json={"message": "Me robaron la tarjeta"}).json()

    assert (body["outcome"], body["autonomy_level"]) == ("abstained", "L0")
    assert "Bloquea tu tarjeta de inmediato" in body["reply"]


def test_with_the_llm_down_the_reply_is_in_the_language_of_the_customer(
    make_client: Callable[[Llm], TestClient], settings: Settings
) -> None:
    client = make_client(Llm(settings, _down, _down))

    body = client.post("/chat", json={"message": "Quero um empréstimo pessoal"}).json()

    assert body["outcome"] == "abstained"
    assert "não conseguimos contratar empréstimos" in body["reply"]


# ---- row 2: invalid JSON --------------------------------------------------------------------


def test_invalid_json_gets_one_retry_then_the_rules(
    make_client: Callable[[Llm], TestClient], settings: Settings, schema: Any
) -> None:
    llm = Llm(settings, _ok("not json"), _ok("Revisemos el cargo."))
    client = make_client(llm)

    body = client.post("/chat", json={"message": NETFLIX}).json()

    assert (body["outcome"], body["llm_fallback"]) == ("recognizing", True)
    assert llm.requests == ["comprehend", "comprehend"]
    read = _audit(schema, body["case_id"], "comprehend")
    assert read["error"].startswith("invalid_json") and read["attempts"] == 2


# ---- row 3: an unfaithful fragment ----------------------------------------------------------


def test_a_clue_whose_fragment_is_not_in_the_message_is_dropped(
    make_client: Callable[[Llm], TestClient], settings: Settings, schema: Any
) -> None:
    invented = reading(
        "unrecognized_charge",
        amount={"value": 999, "currency": "USD", "approximate": False, "evidence": "999 dólares"},
        merchant_hint={"value": "Netflix", "evidence": "Netflix"},
    )
    client = make_client(Llm(settings, _ok(json.dumps(invented)), _ok("Revisemos el cargo.")))

    body = client.post("/chat", json={"message": "No reconozco un cargo en Netflix"}).json()

    read = _audit(schema, body["case_id"], "comprehend")
    assert read["dropped_clues"] == ["amount"] and read["amount"] is None


# ---- row 4: a tool answers ok without writing -----------------------------------------------


def test_a_tool_that_answers_ok_without_writing_leaves_the_case_failed(
    make_client: Callable[[Llm], TestClient],
    settings: Settings,
    schema: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(Llm(settings, _ok(json.dumps(NETFLIX_READING)), _ok("Listo.")))
    monkeypatch.setattr(
        T, "register_dispute", lambda *_a, **_k: T.ToolResult(True, {"folio": "DSP-2026-00042"})
    )
    first = client.post("/chat", json={"message": NETFLIX}).json()
    pending = still_not_recognized(client, first["case_id"])

    body = _confirm(client, first["case_id"], pending["pending_action"]["action_id"])

    assert (body["outcome"], body["dispute_folio"]) == ("failed", None)
    assert _rows(schema, "SELECT count(*) FROM disputes") == [(0,)]


# ---- row 5: a double send -------------------------------------------------------------------


def test_a_double_send_returns_the_same_folio_and_writes_once(
    make_client: Callable[[Llm], TestClient], settings: Settings, schema: Any
) -> None:
    client = make_client(Llm(settings, _ok(json.dumps(NETFLIX_READING)), _ok("Listo.")))
    first = client.post("/chat", json={"message": NETFLIX}).json()
    pending = still_not_recognized(client, first["case_id"])
    action = pending["pending_action"]["action_id"]

    once = _confirm(client, first["case_id"], action)
    twice = _confirm(client, first["case_id"], action)

    assert once["dispute_folio"] == twice["dispute_folio"] is not None
    assert _rows(schema, "SELECT count(*) FROM disputes") == [(1,)]


# ---- row 6: the database is down ------------------------------------------------------------


def test_with_the_database_down_the_customer_gets_the_maintenance_message(
    make_client: Callable[[Llm], TestClient], settings: Settings
) -> None:
    llm = Llm(settings, _ok(json.dumps(NETFLIX_READING)), _ok("Listo."))
    client = make_client(llm)

    class BrokenSession:
        info: dict[str, Any] = {}

        def get(self, *_args: object) -> None:
            raise OperationalError("SELECT", {}, Exception("connection refused"))

        execute = get

    app = client.app
    app.dependency_overrides[get_customer_session] = lambda: BrokenSession()  # type: ignore[attr-defined]
    r = client.post("/chat", json={"message": NETFLIX})
    app.dependency_overrides.clear()  # type: ignore[attr-defined]

    assert r.status_code == 503
    assert r.json()["error_code"] == "db_unavailable"
    assert "Estamos en mantenimiento" in r.json()["message"]
    assert "Estamos em manutenção" in r.json()["message"]
    assert llm.requests == []  # nothing was read, nothing was decided


def test_a_database_failure_in_the_middle_of_an_action_leaves_nothing_written(
    make_client: Callable[[Llm], TestClient],
    settings: Settings,
    schema: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(Llm(settings, _ok(json.dumps(NETFLIX_READING)), _ok("Listo.")))
    first = client.post("/chat", json={"message": NETFLIX}).json()
    pending = still_not_recognized(client, first["case_id"])
    register = T.register_dispute

    def registers_then_the_database_fails(*args: Any, **kwargs: Any) -> T.ToolResult:
        register(*args, **kwargs)
        raise OperationalError("INSERT", {}, Exception("server closed the connection"))

    monkeypatch.setattr(T, "register_dispute", registers_then_the_database_fails)
    r = client.post(
        "/chat",
        json={
            "message": "sí",
            "case_id": first["case_id"],
            "confirm_action_id": pending["pending_action"]["action_id"],
        },
    )

    assert r.status_code == 503 and r.json()["error_code"] == "db_unavailable"
    assert _rows(schema, "SELECT count(*) FROM disputes") == [(0,)]
    (status,) = _rows(
        schema, "SELECT status FROM case_actions WHERE case_id = :c", c=first["case_id"]
    )
    assert status == ("pending",)


# ---- warm-up at startup ---------------------------------------------------------------------


def test_the_warm_up_runs_in_the_background_at_startup(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()

    def slow_warm_up(_self: LLMClient) -> None:
        started.set()
        time.sleep(2)

    monkeypatch.setattr(LLMClient, "warm_up", slow_warm_up)
    app = create_app(settings.model_copy(update={"llm_warm_up": True}))
    t0 = time.perf_counter()
    with TestClient(app) as client:
        health = client.get("/health")
    elapsed = time.perf_counter() - t0

    assert health.status_code == 200
    assert started.wait(timeout=3)
    assert elapsed < 1.5  # the 2 s warm-up did not hold the startup


def test_without_a_key_there_is_no_warm_up(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[bool] = []
    monkeypatch.setattr(LLMClient, "warm_up", lambda _self: called.append(True))
    no_key = settings.model_copy(update={"llm_warm_up": True, "anthropic_api_key": ""})

    with TestClient(create_app(no_key)) as client:
        client.get("/health")

    assert called == []


def _confirm(client: TestClient, case_id: str, action_id: str) -> dict[str, Any]:
    r = client.post(
        "/chat", json={"message": "sí", "case_id": case_id, "confirm_action_id": action_id}
    )
    assert r.status_code == 200, r.text
    return r.json()

"""The trace of a case in the customer's audit view of the demo (TRZ-34 CA4), and the analyst's
raw trace of a case (TRZ-26), on Postgres.

The customer's trace is the history of the case in plain language, in the customer's language:
the rule and the cell with r, W and N of step 7 of the walkthrough, without the analyst's user
name, without percentages and without real-clock dates. It exists only in demo mode and only for
the customer's own cases.
"""

import json
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.adapters.db.session import SchemaUrls
from app.api.deps import get_analyst_session, get_customer_session
from app.core.config import Settings
from app.main import create_app
from tests.agent_support import still_not_recognized
from tests.auth_support import analyst_headers, customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 17, 23, 59)
# The block of step 6 of the walkthrough: 10 of 20 took the PT-BR cell down to A1.
CHANGE = {
    "level_before": "A0",
    "level_after": "A1",
    "changed": True,
    "n": 20,
    "reversals": 10,
    "r": 0.5,
    "w": 0.3274,
    "z": 1.645,
    "threshold": "demote_if_wilson_lower_gte",
    "threshold_value": 0.3,
    "good_blocks": 0,
    "audit_id": 1,
}


def _settings(database_url: str, demo: bool = True) -> Settings:
    return Settings(
        database_url=database_url, llm_enabled=False, log_level="WARNING", demo_mode=demo
    )


@pytest.fixture
def rows(schema: SchemaUrls) -> SchemaUrls:
    load(
        schema.admin,
        [customer("C1"), customer("C2")],
        [card("P1", "C1", currency="USD"), card("P2", "C2", currency="USD")],
        [
            transaction(
                "TX1",
                "C1",
                "P1",
                NOW - timedelta(hours=5),
                amount=120.0,
                currency="USD",
                merchant_name="Netflix",
            )
        ],
    )
    engine = create_engine(schema.admin)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO autonomy_cells (intent, language, level, last_change) "
                "VALUES ('unrecognized_charge', 'pt', 'A1', CAST(:c AS jsonb))"
            ),
            {"c": json.dumps(CHANGE)},
        )
    engine.dispose()
    return schema


@pytest.fixture
def client(rows: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(database_url)), raise_server_exceptions=False) as c:
        yield c


def _step_seven(client: TestClient) -> str:
    """The next PT-BR charge after the cell went down: it waits for an analyst."""
    who = customer_headers(client, "C1")
    first = client.post(
        "/chat", json={"message": "Não reconheço", "transaction_id": "TX1"}, headers=who
    )
    assert first.status_code == 200, first.text
    turn = still_not_recognized(client, first.json()["case_id"], who)
    assert turn["outcome"] == "pending_analyst_approval", turn
    return str(turn["case_id"])


def _trace(client: TestClient, case_id: str, lang: str, who: str = "C1") -> Any:
    return client.get(
        f"/me/clarifications/{case_id}/trace",
        params={"lang": lang},
        headers=customer_headers(client, who),
    )


def _steps(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for turn in body["turns"] for step in turn["steps"]]


def test_the_trace_of_step_seven_tells_the_rule_and_the_cell_in_portuguese(
    client: TestClient,
) -> None:
    case_id = _step_seven(client)
    r = _trace(client, case_id, "pt")
    assert r.status_code == 200, r.text
    steps = _steps(r.json())
    [decide] = [s["text"] for s in steps if (s["actor"], s["action"]) == ("policy", "decide")]
    assert "regra «approval.autonomy_a1»" in decide
    assert (
        "A célula está em A1 desde um bloco de revisões com 10 de 20, r = 0,50, W = 0,327 ≥ 0,30."
        in decide
    )
    spanish = _steps(_trace(client, case_id, "es").json())
    assert any("regla «approval.autonomy_a1»" in s["text"] for s in spanish)


def test_the_analyst_is_not_named_in_the_customers_trace(client: TestClient) -> None:
    case_id = _step_seven(client)
    decided = client.post(
        f"/cases/{case_id}/decision",
        json={"decision": "reject", "reason": "insufficient_data"},
        headers=analyst_headers(client),
    )
    assert decided.status_code == 200, decided.text
    steps = _steps(_trace(client, case_id, "es").json())
    [line] = [s["text"] for s in steps if (s["actor"], s["action"]) == ("human", "decision")]
    assert line == "La analista decidió rechazar: datos insuficientes."
    assert "analista.demo" not in json.dumps(steps)
    # The analyst's own history still names her.
    history = client.get(f"/cases/{case_id}/history", headers=analyst_headers(client)).json()
    assert any("La analista analista.demo decidió rechazar" in h["text"] for h in history)


def test_the_trace_has_no_percentage_no_raw_row_and_no_real_clock_date(
    client: TestClient,
) -> None:
    trace = _trace(client, _step_seven(client), "pt").json()
    # The bank date is of the simulated clock; nothing else carries a date or a clock time.
    trace.pop("bank_date")
    body = json.dumps(trace, ensure_ascii=False)
    assert "%" not in body
    assert '"payload"' not in body and '"result"' not in body
    assert re.search(r"\d{4}-\d{2}-\d{2}", body) is None
    assert re.search(r"\d{1,2}:\d{2}", body) is None


def test_another_customers_case_is_not_found(client: TestClient) -> None:
    case_id = _step_seven(client)
    r = _trace(client, case_id, "es", who="C2")
    assert r.status_code == 404 and r.json()["error_code"] == "case_not_found"
    assert _trace(client, "CASE-UNKNOWN", "es").status_code == 404


def test_the_trace_needs_a_customer_session_and_a_known_language(client: TestClient) -> None:
    case_id = _step_seven(client)
    assert client.get(f"/me/clarifications/{case_id}/trace").status_code == 401
    r = client.get(f"/me/clarifications/{case_id}/trace", headers=analyst_headers(client))
    assert r.status_code == 403
    assert _trace(client, case_id, "en").status_code == 422


def test_outside_demo_mode_the_trace_does_not_exist(rows: SchemaUrls, database_url: str) -> None:
    app = create_app(_settings(database_url, demo=False))
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/me/clarifications/CASE-1/trace")
        assert r.status_code == 404 and r.json()["error_code"] == "http_404"


class BrokenSession:
    info: dict[str, Any] = {}

    def get(self, *_args: object) -> None:
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    execute = get


def test_the_trace_with_the_database_down_is_503(client: TestClient) -> None:
    app: FastAPI = client.app  # type: ignore[assignment]
    headers = customer_headers(client, "C1")
    app.dependency_overrides[get_customer_session] = lambda: BrokenSession()
    r = client.get("/me/clarifications/CASE-1/trace", headers=headers)
    app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["error_code"] == "db_unavailable"


# ---- the analyst's raw trace (TRZ-26) -------------------------------------------------------


def test_the_analyst_reads_every_audit_row_of_a_case_in_write_order(client: TestClient) -> None:
    case_id = _step_seven(client)
    r = client.get(f"/cases/{case_id}/trace", headers=analyst_headers(client))
    assert r.status_code == 200, r.text
    ids = [e["id"] for e in r.json()]
    assert ids == sorted(ids) and len(ids) > 1
    assert ("policy", "decide") in {(e["actor"], e["action"]) for e in r.json()}


def test_the_raw_trace_of_an_unknown_case_is_404(client: TestClient) -> None:
    r = client.get("/cases/CASE-UNKNOWN/trace", headers=analyst_headers(client))
    assert r.status_code == 404 and r.json()["error_code"] == "case_not_found"


def test_the_raw_trace_with_the_database_down_is_503(client: TestClient) -> None:
    app: FastAPI = client.app  # type: ignore[assignment]
    app.dependency_overrides[get_analyst_session] = lambda: BrokenSession()
    r = client.get("/cases/CASE-1/trace")
    app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["error_code"] == "db_unavailable"


# ---- the trace with structure: header, turns, numbered steps and time ---------------------


def test_the_trace_has_its_header_and_its_steps_grouped_by_turn(client: TestClient) -> None:
    case_id = _step_seven(client)
    body = _trace(client, case_id, "pt").json()
    assert set(body) == {"case_id", "bank_date", "language", "status", "turns"}
    assert (body["case_id"], body["language"], body["status"]) == (
        case_id,
        "pt",
        "pending_analyst_approval",
    )
    # No dispute yet: the bank date is the day of the simulated clock.
    assert body["bank_date"] == NOW.date().isoformat()
    turns = body["turns"]
    assert set(turns[0]) == {"number", "kind", "header", "steps"}
    assert set(turns[0]["steps"][0]) == {
        "number",
        "id",
        "trace_id",
        "actor",
        "action",
        "text",
        "offset_ms",
        "duration_ms",
    }
    assert [(t["number"], t["kind"]) for t in turns] == [(1, "button"), (2, "recognition")]
    assert [t["header"] for t in turns] == [
        "Você tocou em «Não reconheço» em uma movimentação",
        "Você disse que não reconhece a cobrança",
    ]
    steps = _steps(body)
    assert [s["number"] for s in steps] == list(range(1, len(steps) + 1))
    assert [s["id"] for s in steps] == sorted(s["id"] for s in steps)
    assert all(len({s["trace_id"] for s in t["steps"]}) == 1 for t in turns)
    for turn in turns:
        offsets = [s["offset_ms"] for s in turn["steps"]]
        assert offsets[0] == 0 and offsets == sorted(offsets)
    [done] = [s for s in turns[0]["steps"] if s["action"] == "turn_complete"]
    assert isinstance(done["duration_ms"], int)


def test_the_bank_date_is_the_one_of_the_dispute_when_the_case_has_one(
    client: TestClient, rows: SchemaUrls
) -> None:
    who = customer_headers(client, "C1")
    first = client.post(
        "/chat", json={"message": "No lo reconozco", "transaction_id": "TX1"}, headers=who
    ).json()
    turn = still_not_recognized(client, first["case_id"], who)
    pending = turn["pending_action"]["action_id"]
    done = client.post(
        "/chat",
        json={"message": "sí", "case_id": first["case_id"], "confirm_action_id": pending},
        headers=who,
    ).json()
    assert done["outcome"] == "registered_verified", done
    engine = create_engine(rows.admin)
    with engine.begin() as conn:
        conn.execute(text("UPDATE disputes SET business_at = '2026-06-10 12:00'"))
    engine.dispose()
    body = _trace(client, first["case_id"], "es").json()
    assert body["bank_date"] == "2026-06-10"
    assert [t["kind"] for t in body["turns"]] == ["button", "recognition", "confirmation"]
    assert body["turns"][2]["header"] == "Confirmaste la acción"

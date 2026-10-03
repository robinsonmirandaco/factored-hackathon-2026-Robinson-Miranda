"""The metrics endpoint (TRZ-37) on Postgres: every figure comes from the audit log.

The audit rows here are written straight into an empty database, with no case in the cases
table: the metrics still count them, so nothing is read from separate counters (CA2). The cells
rebuilt from the audit log are compared with autonomy_cells in test_autonomy.py.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.adapters.db.session import SchemaUrls
from app.api.deps import get_analyst_session
from app.core.config import Settings
from app.main import create_app
from tests.auth_support import analyst_headers, customer_headers
from tests.serving_data import customer, load

pytestmark = pytest.mark.integration


@pytest.fixture
def client(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    load(schema.admin, [customer("C1")], [], [])
    settings = Settings(database_url=database_url, llm_enabled=False, log_level="WARNING")
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


def _audit(schema: SchemaUrls, rows: list[dict[str, Any]]) -> None:
    engine = create_engine(schema.admin)
    with engine.begin() as conn:
        for i, row in enumerate(rows):
            conn.execute(
                text(
                    "INSERT INTO audit_log (trace_id, case_id, customer_id, actor, action, "
                    "payload, result, latency_ms, input_tokens, output_tokens, cost_usd) "
                    "VALUES (:trace, :case_id, 'C1', :actor, :action, CAST(:payload AS jsonb), "
                    "CAST(:result AS jsonb), :latency, :input, :output, :cost)"
                ),
                {
                    "trace": f"t{i}",
                    "case_id": row.get("case_id"),
                    "actor": row["actor"],
                    "action": row["action"],
                    "payload": json.dumps(row.get("payload")),
                    "result": json.dumps(row.get("result")),
                    "latency": row.get("latency_ms"),
                    "input": row.get("input_tokens"),
                    "output": row.get("output_tokens"),
                    "cost": row.get("cost_usd"),
                },
            )
    engine.dispose()


def _turn(case_id: str, outcome: str, ms: int) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "actor": "agent",
        "action": "turn_complete",
        "result": {"outcome": outcome, "tokens": 0},
        "latency_ms": ms,
    }


def _llm(case_id: str, tokens_in: int, tokens_out: int, cost: float) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "actor": "agent",
        "action": "comprehend",
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost_usd": cost,
    }


def test_the_metrics_are_computed_from_the_audit_log(
    client: TestClient, schema: SchemaUrls
) -> None:
    _audit(
        schema,
        [
            _turn("A", "informed", 100),
            _llm("A", 1000, 200, 0.0012),
            _turn("B", "escalated", 200),
            _turn("B", "informed", 300),
            {"case_id": "C", "actor": "system", "action": "mark_simulated"},
            _turn("C", "pending_analyst_approval", 400),
            _llm("C", 500, 100, 0.0006),
            _turn("D", "security_blocked", 500),
            # Rows that are not a customer turn count for nothing but their usage.
            {"case_id": "E", "actor": "policy", "action": "decide", "latency_ms": 9000},
        ],
    )
    r = client.get("/metrics", headers=analyst_headers(client))
    assert r.status_code == 200, r.text
    m = r.json()
    assert (m["cases"], m["contained"], m["containment"], m["turns"]) == (4, 1, 0.25, 5)
    assert m["handed_to_person"] == {
        "escalated": 1,
        "pending_analyst_approval": 1,
        "security_blocked": 1,
        "failed": 0,
    }
    # percentile_cont over 100..500: the median, and 400 + 0.8 * 100.
    assert m["turn_latency_ms"] == {"p50": 300.0, "p95": 480.0}
    assert (m["input_tokens"], m["output_tokens"], m["cost_usd"]) == (1500, 300, 0.0018)
    assert m["simulated"] == {
        "label": "[simulado]",
        "cases": 1,
        "contained": 0,
        "handed_to_person": 1,
    }
    assert m["autonomy"] == []


def test_a_case_counts_once_by_the_turn_that_handed_it_over_first(
    client: TestClient, schema: SchemaUrls
) -> None:
    _audit(schema, [_turn("A", "pending_analyst_approval", 10), _turn("A", "failed", 20)])
    m = client.get("/metrics", headers=analyst_headers(client)).json()
    assert m["cases"] == 1
    assert m["handed_to_person"]["pending_analyst_approval"] == 1
    assert m["handed_to_person"]["failed"] == 0


def test_an_empty_audit_log_gives_zeros_and_no_rates(client: TestClient) -> None:
    m = client.get("/metrics", headers=analyst_headers(client)).json()
    assert (m["cases"], m["containment"], m["turn_latency_ms"], m["cost_usd"]) == (
        0,
        None,
        None,
        0.0,
    )


def test_the_cells_count_the_reviews_of_the_open_block(
    client: TestClient, schema: SchemaUrls
) -> None:
    cell = {"intent": "unrecognized_charge", "language": "pt"}
    review = {
        "actor": "human",
        "action": "decision",
        "payload": {"decision": "reject"},
        "result": {"review": {"cell": cell, "reversal": True, "reason": "wrong_charge"}},
    }
    _audit(schema, [review, {**review, "result": {"status": "approved"}}, review])
    m = client.get("/metrics", headers=analyst_headers(client)).json()
    assert m["autonomy"] == [
        {
            "intent": "unrecognized_charge",
            "language": "pt",
            "level": "A0",
            "block_reviews": 2,
            "block_reversals": 2,
            "good_blocks": 0,
        }
    ]


def test_the_metrics_are_for_the_analyst_only(client: TestClient) -> None:
    assert client.get("/metrics").status_code == 401
    r = client.get("/metrics", headers=customer_headers(client, "C1"))
    assert r.status_code == 403 and r.json()["error_code"] == "forbidden"


class BrokenSession:
    info: dict[str, Any] = {}

    def execute(self, *_args: object) -> None:
        raise OperationalError("SELECT", {}, Exception("connection refused"))


def test_the_metrics_with_the_database_down_are_503(client: TestClient) -> None:
    app: FastAPI = client.app  # type: ignore[assignment]
    app.dependency_overrides[get_analyst_session] = lambda: BrokenSession()
    r = client.get("/metrics")
    app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["error_code"] == "db_unavailable"

"""Audit log against Postgres (TRZ-26): what every step records (CA1), the trace_id of each row
(CA2), the plain-language history (CA4) and the absence of model reasoning (CA5)."""

import dataclasses
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.adapters.db.audit import REASONING_KEYS
from app.adapters.db.migrations import MIGRATIONS_DIR, apply_migrations
from app.adapters.db.models import AuditRecord, Case
from app.adapters.db.session import Database, SchemaUrls
from app.adapters.llm import load_comprehension_prompt
from app.api.deps import get_analyst_session
from app.core.config import Settings
from app.main import create_app
from tests.agent_support import fake_llm, llm_settings, reading
from tests.auth_support import analyst_headers, customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

PROMPT_VERSION = load_comprehension_prompt(Path("config/prompts/comprehension.yaml")).version
MESSAGE = "No reconozco el cargo de 100 dólares en Walmart"


ANSWER = reading(
    "unrecognized_charge",
    amount={"value": 100, "currency": "USD", "approximate": False, "evidence": "100 dólares"},
    merchant_hint={"value": "Walmart", "evidence": "en Walmart"},
)


@pytest.fixture
def app(database_url: str, schema: SchemaUrls) -> FastAPI:
    settings = llm_settings(database_url, log_level="WARNING")
    app = create_app(settings)
    runtime = app.state.runtime
    agent = dataclasses.replace(runtime.agent, llm=fake_llm(settings, ANSWER))
    app.state.runtime = dataclasses.replace(runtime, agent=agent)
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1")],
        [
            transaction(
                "TX1", "C1", "P1", settings.trazo_now, currency="USD", merchant_name="Walmart"
            )
        ],
    )
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers.update(customer_headers(c, "C1"))
        yield c


def _rows(schema: SchemaUrls, case_id: str) -> list[AuditRecord]:
    owner = Database(schema.admin)
    with owner.session() as s:
        rows = list(
            s.execute(
                select(AuditRecord).where(AuditRecord.case_id == case_id).order_by(AuditRecord.id)
            ).scalars()
        )
    owner.dispose()
    return rows


def _two_turns(client: TestClient) -> tuple[str, str, str]:
    first = client.post("/chat", json={"message": MESSAGE})
    assert first.status_code == 200, first.text
    case_id = first.json()["case_id"]
    second = client.post(
        "/chat",
        json={"message": "sí, confirmo", "case_id": case_id, "confirm": True},
    )
    assert second.status_code == 200, second.text
    return case_id, first.headers["x-trace-id"], second.headers["x-trace-id"]


def test_every_row_of_a_case_carries_the_trace_id_of_its_request(
    client: TestClient, schema: SchemaUrls
) -> None:
    case_id, first, second = _two_turns(client)
    rows = _rows(schema, case_id)

    assert first != second
    by_trace = {t: [r for r in rows if r.trace_id == t] for t in (first, second)}
    assert by_trace[first] and by_trace[second]
    assert len(by_trace[first]) + len(by_trace[second]) == len(rows)
    # Rows of the first request were all written before any row of the second.
    assert max(r.id for r in by_trace[first]) < min(r.id for r in by_trace[second])

    owner = Database(schema.admin)
    with owner.session() as s:
        case = s.get(Case, case_id)
        assert case is not None and case.trace_id == first
        outside = s.execute(
            select(AuditRecord).where(
                AuditRecord.trace_id.in_([first, second]), AuditRecord.case_id != case_id
            )
        ).all()
    owner.dispose()
    assert outside == []


def test_each_step_records_actor_input_result_latency_cost_and_versions(
    client: TestClient, schema: SchemaUrls
) -> None:
    case_id, _, _ = _two_turns(client)
    rows = _rows(schema, case_id)
    llm_steps = {"comprehend", "compose"}

    assert {r.action for r in rows} >= {"comprehend", "decide", "confirm", "open_dispute"}
    for r in rows:
        assert r.actor and r.action and r.result is not None, r.action
        assert r.policy_version == "2026.09.1", r.action
        assert r.verified is None  # the read-back arrives with TRZ-19
        if r.action in llm_steps:
            assert r.model == "test-model"
            assert r.input_tokens and r.input_tokens > 0
            assert r.output_tokens and r.output_tokens > 0
            assert r.cost_usd and r.cost_usd > 0
            assert r.latency_ms is not None
            # Comprehension cites its versioned prompt file; compose its content hash.
            versioned = r.prompt_version == PROMPT_VERSION
            assert versioned or (r.prompt_version or "").startswith("sha256:"), r.action
        else:
            assert (r.model, r.prompt_version, r.input_tokens, r.cost_usd) == (
                None,
                None,
                None,
                None,
            ), r.action
    compose = next(r for r in rows if r.action == "compose")
    # Compose and its validator are two prompts, and the row cites both.
    assert "+" in (compose.prompt_version or "")
    extract = next(r for r in rows if r.action == "comprehend")
    assert extract.payload and "redacted_text" in extract.payload


def test_no_row_stores_model_reasoning(client: TestClient, schema: SchemaUrls) -> None:
    case_id, _, _ = _two_turns(client)

    def keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return {str(k).lower() for k in value} | set().union(*map(keys, value.values()))
        if isinstance(value, list):
            return set().union(*map(keys, value)) if value else set()
        return set()

    for r in _rows(schema, case_id):
        assert not (keys(r.payload) | keys(r.result)) & REASONING_KEYS, r.action


def test_the_database_refuses_a_row_outside_a_request(schema: SchemaUrls) -> None:
    engine = create_engine(schema.admin)
    with pytest.raises(IntegrityError, match="ck_audit_log_trace_id"):
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO audit_log (trace_id, actor, action) VALUES ('-', 'tool', 'x')")
            )
    engine.dispose()


def test_a_client_trace_id_of_a_dash_is_replaced(client: TestClient) -> None:
    r = client.post("/chat", json={"message": MESSAGE}, headers={"x-trace-id": "-"})
    assert r.status_code == 200
    assert r.headers["x-trace-id"] != "-"


def test_the_migration_keeps_rows_written_before_it(tmp_path: Path) -> None:
    settings = Settings()
    assert settings.admin_database_url
    name = f"legacy_{uuid.uuid4().hex[:12]}"
    folder = tmp_path / "migrations"
    folder.mkdir()
    for sql in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if sql.name < "0005":
            shutil.copy(sql, folder / sql.name)

    def scoped(url: str) -> str:
        from sqlalchemy import make_url

        u = make_url(url).update_query_dict({"options": f"-csearch_path={name}"})
        return u.render_as_string(hide_password=False)

    admin, app = scoped(settings.admin_database_url), scoped(settings.database_url)
    root = create_engine(settings.admin_database_url)
    with root.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {name}"))
    try:
        apply_migrations(admin, app, folder)
        engine = create_engine(admin)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO audit_log (trace_id, case_id, actor, action, result) "
                    "VALUES ('old', 'K1', 'tool', 'lookup_transaction', '{\"count\": 1}')"
                )
            )

        applied = apply_migrations(admin, app)
        assert applied[0] == "0005_audit_fields"
        assert applied == sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql") if p.name >= "0005")

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT trace_id, action, verified, model, policy_version "
                    "FROM case_history WHERE case_id = 'K1'"
                )
            ).one()
        engine.dispose()
        assert tuple(row) == ("old", "lookup_transaction", None, None, None)
    finally:
        with root.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {name} CASCADE"))
        root.dispose()


def test_history_tells_each_step_in_spanish_and_portuguese(
    client: TestClient, schema: SchemaUrls
) -> None:
    first = client.post("/chat", json={"message": MESSAGE})
    case_id = first.json()["case_id"]
    rows = _rows(schema, case_id)

    analyst = analyst_headers(client)
    es = client.get(f"/cases/{case_id}/history", headers=analyst)
    pt = client.get(f"/cases/{case_id}/history", params={"lang": "pt"}, headers=analyst)

    assert es.status_code == 200 and pt.status_code == 200
    assert [e["id"] for e in es.json()] == [r.id for r in rows]
    assert {e["trace_id"] for e in es.json()} == {first.headers["x-trace-id"]}
    assert es.json()[0]["text"] == (
        "El sistema entendió el mensaje del cliente como «cargo no reconocido»."
    )
    assert pt.json()[0]["text"] == (
        "O sistema entendeu a mensagem do cliente como «cobrança não reconhecida»."
    )
    decide = next(e for e in es.json() if e["action"] == "decide")
    assert decide["text"].startswith("La política v2026.09.1")
    assert all(e["text"] != p["text"] for e, p in zip(es.json(), pt.json(), strict=True))
    assert "Walmart" not in es.text and "100" not in es.text


def test_history_of_an_unknown_case_is_404(client: TestClient) -> None:
    r = client.get("/cases/CASE-NOPE/history", headers=analyst_headers(client))
    assert r.status_code == 404
    assert r.json()["error_code"] == "case_not_found"


def test_history_rejects_an_unsupported_language(client: TestClient) -> None:
    r = client.get(
        "/cases/CASE-NOPE/history", params={"lang": "fr"}, headers=analyst_headers(client)
    )
    assert r.status_code == 422
    assert set(r.json()) == {"error_code", "message", "trace_id"}
    assert r.json()["error_code"] == "validation_error"


def test_history_reports_a_database_failure_as_503(app: FastAPI, client: TestClient) -> None:
    class BrokenSession:
        info: dict[str, Any] = {}

        def get(self, *_args: object) -> None:
            raise OperationalError("SELECT", {}, Exception("connection refused"))

        execute = get

    app.dependency_overrides[get_analyst_session] = lambda: BrokenSession()
    r = client.get("/cases/CASE-1/history")
    assert r.status_code == 503
    assert r.json()["error_code"] == "db_unavailable"
    assert r.json()["trace_id"] == r.headers["x-trace-id"]

"""The purge of redacted conversations (TRZ-41), on Postgres, run as of a made-up later time.

CA1: the conversation text older than 90 days is replaced, in the audit log and in
info_requests. CA2: the audit log keeps every row, with its actor, action, ids and outcome, and
stays append-only for any other change. CA3: the dates are made up: the purge runs as of 91 days
from now, and as of 89 days nothing is touched.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.adapters.db.session import Database, SchemaUrls
from app.cli import jobs
from app.core.config import Settings
from app.core.time import utcnow
from app.domain.retention import PURGED_TEXT
from app.main import create_app
from tests.agent_support import still_not_recognized
from tests.auth_support import analyst_headers, customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 17, 23, 59)
# Words that appear only in what the customer and the analyst wrote.
MESSAGE = "No reconozco un cargo de 1500 dólares en Sears, estaba en Zanzibar"
QUESTION = "¿Estuviste en Quetzaltenango ese día?"
NOTE = "Revisar con Ushuaia"
ANSWER = "No, estaba en Tegucigalpa"
WORDS = ("Zanzibar", "Quetzaltenango", "Ushuaia", "Tegucigalpa")


@pytest.fixture
def client(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1")],
        [
            transaction(
                "C1S",
                "C1",
                "P1",
                NOW - timedelta(hours=40),
                amount=1500.0,
                currency="USD",
                merchant_name="Sears",
            )
        ],
    )
    settings = Settings(database_url=database_url, llm_enabled=False, log_level="WARNING")
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


def _query(schema: SchemaUrls, sql: str, **params: Any) -> list[tuple]:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        rows = [tuple(r) for r in conn.execute(text(sql), params)]
    engine.dispose()
    return rows


def _conversation(client: TestClient) -> str:
    """A message, a question of the analyst with a note, and the customer's answer."""
    who = customer_headers(client, "C1")
    first = client.post("/chat", json={"message": MESSAGE}, headers=who).json()
    case_id = str(still_not_recognized(client, first["case_id"], who)["case_id"])
    r = client.post(
        f"/cases/{case_id}/decision",
        json={"decision": "need_info", "question": QUESTION, "note": NOTE},
        headers=analyst_headers(client),
    )
    assert r.status_code == 200, r.text
    r = client.post(f"/me/clarifications/{case_id}/reply", json={"text": ANSWER}, headers=who)
    assert r.status_code == 200, r.text
    return case_id


def _purge(database_url: str, now: datetime) -> dict[str, Any]:
    db = Database(database_url)
    try:
        return jobs.purge_conversations(db, now, 90)
    finally:
        db.dispose()


def _text_of_every_row(schema: SchemaUrls) -> str:
    rows = _query(
        schema, "SELECT coalesce(payload::text, '') || coalesce(result::text, '') FROM audit_log"
    )
    asked = _query(schema, "SELECT question || coalesce(answer, '') FROM info_requests")
    return " ".join(r for (r,) in rows + asked)


ROWS = (
    "SELECT id, trace_id, case_id, customer_id, actor, action, created_at FROM audit_log "
    "WHERE action <> 'retention_purge' ORDER BY id"
)


def test_the_text_older_than_90_days_is_replaced_and_every_row_is_kept(
    client: TestClient, schema: SchemaUrls, database_url: str
) -> None:
    case_id = _conversation(client)
    before = _query(schema, ROWS)
    assert all(word in _text_of_every_row(schema) for word in WORDS)

    # CA3: 89 days later nothing is old enough.
    assert _purge(database_url, utcnow() + timedelta(days=89))["audit_rows"] == 0
    assert all(word in _text_of_every_row(schema) for word in WORDS)

    result = _purge(database_url, utcnow() + timedelta(days=91))
    # CA1: no word the customer or the analyst wrote is left, in any table.
    remaining = _text_of_every_row(schema)
    assert not [word for word in WORDS if word in remaining]
    assert PURGED_TEXT in remaining
    assert _query(schema, "SELECT question, answer FROM info_requests") == [
        (PURGED_TEXT, PURGED_TEXT)
    ]
    # CA2: every row is still there, the same; each run adds only its own row.
    assert _query(schema, ROWS) == before
    runs = _query(
        schema,
        "SELECT case_id, customer_id, payload, result FROM audit_log "
        "WHERE actor = 'system' AND action = 'retention_purge' ORDER BY id",
    )
    assert [(c, k, p["retention_days"]) for c, k, p, _ in runs] == [(None, None, 90)] * 2
    assert runs[-1][3] == result
    assert result["audit_rows"] > 0 and result["info_requests"] == 1
    oldest, newest = (datetime.fromisoformat(result[k]) for k in ("oldest", "newest"))
    assert before[0][6] <= oldest <= newest <= before[-1][6]
    # The decisions are intact: actor, action, rule and outcome are not text.
    assert _query(
        schema,
        "SELECT result->>'rule' FROM audit_log WHERE case_id = :c AND action = 'decide'",
        c=case_id,
    ) == [("escalate.amount_above_human_review",)]

    # A second run finds nothing left.
    again = _purge(database_url, utcnow() + timedelta(days=91))
    assert (again["audit_rows"], again["info_requests"]) == (0, 0)


def test_a_purged_case_still_opens_with_its_dossier_and_history(
    client: TestClient, database_url: str
) -> None:
    case_id = _conversation(client)
    _purge(database_url, utcnow() + timedelta(days=91))
    analyst = analyst_headers(client)
    dossier = client.get(f"/cases/{case_id}/dossier", headers=analyst)
    assert dossier.status_code == 200, dossier.text
    assert dossier.json()["original_message"] == PURGED_TEXT
    history = client.get(f"/cases/{case_id}/history", headers=analyst)
    assert history.status_code == 200
    assert not any(word in history.text for word in WORDS)
    mine = client.get("/me/clarifications", headers=customer_headers(client, "C1")).json()
    assert [c["info_request"]["question"] for c in mine if c["case_id"] == case_id] == [PURGED_TEXT]


def test_any_other_change_to_the_audit_log_still_fails(
    client: TestClient, schema: SchemaUrls
) -> None:
    _conversation(client)
    engine = create_engine(schema.admin)
    try:
        for sql in (
            "UPDATE audit_log SET action = 'forged'",
            "DELETE FROM audit_log",
            # The purge flag alone does not allow more than the purge itself.
            "SELECT set_config('app.retention_purge', 'on', true); "
            "UPDATE audit_log SET payload = retention_strip(payload), action = 'forged'",
            "SELECT set_config('app.retention_purge', 'on', true); "
            'UPDATE audit_log SET payload = \'{"redacted_text": "otro"}\'',
        ):
            with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
                conn.execute(text(sql))
    finally:
        engine.dispose()


def test_only_the_analyst_role_may_purge(client: TestClient, database_url: str) -> None:
    db = Database(database_url)
    try:
        for context in ({"customer_id": "C1"}, {}):
            with pytest.raises(DBAPIError), db.session(**context) as s:
                s.execute(text("SELECT * FROM purge_conversation_text(now()::timestamp)"))
        with pytest.raises(DBAPIError, match="permission denied"), db.session(role="analyst") as s:
            s.execute(text("UPDATE audit_log SET action = 'forged'"))
    finally:
        db.dispose()


def test_the_command_runs_the_purge_as_of_the_time_given(
    client: TestClient, schema: SchemaUrls, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _conversation(client)
    monkeypatch.setenv("DATABASE_URL", database_url)
    assert jobs.main(["purge"]) == 0
    assert all(word in _text_of_every_row(schema) for word in WORDS)
    later = (utcnow() + timedelta(days=91)).isoformat(timespec="minutes")
    assert jobs.main(["purge", "--now", later]) == 0
    assert not any(word in _text_of_every_row(schema) for word in WORDS)


def test_the_marker_is_shown_in_the_language_asked_for(
    client: TestClient, database_url: str
) -> None:
    case_id = _conversation(client)
    _purge(database_url, utcnow() + timedelta(days=91))
    who = customer_headers(client, "C1")
    trace = client.get(f"/me/clarifications/{case_id}/trace", params={"lang": "pt"}, headers=who)
    assert trace.status_code == 200, trace.text
    assert "«[removido por retenção]»" in trace.text and PURGED_TEXT not in trace.text
    dossier = client.get(
        f"/cases/{case_id}/dossier", params={"lang": "pt"}, headers=analyst_headers(client)
    ).json()
    assert dossier["original_message"] == "[removido por retenção]"
    [exchange] = dossier["info_exchanges"]
    assert (exchange["question"], exchange["answer"]) == (
        "[removido por retenção]",
        "[removido por retenção]",
    )


def test_the_command_refuses_to_run_as_a_superuser(
    client: TestClient, schema: SchemaUrls, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The owner of the test database is a superuser, as POSTGRES_USER is in compose and CI.
    _conversation(client)
    later = (utcnow() + timedelta(days=91)).isoformat(timespec="minutes")
    monkeypatch.setenv("DATABASE_URL", schema.admin)
    with pytest.raises(SystemExit, match="connect as trazo_app"):
        jobs.main(["purge", "--now", later])
    assert all(word in _text_of_every_row(schema) for word in WORDS)
    assert _query(schema, "SELECT count(*) FROM audit_log WHERE action = 'retention_purge'") == [
        (0,)
    ]


def test_the_command_refuses_to_run_as_a_role_that_creates_roles(
    client: TestClient,
    schema: SchemaUrls,
    role_that_creates_roles: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _conversation(client)
    later = (utcnow() + timedelta(days=91)).isoformat(timespec="minutes")
    monkeypatch.setenv("DATABASE_URL", role_that_creates_roles)
    with pytest.raises(SystemExit, match="CREATEROLE"):
        jobs.main(["all", "--now", later])
    assert all(word in _text_of_every_row(schema) for word in WORDS)

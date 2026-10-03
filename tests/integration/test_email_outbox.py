"""Transactional email with the outbox pattern (TRZ-33), on Postgres and with a simulated
provider that can be down.

CA1: with the flag off nothing is written or sent. CA2: the email of a notification is written
to the outbox in the transaction of the notification and a process sends it with retries. CA3:
it goes only to the test inbox of the allowlist; an address outside it fails without a call.
CA4: with the provider down the email stays pending and is retried later.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.adapters.db.session import Database, SchemaUrls
from app.adapters.email import EmailError
from app.cli import jobs
from app.core.config import Settings
from app.core.time import utcnow
from app.domain.email import EmailConfig
from app.main import create_app
from app.services.email_outbox import send_due
from tests.agent_support import still_not_recognized
from tests.auth_support import analyst_headers, customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

# Simulated now of the charges; the outbox runs on the real clock, from T0 on.
NOW = datetime(2026, 6, 17, 23, 59)
INBOX = "qa@example.test"
SEARS = "No reconozco un cargo de 1500 dólares en Sears"


class FakeProvider:
    """Records every send; while `down` it fails like a provider that does not answer."""

    def __init__(self) -> None:
        self.down = False
        self.sent: list[tuple[str, str, str, str]] = []

    def send(self, to: str, subject: str, body: str, idempotency_key: str) -> None:
        if self.down:
            raise EmailError("ConnectError")
        self.sent.append((to, subject, body, idempotency_key))


def _settings(database_url: str, **email: Any) -> Settings:
    return Settings(database_url=database_url, llm_enabled=False, log_level="WARNING", **email)


ON = {"email_enabled": True, "email_test_inbox": INBOX, "email_allowlist": INBOX}


def _client(schema: SchemaUrls, settings: Settings) -> TestClient:
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
    return TestClient(create_app(settings), raise_server_exceptions=False)


@pytest.fixture
def on(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    with _client(schema, _settings(database_url, **ON)) as c:
        yield c


def _query(schema: SchemaUrls, sql: str, **params: Any) -> list[tuple]:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        rows = [tuple(r) for r in conn.execute(text(sql), params)]
    engine.dispose()
    return rows


def _rejected(client: TestClient) -> str:
    """A 1500 USD case escalated to a person, which the analyst rejects: one notification."""
    who = customer_headers(client, "C1")
    first = client.post("/chat", json={"message": SEARS}, headers=who).json()
    case_id = still_not_recognized(client, first["case_id"], who)["case_id"]
    r = client.post(
        f"/cases/{case_id}/decision",
        json={"decision": "reject", "reason": "insufficient_data"},
        headers=analyst_headers(client),
    )
    assert r.status_code == 200, r.text
    return str(case_id)


def _send(database_url: str, provider: FakeProvider, now: datetime, **config: Any) -> dict:
    db = Database(database_url)
    try:
        with db.session(role="analyst") as session:
            email = EmailConfig(INBOX, frozenset({INBOX}), **config)
            return send_due(session, provider, email, now)
    finally:
        db.dispose()


def test_with_the_flag_off_nothing_is_written_nor_sent(
    schema: SchemaUrls, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CA1: the default settings.
    with _client(schema, _settings(database_url)) as client:
        _rejected(client)
    assert _query(schema, "SELECT count(*) FROM notifications") == [(1,)]
    assert _query(schema, "SELECT count(*) FROM email_outbox") == [(0,)]
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("EMAIL_ENABLED", "false")
    assert jobs.main(["send-email"]) == 0


def test_the_email_of_a_notification_is_written_with_it_to_the_test_inbox(
    on: TestClient, schema: SchemaUrls
) -> None:
    # CA2, CA3.
    case_id = _rejected(on)
    [(to, subject, body, status, attempts, note)] = _query(
        schema,
        "SELECT o.to_address, o.subject, o.body, o.status, o.attempts, n.text "
        "FROM email_outbox o JOIN notifications n ON n.id = o.notification_id",
    )
    assert (to, status, attempts) == (INBOX, "pending", 0)
    assert subject == "TRAZO [simulado]: novedades en tu aclaración"
    assert body.startswith(note) and f"Caso {case_id}." in body and "[simulado]" in body


def test_a_customer_session_cannot_read_the_outbox(
    on: TestClient, schema: SchemaUrls, database_url: str
) -> None:
    _rejected(on)
    db = Database(database_url)
    try:
        with db.session(customer_id="C1") as s:
            assert s.execute(text("SELECT count(*) FROM email_outbox")).scalar_one() == 0
    finally:
        db.dispose()


def test_a_provider_down_leaves_the_email_pending_and_it_is_sent_on_a_later_run(
    on: TestClient, schema: SchemaUrls, database_url: str
) -> None:
    # CA4.
    case_id = _rejected(on)
    t0 = utcnow() + timedelta(seconds=1)
    provider = FakeProvider()
    provider.down = True
    assert _send(database_url, provider, t0) == {"sent": 0, "retry": 1, "failed": 0}
    [(status, attempts, due, error)] = _query(
        schema, "SELECT status, attempts, next_attempt_at, last_error FROM email_outbox"
    )
    assert (status, attempts, due, error) == (
        "pending",
        1,
        t0 + timedelta(minutes=1),
        "ConnectError",
    )

    # Before its wait is over the email is not tried again.
    provider.down = False
    assert _send(database_url, provider, t0 + timedelta(seconds=30)) == {
        "sent": 0,
        "retry": 0,
        "failed": 0,
    }
    assert _send(database_url, provider, t0 + timedelta(minutes=1)) == {
        "sent": 1,
        "retry": 0,
        "failed": 0,
    }
    [(outbox_id, status, attempts, sent_at)] = _query(
        schema, "SELECT id, status, attempts, sent_at FROM email_outbox"
    )
    assert (status, attempts, sent_at) == ("sent", 2, t0 + timedelta(minutes=1))
    [(to, _subject, _body, key)] = provider.sent
    assert (to, key) == (INBOX, f"email-outbox-{outbox_id}")

    # A sent email is not sent again.
    assert _send(database_url, provider, t0 + timedelta(hours=1))["sent"] == 0
    # Every attempt is audited, without the address or the body.
    rows = _query(
        schema,
        "SELECT result, payload::text || result::text FROM audit_log "
        "WHERE action = 'email_attempt' AND case_id = :c ORDER BY id",
        c=case_id,
    )
    assert [r["status"] for r, _ in rows] == ["pending", "sent"]
    assert not any(INBOX in raw for _, raw in rows)


def test_after_the_last_attempt_the_email_fails_for_good(
    on: TestClient, schema: SchemaUrls, database_url: str
) -> None:
    _rejected(on)
    provider = FakeProvider()
    provider.down = True
    at = utcnow() + timedelta(seconds=1)
    for wait in (1, 2, 4, 8):
        assert _send(database_url, provider, at)["retry"] == 1
        at += timedelta(minutes=wait)
    assert _send(database_url, provider, at) == {"sent": 0, "retry": 0, "failed": 1}
    assert _query(schema, "SELECT status, attempts FROM email_outbox") == [("failed", 5)]
    provider.down = False
    assert _send(database_url, provider, at + timedelta(days=1))["sent"] == 0


def test_an_address_outside_the_allowlist_fails_without_calling_the_provider(
    schema: SchemaUrls, database_url: str
) -> None:
    # CA3: the inbox of the settings is not in the allowlist.
    settings = _settings(database_url, **{**ON, "email_allowlist": "other@example.test"})
    with _client(schema, settings) as client:
        _rejected(client)
    provider = FakeProvider()
    db = Database(database_url)
    try:
        with db.session(role="analyst") as session:
            config = EmailConfig(INBOX, frozenset({"other@example.test"}))
            assert send_due(session, provider, config, utcnow())["failed"] == 1
    finally:
        db.dispose()
    assert provider.sent == []
    assert _query(schema, "SELECT status, last_error FROM email_outbox") == [
        ("failed", "not_allowed")
    ]


def test_the_command_refuses_to_send_without_the_resend_key(
    on: TestClient, schema: SchemaUrls, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rejected(on)
    monkeypatch.setenv("DATABASE_URL", database_url)
    for k, v in {"EMAIL_ENABLED": "true", "EMAIL_TEST_INBOX": INBOX, "RESEND_API_KEY": ""}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit, match="RESEND_API_KEY"):
        jobs.main(["send-email"])
    assert _query(schema, "SELECT status, attempts FROM email_outbox") == [("pending", 0)]

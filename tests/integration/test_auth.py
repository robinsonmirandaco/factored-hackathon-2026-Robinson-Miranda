"""Authentication and sessions against Postgres (TRZ-09): one-time codes without enumeration,
the lockout, inactivity on the real clock, the customer of the session only, analyst roles and
no data behind a document alone."""

import dataclasses
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from app.adapters.db.models import AuditRecord, Case
from app.adapters.db.session import Database, SchemaUrls
from app.core.config import Settings
from app.main import create_app
from app.services import auth as auth_module
from tests.agent_support import fake_llm, llm_settings, reading, still_not_recognized
from tests.auth_support import (
    DEMO_CODE,
    TEST_ANALYST_PASSWORD,
    TEST_JWT_SECRET,
    analyst_headers,
    customer_headers,
    customer_token,
    document,
)
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NETFLIX = "No reconozco un cargo de 120 dólares en Netflix"
START = datetime(2026, 9, 27, 12, 0)


class Clock:
    """The real clock of the app, moved by hand."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


def _app(database_url: str, clock: Clock, **overrides: Any) -> FastAPI:
    settings = Settings(
        database_url=database_url, llm_enabled=False, log_level="WARNING", **overrides
    )
    app = create_app(settings)
    app.state.runtime = dataclasses.replace(app.state.runtime, now=clock)
    return app


@pytest.fixture
def seeded(schema: SchemaUrls) -> SchemaUrls:
    load(
        schema.admin,
        [customer("C1"), customer("C2")],
        [card("P1", "C1"), card("P2", "C2")],
        [
            transaction(
                "TX1",
                "C1",
                "P1",
                datetime(2026, 6, 17, 3, 59),
                amount=120.0,
                currency="USD",
                merchant_name="Netflix",
            ),
            transaction(
                "TX2",
                "C2",
                "P2",
                datetime(2026, 6, 17, 3, 59),
                amount=120.0,
                currency="USD",
                merchant_name="Netflix",
            ),
        ],
    )
    return schema


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def app(seeded: SchemaUrls, database_url: str, clock: Clock) -> FastAPI:
    return _app(database_url, clock)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _error(r: Any, status: int, code: str) -> None:
    assert r.status_code == status, r.text
    assert set(r.json()) == {"error_code", "message", "trace_id"}
    assert r.json()["error_code"] == code


def _verify(client: TestClient, customer_id: str, code: str) -> Any:
    return client.post("/auth/otp/verify", json={**document(customer_id), "code": code})


def _query(schema: SchemaUrls, sql: str) -> list[Any]:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        rows = list(conn.execute(text(sql)).all())
    engine.dispose()
    return rows


def _case(schema: SchemaUrls, case_id: str) -> Case:
    owner = Database(schema.admin)
    with owner.session() as s:
        case = s.get(Case, case_id)
        assert case is not None
    owner.dispose()
    return case


def _executed(schema: SchemaUrls) -> tuple[int, int]:
    return tuple(  # type: ignore[return-value]
        _query(schema, q)[0][0]
        for q in ("SELECT count(*) FROM disputes", "SELECT count(*) FROM card_blocks")
    )


# ---- CA1: the same answer for every document ----------------------------------------------


def test_code_request_answers_the_same_whether_the_document_exists(client: TestClient) -> None:
    known = client.post("/auth/otp/request", json=document("C1"))
    unknown = client.post("/auth/otp/request", json=document("NOBODY"))

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json() == {"status": "code_sent", "expires_in_seconds": 300}


def test_a_right_code_for_an_unknown_document_fails_like_a_wrong_code(
    client: TestClient,
) -> None:
    client.post("/auth/otp/request", json=document("NOBODY"))
    unknown = _verify(client, "NOBODY", DEMO_CODE)
    client.post("/auth/otp/request", json=document("C1"))
    wrong = _verify(client, "C1", "000000")

    _error(unknown, 401, "invalid_code")
    _error(wrong, 401, "invalid_code")
    assert unknown.json()["message"] == wrong.json()["message"]


def test_code_request_rejects_an_invalid_body(client: TestClient) -> None:
    _error(client.post("/auth/otp/request", json={"document_type": "CC"}), 422, "validation_error")


def test_code_request_reports_a_database_failure_as_503(seeded: SchemaUrls, clock: Clock) -> None:
    # A port nothing listens on: the database is down. Without `with`, the client skips the
    # startup check, which would need the database too.
    app = _app("postgresql+psycopg://trazo_app:x@localhost:1/none", clock)
    c = TestClient(app, raise_server_exceptions=False)
    _error(c.post("/auth/otp/request", json=document("C1")), 503, "db_unavailable")
    _error(_verify(c, "C1", DEMO_CODE), 503, "db_unavailable")
    login = {"username": "analista.demo", "password": TEST_ANALYST_PASSWORD}
    _error(c.post("/auth/analyst/login", json=login), 503, "db_unavailable")
    token = jwt.encode(
        {"sub": "C1", "role": "customer", "jti": "j", "iat": 0, "exp": 0}, TEST_JWT_SECRET
    )
    _error(c.post("/chat", json={"message": "hola"}, headers=_bearer(token)), 503, "db_unavailable")


@pytest.mark.parametrize("customer_id", ["C1", "NOBODY"])
def test_code_requests_are_limited_per_document_the_same_way_for_every_document(
    client: TestClient, clock: Clock, customer_id: str
) -> None:
    for _ in range(5):
        assert client.post("/auth/otp/request", json=document(customer_id)).status_code == 202
    limited = client.post("/auth/otp/request", json=document(customer_id))
    _error(limited, 429, "code_requests_limited")
    assert limited.json()["message"] == "Too many code requests. Try again in a few minutes."
    # Another document keeps its own count.
    assert client.post("/auth/otp/request", json=document("C2")).status_code == 202
    clock.advance(15)
    assert client.post("/auth/otp/request", json=document(customer_id)).status_code == 202


def test_a_refused_code_request_issues_no_code(
    seeded: SchemaUrls, database_url: str, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(auth_module, "log", recorder)
    app = _app(database_url, clock, demo_mode=False, app_env="local")
    with TestClient(app, raise_server_exceptions=False) as c:
        for _ in range(6):
            c.post("/auth/otp/request", json=document("C1"))
    issued = [kw["code"] for event, kw in recorder.events if event == "otp_code_issued"]
    assert len(issued) == 5


# ---- CA2: the token -------------------------------------------------------------------------


def test_a_right_code_returns_a_signed_customer_token_with_expiry(client: TestClient) -> None:
    client.post("/auth/otp/request", json=document("C1"))
    r = _verify(client, "C1", DEMO_CODE)

    assert r.status_code == 200
    body = r.json()
    assert (body["token_type"], body["role"]) == ("bearer", "customer")
    assert (body["expires_in_seconds"], body["idle_timeout_seconds"]) == (7200, 900)
    claims = jwt.decode(
        body["access_token"], TEST_JWT_SECRET, algorithms=["HS256"], options={"verify_exp": False}
    )
    assert (claims["sub"], claims["role"]) == ("C1", "customer")
    assert claims["exp"] - claims["iat"] == 7200
    assert claims["jti"]


def test_a_code_works_once(client: TestClient) -> None:
    client.post("/auth/otp/request", json=document("C1"))
    assert _verify(client, "C1", DEMO_CODE).status_code == 200
    _error(_verify(client, "C1", DEMO_CODE), 401, "invalid_code")


def test_a_code_expires_after_five_minutes(client: TestClient, clock: Clock) -> None:
    client.post("/auth/otp/request", json=document("C1"))
    clock.advance(5)
    _error(_verify(client, "C1", DEMO_CODE), 401, "invalid_code")


def test_code_verification_rejects_a_malformed_code(client: TestClient) -> None:
    _error(_verify(client, "C1", "12ab"), 422, "validation_error")


def test_a_token_signed_with_another_key_or_tampered_is_refused(client: TestClient) -> None:
    token = customer_token(client, "C1")
    claims = jwt.decode(token, TEST_JWT_SECRET, algorithms=["HS256"], options={"verify_exp": False})
    forged = jwt.encode({**claims, "sub": "C2"}, "another-secret-of-32-characters-at-least")

    _error(
        client.post("/chat", json={"message": "hola"}, headers=_bearer(forged)),
        401,
        "invalid_token",
    )
    _error(
        client.post("/chat", json={"message": "hola"}, headers=_bearer(token[:-2])),
        401,
        "invalid_token",
    )


def test_the_api_refuses_to_start_without_a_long_secret(database_url: str) -> None:
    with pytest.raises(ValueError, match="JWT_SECRET"):
        create_app(Settings(database_url=database_url, jwt_secret="short"))


def test_the_api_refuses_to_start_without_the_document_key(database_url: str) -> None:
    with pytest.raises(ValueError, match="DOCUMENT_HASH_KEY"):
        create_app(Settings(database_url=database_url, document_hash_key=""))


# ---- CA3: the fixed code only in demo mode --------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))

    warning = info


def test_without_demo_mode_the_code_is_random_and_only_in_the_local_log(
    seeded: SchemaUrls, database_url: str, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(auth_module, "log", recorder)
    app = _app(database_url, clock, demo_mode=False, app_env="local")
    with TestClient(app, raise_server_exceptions=False) as c:
        c.post("/auth/otp/request", json=document("C1"))
        issued = [kw["code"] for event, kw in recorder.events if event == "otp_code_issued"]
        assert len(issued) == 1 and len(issued[0]) == 6 and issued[0].isdigit()
        # One chance in a million that the random code is the demo code.
        if issued[0] != DEMO_CODE:
            _error(_verify(c, "C1", DEMO_CODE), 401, "invalid_code")
        assert _verify(c, "C1", issued[0]).status_code == 200


def test_outside_local_the_random_code_is_not_logged(
    seeded: SchemaUrls, database_url: str, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(auth_module, "log", recorder)
    app = _app(database_url, clock, demo_mode=False, app_env="prod")
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.post("/auth/otp/request", json=document("C1")).status_code == 202
    assert recorder.events == []


# ---- CA4: three wrong codes lock the document ----------------------------------------------


@pytest.mark.parametrize("customer_id", ["C1", "NOBODY"])
def test_three_wrong_codes_lock_the_document_for_15_minutes(
    client: TestClient, clock: Clock, seeded: SchemaUrls, customer_id: str
) -> None:
    client.post("/auth/otp/request", json=document(customer_id))
    _error(_verify(client, customer_id, "000001"), 401, "invalid_code")
    _error(_verify(client, customer_id, "000002"), 401, "invalid_code")
    _error(_verify(client, customer_id, "000003"), 429, "document_locked")
    # Locked: even the right code fails, and asking for a new one issues nothing.
    client.post("/auth/otp/request", json=document(customer_id))
    _error(_verify(client, customer_id, DEMO_CODE), 429, "document_locked")

    rows = _query(
        seeded,
        "SELECT customer_id, actor, payload, result FROM audit_log "
        "WHERE action = 'document_locked'",
    )
    assert len(rows) == 1
    owner, actor, payload, result = rows[0]
    assert actor == "auth"
    assert owner == (customer_id if customer_id == "C1" else None)
    assert "DOC" not in str(payload) and len(payload["document_ref"]) == 12
    assert result["failed_attempts"] == 3

    clock.advance(15)
    client.post("/auth/otp/request", json=document(customer_id))
    after = _verify(client, customer_id, DEMO_CODE)
    if customer_id == "C1":
        assert after.status_code == 200
    else:
        _error(after, 401, "invalid_code")


def test_asking_for_a_new_code_does_not_reset_the_attempts(client: TestClient) -> None:
    client.post("/auth/otp/request", json=document("C1"))
    _verify(client, "C1", "000001")
    _verify(client, "C1", "000002")
    client.post("/auth/otp/request", json=document("C1"))
    _error(_verify(client, "C1", "000003"), 429, "document_locked")


# ---- CA5: inactivity on the real clock; the case is kept, expired ------------------------


def test_a_session_expires_after_15_idle_minutes_and_its_case_is_expired(
    client: TestClient, clock: Clock, seeded: SchemaUrls
) -> None:
    headers = customer_headers(client, "C1")
    first = client.post("/chat", json={"message": NETFLIX}, headers=headers).json()
    assert first["outcome"] == "recognizing"
    pending = still_not_recognized(client, first["case_id"], headers)
    assert pending["outcome"] == "awaiting_confirmation"

    clock.advance(15.5)
    action_id = pending["pending_action"]["action_id"]
    late = client.post(
        "/chat",
        json={"message": "sí", "case_id": first["case_id"], "confirm_action_id": action_id},
        headers=headers,
    )
    _error(late, 401, "session_expired")
    case = _case(seeded, first["case_id"])
    assert (case.status, case.recommended_action) == ("expired", None)

    # Logged in again, a "sí" to the action of the kept case runs nothing: it was cancelled.
    again = client.post(
        "/chat",
        json={"message": "sí", "case_id": first["case_id"], "confirm_action_id": action_id},
        headers=customer_headers(client, "C1"),
    ).json()
    assert (again["outcome"], again["actions_taken"]) == ("no_pending_action", [])
    assert _executed(seeded) == (0, 0)
    expired = _query(
        seeded,
        f"SELECT result FROM audit_log WHERE action = 'case_expired' "
        f"AND case_id = '{first['case_id']}'",
    )
    assert expired[0][0]["status_before"] == "awaiting_confirmation"
    assert expired[0][0]["pending_action_dropped"] in ("register", "register_and_offer_block")
    assert expired[0][0]["action_id"] == action_id
    assert _query(seeded, f"SELECT status FROM case_actions WHERE id = '{action_id}'") == [
        ("canceled",)
    ]


def test_a_new_login_expires_the_case_of_a_session_left_idle(
    client: TestClient, clock: Clock, seeded: SchemaUrls
) -> None:
    first = client.post(
        "/chat", json={"message": NETFLIX}, headers=customer_headers(client, "C1")
    ).json()
    # Left at the recognition step, which also waits on the customer.
    assert first["outcome"] == "recognizing"
    clock.advance(20)
    # The old token is never used again; the new login alone ends that session.
    again = client.post(
        "/chat",
        json={
            "message": "sí",
            "case_id": first["case_id"],
            "confirm_action_id": "ACT-0000000000",
        },
        headers=customer_headers(client, "C1"),
    ).json()
    assert (again["outcome"], again["actions_taken"]) == ("no_pending_action", [])
    assert _case(seeded, first["case_id"]).status == "expired"
    assert _executed(seeded) == (0, 0)


def test_activity_keeps_the_session_alive_until_its_absolute_expiry(
    seeded: SchemaUrls, database_url: str, clock: Clock
) -> None:
    app = _app(database_url, clock, session_max_minutes=30)
    with TestClient(app, raise_server_exceptions=False) as c:
        headers = customer_headers(c, "C1")
        for _ in range(2):
            clock.advance(14)
            assert c.post("/chat", json={"message": "hola"}, headers=headers).status_code == 200
        clock.advance(3)
        _error(c.post("/chat", json={"message": "hola"}, headers=headers), 401, "session_expired")


def test_session_expiry_uses_the_real_clock_not_the_simulated_one(
    seeded: SchemaUrls, database_url: str, clock: Clock
) -> None:
    # The simulated clock is months behind the real one; a fresh session still works.
    app = _app(database_url, clock, trazo_now=datetime(2026, 6, 17, 23, 59))
    with TestClient(app, raise_server_exceptions=False) as c:
        headers = customer_headers(c, "C1")
        assert c.post("/chat", json={"message": "hola"}, headers=headers).status_code == 200


def test_logout_revokes_the_token(client: TestClient, seeded: SchemaUrls) -> None:
    headers = customer_headers(client, "C1")
    first = client.post("/chat", json={"message": NETFLIX}, headers=headers).json()

    assert client.post("/auth/logout", headers=headers).status_code == 204
    _error(client.post("/chat", json={"message": "hola"}, headers=headers), 401, "session_revoked")
    assert _case(seeded, first["case_id"]).status == "expired"
    _error(client.post("/auth/logout"), 401, "not_authenticated")


# ---- CA6: the customer comes from the session only -----------------------------------------


def test_a_foreign_customer_id_is_stopped_before_any_llm_call(
    seeded: SchemaUrls, database_url: str, clock: Clock
) -> None:
    sent: list[str] = []
    settings = llm_settings(database_url, log_level="WARNING")
    app = create_app(settings)
    runtime = app.state.runtime
    llm = fake_llm(settings, reading("unrecognized_charge"), sent)
    app.state.runtime = dataclasses.replace(
        runtime, now=clock, agent=dataclasses.replace(runtime.agent, llm=llm)
    )
    with TestClient(app, raise_server_exceptions=False) as c:
        headers = customer_headers(c, "C1")
        first = c.post("/chat", json={"message": NETFLIX}, headers=headers).json()
        assert sent, "the fake LLM is wired: a normal turn reaches it"
        sent.clear()
        stopped = c.post(
            "/chat",
            json={"customer_id": "C2", "message": NETFLIX, "case_id": first["case_id"]},
            headers=headers,
        ).json()
    assert (stopped["outcome"], stopped["tokens"], stopped["llm_fallback"]) == (
        "security_blocked",
        0,
        True,
    )
    assert sent == []
    # A continued case keeps the intent it had.
    assert stopped["intent"] == first["intent"]


def test_a_customer_id_in_the_body_never_selects_another_customer(
    client: TestClient, seeded: SchemaUrls
) -> None:
    r = client.post(
        "/chat",
        json={"customer_id": "C2", "message": NETFLIX},
        headers=customer_headers(client, "C1"),
    )
    assert r.status_code == 200
    body = r.json()
    assert (body["outcome"], body["actions_taken"]) == ("security_blocked", [])
    # The case number and trace id are random hex and may contain "C2" by chance: they are
    # taken out before looking for the other customer's ids.
    shown = r.text.replace(body["case_id"], "").replace(body["trace_id"], "")
    assert "TX2" not in shown and "C2" not in shown

    case = _case(seeded, body["case_id"])
    assert (case.customer_id, case.status, case.transaction_id) == ("C1", "security_blocked", None)
    owner = Database(seeded.admin)
    with owner.session() as s:
        rows = s.scalars(select(AuditRecord).where(AuditRecord.case_id == body["case_id"])).all()
        assert {r.customer_id for r in rows} == {"C1"}
        assert ("agent", "security_event") in {(r.actor, r.action) for r in rows}
        decide = next(r for r in rows if r.action == "decide")
        assert decide.result["rule"] == "security.security_event"
        # No step read the other customer's charge.
        assert all("TX2" not in str(r.payload) + str(r.result) for r in rows)
    owner.dispose()
    assert _query(seeded, "SELECT count(*) FROM cases WHERE customer_id = 'C2'")[0][0] == 0


def test_the_customer_id_of_the_session_itself_in_the_body_is_just_ignored(
    client: TestClient,
) -> None:
    r = client.post(
        "/chat",
        json={"customer_id": "C1", "message": NETFLIX},
        headers=customer_headers(client, "C1"),
    )
    assert r.json()["outcome"] == "recognizing"


def test_another_customers_case_id_is_not_found(client: TestClient) -> None:
    first = client.post(
        "/chat", json={"message": NETFLIX}, headers=customer_headers(client, "C1")
    ).json()
    r = client.post(
        "/chat",
        json={
            "message": "sí",
            "case_id": first["case_id"],
            "confirm_action_id": "ACT-0000000000",
        },
        headers=customer_headers(client, "C2"),
    )
    _error(r, 404, "case_not_found")


# ---- CA7 and CA8: analysts, and roles kept apart ------------------------------------------


def test_the_analyst_logs_in_with_the_test_credentials(client: TestClient) -> None:
    r = client.post(
        "/auth/analyst/login",
        json={"username": "analista.demo", "password": TEST_ANALYST_PASSWORD},
    )
    assert r.status_code == 200
    claims = jwt.decode(
        r.json()["access_token"],
        TEST_JWT_SECRET,
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    assert (claims["sub"], claims["role"]) == ("analista.demo", "analyst")


def test_wrong_analyst_credentials_are_refused(client: TestClient) -> None:
    for body in (
        {"username": "analista.demo", "password": "wrong"},
        {"username": "someone", "password": TEST_ANALYST_PASSWORD},
    ):
        _error(client.post("/auth/analyst/login", json=body), 401, "invalid_credentials")
    _error(client.post("/auth/analyst/login", json={"username": "x"}), 422, "validation_error")


def test_without_an_analyst_password_no_analyst_gets_in(
    seeded: SchemaUrls, database_url: str, clock: Clock
) -> None:
    app = _app(database_url, clock, analyst_demo_password="")
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/auth/analyst/login", json={"username": "analista.demo", "password": "x"})
    _error(r, 401, "invalid_credentials")


ANALYST_ROUTES = [
    ("GET", "/cases/CASE-1"),
    ("GET", "/cases/CASE-1/trace"),
    ("GET", "/cases/CASE-1/history"),
    ("GET", "/cases/CASE-1/dossier"),
    ("GET", "/queue"),
    ("POST", "/cases/CASE-1/decision"),
    ("GET", "/metrics"),
]


def test_a_customer_token_gets_403_on_every_analyst_route(client: TestClient) -> None:
    headers = customer_headers(client, "C1")
    for method, path in ANALYST_ROUTES:
        r = client.request(method, path, headers=headers, json={"decision": "approve"})
        _error(r, 403, "forbidden")


def test_an_analyst_token_gets_403_on_chat(client: TestClient) -> None:
    r = client.post("/chat", json={"message": "hola"}, headers=analyst_headers(client))
    _error(r, 403, "forbidden")


def test_an_analyst_token_opens_the_analyst_routes(client: TestClient) -> None:
    assert client.get("/queue", headers=analyst_headers(client)).status_code == 200


# ---- CA9: a document alone reaches no data --------------------------------------------------

PUBLIC = {"/health", "/auth/otp/request", "/auth/otp/verify", "/auth/analyst/login"}


def test_every_route_but_login_and_health_requires_a_session(
    app: FastAPI, client: TestClient
) -> None:
    routes = [
        (method.upper(), path.replace("{case_id}", "CASE-1"))
        for path, operations in app.openapi()["paths"].items()
        if path not in PUBLIC
        for method in operations
    ]
    assert len(routes) == 9
    for method, path in routes:
        r = client.request(method, path, json={"message": "hola", "decision": "approve"})
        _error(r, 401, "not_authenticated")


# ---- helpers ----------------------------------------------------------------------------


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}

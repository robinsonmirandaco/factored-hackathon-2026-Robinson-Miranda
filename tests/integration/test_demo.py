"""Demo seed and reset (TRZ-38) on Postgres, through `make seed-demo` and the API.

CA1: the seed chooses the demo people by rule, never a customer of an evaluation case, gives
them invented documents stored only as a hash, and creates the starting cases and the reviews of
the PT-BR cell through the agent and the analyst decisions. CA2: the reset exists only in demo
mode. CA3: everything seeded is marked simulated in the database and the API. CA4: the golden
evaluation gives the same results with and without the demo seeded.

The demo configuration here is a small one of the same shape as config/demo.yaml, over made-up
customers; the repository's own is checked against the cohort by `make seed-demo` itself.
"""

import hashlib
import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError

from app.adapters.db.session import SchemaUrls
from app.cli import seed_demo
from app.core.config import Settings
from app.domain.pii import document_hash
from app.main import create_app
from app.services import demo
from tests.auth_support import DEMO_CODE, analyst_headers
from tests.serving_data import KEY, card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 17, 23, 59)
MX = {"country": "México", "country_code": "MX", "timezone": "America/Mexico_City"}
MX_DOC = {"document_type": "DNI", "document_number": "DEMO-MX-0001"}

CONFIG: dict[str, Any] = {
    "version": "test-demo",
    "order_seed": "test",
    "complaint_lookback_days": 90,
    "max_candidates": 10,
    "seed_analyst": "demo.seed",
    "personas": [
        {
            "role": "mx_main",
            "country": "MX",
            "document": {"type": "DNI", "number": "DEMO-MX-0001"},
            "walkthrough": "10.3 steps 1 to 3",
            "charges": {
                "two": {"type": "Purchase", "card": True, "usd": [0, 500], "same_merchant": 2}
            },
            "rehearsals": [
                {
                    "turns": [
                        {"say": "No reconozco un cargo de {two.merchant}"},
                        {"option": 0, "say": "Es este"},
                        {"recognition": "not_recognized", "say": "Sigo sin reconocerlo"},
                        {"confirm": True, "say": "Sí, confirmo"},
                    ],
                    "expect": "registered_verified",
                }
            ],
        }
    ],
    "seeded": [
        {
            "role": "queue_mx",
            "country": "MX",
            "charges": {"charge": {"type": "Payment", "usd": [500, 1000]}},
            "script": {
                "turns": [
                    {"button": "charge", "say": "No lo reconozco"},
                    {"recognition": "not_recognized", "say": "Sigo sin reconocerlo"},
                ],
                "expect": "pending_analyst_approval",
            },
        }
    ],
    "pt_cell": {
        "role": "pt_cell",
        "country": "CO",
        "charges": {"charge": {"type": "Payment", "usd": [500, 1000]}},
        "script": {
            "turns": [
                {"button": "charge", "say": "Não reconheço"},
                {"recognition": "not_recognized", "say": "Continuo sem reconhecer"},
            ],
            "expect": "pending_analyst_approval",
        },
        "decisions": ["approve", {"reject": "wrong_charge"}, "approve"],
    },
}


def _bank(schema: SchemaUrls) -> None:
    """Two customers fit the persona (M1, M2), one the queue (Q1) and three the cell (K1-K3)."""
    customers = [customer(c, **MX, document_type="DNI") for c in ("M1", "M2", "Q1")]
    customers += [customer(c) for c in ("K1", "K2", "K3")]
    products = [card(f"P{c}", c, currency="USD") for c in ("M1", "M2", "Q1", "K1", "K2", "K3")]
    rows = []
    for c in ("M1", "M2"):
        for i, (amount, hours) in enumerate(((120.0, 20), (130.0, 30))):
            rows.append(
                transaction(
                    f"{c}T{i}",
                    c,
                    f"P{c}",
                    NOW - timedelta(hours=hours),
                    amount=amount,
                    currency="USD",
                    merchant_name="Liverpool",
                )
            )
    for c in ("Q1", "K1", "K2", "K3"):
        rows.append(
            transaction(
                f"{c}PAY",
                c,
                f"P{c}",
                NOW - timedelta(hours=10),
                amount=700.0,
                currency="USD",
                transaction_type="Payment",
                channel="App",
                merchant_name=None,
            )
        )
    load(schema.admin, customers, products, rows)
    _exec(schema, "INSERT INTO seed_runs (source, detail) VALUES ('cohort', '{}')")


def _exec(schema: SchemaUrls, sql: str, **params: Any) -> list[tuple]:
    engine = create_engine(schema.admin)
    with engine.begin() as conn:
        result = conn.execute(text(sql), params)
        rows = [tuple(r) for r in result] if result.returns_rows else []
    engine.dispose()
    return rows


def _seed(
    schema: SchemaUrls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, excluded: set[str]
) -> None:
    """Runs `make seed-demo` with the test configuration and exclusion list."""
    (tmp_path / "eval").mkdir(exist_ok=True)
    data = "".join(f"{c}\n" for c in sorted(excluded)).encode()
    (tmp_path / "eval" / demo.CASE_CUSTOMERS).write_bytes(data)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"case_customers": {"sha256": hashlib.sha256(data).hexdigest()}})
    )
    config = tmp_path / "demo.yaml"
    config.write_text(yaml.safe_dump(CONFIG))
    monkeypatch.setattr(seed_demo, "MANIFEST", manifest)
    monkeypatch.setenv("ADMIN_DATABASE_URL", schema.admin)
    monkeypatch.setenv("DATABASE_URL", schema.app)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEMO_CONFIG_PATH", str(config))
    monkeypatch.setenv("LLM_ENABLED", "false")
    assert seed_demo.main() == 0


@pytest.fixture
def seeded(
    schema: SchemaUrls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    _bank(schema)
    _seed(schema, tmp_path, monkeypatch, excluded=set())
    settings = Settings(
        database_url=schema.app,
        llm_enabled=False,
        log_level="WARNING",
        demo_config_path=tmp_path / "demo.yaml",
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


def _roles(schema: SchemaUrls) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for role, customer_id in _exec(
        schema, "SELECT role, customer_id FROM demo_roles ORDER BY role, position"
    ):
        out.setdefault(role, []).append(customer_id)
    return out


def _state(schema: SchemaUrls) -> dict[str, Any]:
    """The demo state without its random ids: cases, open queue, reviews and counters."""
    return {
        "cases": _exec(
            schema,
            "SELECT customer_id, status, language, simulated, recommended_action, "
            "escalation_reason FROM cases ORDER BY 1, 2",
        ),
        "queue": _exec(
            schema,
            "SELECT c.customer_id, q.kind FROM case_queue q JOIN cases c ON c.id = q.case_id "
            "WHERE q.resolved_at IS NULL ORDER BY 1",
        ),
        "reviews": _exec(
            schema,
            "SELECT count(*), count(*) FILTER (WHERE (result->'review'->>'reversal')::boolean) "
            "FROM audit_log WHERE result ? 'review'",
        ),
        "products": _exec(schema, "SELECT product_id, product_status FROM products ORDER BY 1"),
        "draw": _exec(schema, "SELECT last_value, is_called FROM audit_draw_seq"),
        "folio": _exec(schema, "SELECT last_value, is_called FROM dispute_folio_seq"),
        "switch": _exec(schema, "SELECT all_to_human FROM automation_switch"),
        "cells": _exec(
            schema,
            "SELECT intent, language, level, block_reviews, block_reversals FROM autonomy_cells "
            "ORDER BY 1, 2",
        ),
    }


def _reset(client: TestClient, key: str = "k1") -> Any:
    return client.post("/demo/reset", headers={**analyst_headers(client), "idempotency-key": key})


# ---- CA1: who the demo people are --------------------------------------------------------


def test_the_seed_fills_every_role_by_rule_and_never_with_an_excluded_customer(
    schema: SchemaUrls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bank(schema)
    _seed(schema, tmp_path, monkeypatch, excluded={"M1"})
    roles = _roles(schema)
    assert roles["mx_main"] == ["M2"] and roles["queue_mx"] == ["Q1"]
    assert sorted(roles["pt_cell"]) == ["K1", "K2", "K3"]
    # The same data and rule with the other customer excluded picks the other one, and the
    # invented document moves with the role.
    _seed(schema, tmp_path, monkeypatch, excluded={"M2"})
    assert _roles(schema)["mx_main"] == ["M1"]
    digest = document_hash(KEY, "DNI", "DEMO-MX-0001")
    holders = "SELECT customer_id FROM customers WHERE document_hash = :h"
    assert _exec(schema, holders, h=digest) == [("M1",)]


def test_a_role_no_customer_fits_stops_the_seed(
    schema: SchemaUrls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bank(schema)
    with pytest.raises(SystemExit, match="mx_main"):
        _seed(schema, tmp_path, monkeypatch, excluded={"M1", "M2"})


def test_the_seed_refuses_a_list_that_is_not_the_manifest_one(
    schema: SchemaUrls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bank(schema)
    _seed(schema, tmp_path, monkeypatch, excluded=set())
    (tmp_path / "eval" / demo.CASE_CUSTOMERS).write_text("M9\n")
    with pytest.raises(SystemExit, match="does not match the manifest"):
        seed_demo.main()


def test_the_seed_refuses_a_database_without_the_cohort(
    schema: SchemaUrls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bank(schema)
    _exec(schema, "UPDATE seed_runs SET source = 'synthetic'")
    with pytest.raises(SystemExit, match="needs the cohort"):
        _seed(schema, tmp_path, monkeypatch, excluded=set())


def test_the_invented_document_logs_the_persona_in_and_only_its_hash_is_stored(
    seeded: TestClient, schema: SchemaUrls
) -> None:
    assert seeded.post("/auth/otp/request", json=MX_DOC).status_code == 202
    r = seeded.post("/auth/otp/verify", json={**MX_DOC, "code": DEMO_CODE})
    assert r.status_code == 200
    me = seeded.get("/me", headers={"authorization": f"Bearer {r.json()['access_token']}"})
    assert me.status_code == 200 and me.json()["country_code"] == "MX"
    subject = "SELECT subject FROM sessions WHERE role = 'customer' ORDER BY created_at DESC"
    assert _exec(schema, subject)[0] == (_roles(schema)["mx_main"][0],)
    digest = document_hash(KEY, "DNI", "DEMO-MX-0001")
    assert _exec(schema, "SELECT count(*) FROM customers WHERE document_hash = :h", h=digest) == [
        (1,)
    ]
    dump = _exec(schema, "SELECT string_agg(row_to_json(c)::text, '') FROM customers c")[0][0]
    assert "DEMO-MX-0001" not in dump


def test_the_starting_cases_go_through_the_agent_and_are_marked_simulated(
    seeded: TestClient, schema: SchemaUrls
) -> None:
    cases = _exec(schema, "SELECT id, simulated FROM cases")
    assert len(cases) == 4 and all(simulated for _, simulated in cases)
    # Each one has the agent's own trail and the row that marks it.
    for case_id, _ in cases:
        actions = {
            a
            for (a,) in _exec(schema, "SELECT action FROM audit_log WHERE case_id = :c", c=case_id)
        }
        assert {"turn_complete", "mark_simulated"} <= actions
    queue = seeded.get("/queue", headers=analyst_headers(seeded)).json()["items"]
    assert [(i["customer_id"], i["simulated"]) for i in queue] == [("Q1", True)]
    dossier = seeded.get(f"/cases/{queue[0]['case_id']}/dossier", headers=analyst_headers(seeded))
    assert dossier.json()["simulated"] is True
    history = seeded.get(f"/cases/{queue[0]['case_id']}/history", headers=analyst_headers(seeded))
    assert any(e["text"].startswith("[simulado]") for e in history.json())


def test_the_cell_gets_its_reviews_and_reversals_from_real_decisions(
    seeded: TestClient, schema: SchemaUrls
) -> None:
    rows = _exec(
        schema,
        "SELECT payload->>'decision', result->'review'->'cell'->>'language', "
        "(result->'review'->>'reversal')::boolean FROM audit_log "
        "WHERE actor = 'human' AND action = 'decision' ORDER BY id",
    )
    assert rows == [("approve", "pt", False), ("reject", "pt", True), ("approve", "pt", False)]


def test_a_customer_case_is_not_simulated(seeded: TestClient, schema: SchemaUrls) -> None:
    headers = customer_headers_for_demo(seeded)
    r = seeded.post(
        "/chat", json={"message": "No reconozco un cargo de Liverpool"}, headers=headers
    )
    assert r.status_code == 200
    assert _exec(schema, "SELECT simulated FROM cases WHERE id = :c", c=r.json()["case_id"]) == [
        (False,)
    ]


def customer_headers_for_demo(client: TestClient) -> dict[str, str]:
    """Signs the persona in with its invented document and the demo code."""
    assert client.post("/auth/otp/request", json=MX_DOC).status_code == 202
    r = client.post("/auth/otp/verify", json={**MX_DOC, "code": DEMO_CODE})
    return {"authorization": f"Bearer {r.json()['access_token']}"}


# ---- CA2: the reset ----------------------------------------------------------------------


def test_the_reset_brings_back_the_seeded_state_after_a_walkthrough(
    seeded: TestClient, schema: SchemaUrls
) -> None:
    before = _state(schema)
    assert before["draw"] == [(1, False)] and before["reviews"] == [(3, 1)]
    # The seeded decisions are counted in their cell like any other review (TRZ-30).
    assert before["cells"] == [("unrecognized_charge", "pt", "A0", 3, 1)]
    analyst = analyst_headers(seeded)
    # A walkthrough: the persona registers and blocks, the analyst decides, the switch moves.
    headers = customer_headers_for_demo(seeded)
    t1 = seeded.post(
        "/chat", json={"message": "No reconozco un cargo de Liverpool"}, headers=headers
    ).json()
    case = t1["case_id"]
    option = t1["options"][0]["transaction_id"]
    seeded.post(
        "/chat", json={"message": "Es este", "case_id": case, "option": option}, headers=headers
    )
    t3 = seeded.post(
        "/chat",
        json={"message": "Sigo", "case_id": case, "recognition": "not_recognized"},
        headers=headers,
    ).json()
    t4 = seeded.post(
        "/chat",
        json={
            "message": "Sí",
            "case_id": case,
            "confirm_action_id": t3["pending_action"]["action_id"],
        },
        headers=headers,
    ).json()
    assert t4["outcome"] == "registered_verified"
    seeded.post(
        "/chat",
        json={
            "message": "Sí",
            "case_id": case,
            "confirm_action_id": t4["pending_action"]["action_id"],
        },
        headers=headers,
    )
    persona = _roles(schema)["mx_main"][0]
    blocked = "SELECT product_status FROM products WHERE product_id = :p"
    assert _exec(schema, blocked, p=f"P{persona}") == [("Blocked",)]
    seeded.put("/automation", json={"all_to_human": True}, headers=analyst)
    # The analyst rejects the case of the queue: one more review, in the ES cell.
    queued = _roles(schema)["queue_mx"][0]
    [(queued_case,)] = _exec(schema, "SELECT id FROM cases WHERE customer_id = :c", c=queued)
    r = seeded.post(
        f"/cases/{queued_case}/decision",
        json={"decision": "reject", "reason": "other"},
        headers=analyst,
    )
    assert r.status_code == 200, r.text
    assert ("unrecognized_charge", "es", "A0", 1, 1) in _state(schema)["cells"]
    assert _state(schema) != before

    r = _reset(seeded)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "cases_created": 4,
        "reviews": 3,
        "reversals": 1,
        "demo_version": "test-demo",
    }
    assert _state(schema) == before
    # The persona's session ended; the analyst's did not.
    assert seeded.get("/me", headers=headers).status_code == 401
    assert seeded.get("/queue", headers=analyst).status_code == 200
    # Only the reset row and the seeded cases' rows are in the audit log.
    assert _exec(schema, "SELECT count(*) FROM audit_log WHERE action = 'demo_reset'") == [(1,)]


def test_the_first_registration_after_a_reset_is_drawn_for_audit(
    seeded: TestClient, schema: SchemaUrls
) -> None:
    assert _reset(seeded).status_code == 200
    headers = customer_headers_for_demo(seeded)
    t1 = seeded.post(
        "/chat", json={"message": "No reconozco un cargo de Liverpool"}, headers=headers
    ).json()
    case = t1["case_id"]
    option = t1["options"][0]["transaction_id"]
    seeded.post(
        "/chat", json={"message": "Es este", "case_id": case, "option": option}, headers=headers
    )
    t3 = seeded.post(
        "/chat",
        json={"message": "Sigo", "case_id": case, "recognition": "not_recognized"},
        headers=headers,
    ).json()
    seeded.post(
        "/chat",
        json={
            "message": "Sí",
            "case_id": case,
            "confirm_action_id": t3["pending_action"]["action_id"],
        },
        headers=headers,
    )
    assert _exec(schema, "SELECT kind FROM case_queue WHERE case_id = :c", c=case) == [
        ("audit_sample",)
    ]


def test_the_same_key_resets_once(seeded: TestClient, schema: SchemaUrls) -> None:
    first = _reset(seeded, "same")
    resets = _exec(schema, "SELECT count(*) FROM demo_resets")
    second = _reset(seeded, "same")
    assert first.json() == second.json()
    assert _exec(schema, "SELECT count(*) FROM demo_resets") == resets
    assert _exec(schema, "SELECT count(*) FROM audit_log WHERE action = 'demo_reset'") == [(1,)]


def test_the_reset_needs_its_key(seeded: TestClient) -> None:
    r = seeded.post("/demo/reset", headers=analyst_headers(seeded))
    assert r.status_code == 422 and r.json()["error_code"] == "validation_error"


def test_the_reset_is_for_the_analyst(seeded: TestClient) -> None:
    assert seeded.post("/demo/reset", headers={"idempotency-key": "k"}).status_code == 401
    r = seeded.post(
        "/demo/reset", headers={**customer_headers_for_demo(seeded), "idempotency-key": "k"}
    )
    assert r.status_code == 403


def test_the_demo_routes_do_not_exist_without_demo_mode(schema: SchemaUrls) -> None:
    settings = Settings(database_url=schema.app, llm_enabled=False, demo_mode=False)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        for method, path in (("POST", "/demo/reset"), ("GET", "/demo")):
            r = client.request(method, path, headers={"idempotency-key": "k"})
            assert r.status_code == 404
            assert r.json()["error_code"] == "http_404"
            assert client.get("/no-such-route").json()["message"] == r.json()["message"]


def test_a_database_without_seed_demo_cannot_be_reset(schema: SchemaUrls) -> None:
    settings = Settings(database_url=schema.app, llm_enabled=False)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        assert client.get("/demo", headers=analyst_headers(client)).json()["seeded"] is False
        r = _reset(client)
        assert r.status_code == 409 and r.json()["error_code"] == "demo_not_seeded"


def test_the_demo_state_says_it_can_be_reset(seeded: TestClient) -> None:
    r = seeded.get("/demo", headers=analyst_headers(seeded))
    assert r.json() == {"seeded": True, "demo_version": "test-demo"}


def test_the_reset_with_the_database_down_is_503(
    seeded: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(*_args: object) -> bool:
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    headers = analyst_headers(seeded)
    monkeypatch.setattr(demo, "is_seeded", down)
    r = seeded.post("/demo/reset", headers={**headers, "idempotency-key": "k"})
    assert r.status_code == 503 and r.json()["error_code"] == "db_unavailable"


def test_trazo_app_still_cannot_empty_a_table(seeded: TestClient, schema: SchemaUrls) -> None:
    engine = create_engine(schema.app)
    for sql in ("TRUNCATE cases CASCADE", "DELETE FROM case_queue", "TRUNCATE audit_log"):
        with engine.begin() as conn, pytest.raises(DBAPIError, match="permission denied"):
            conn.execute(text(sql))
    # The reset function asks for the analyst role itself.
    with engine.begin() as conn, pytest.raises(DBAPIError, match="analyst role"):
        conn.execute(text("SELECT demo_reset()"))
    engine.dispose()


# ---- CA4: the evaluation never uses the demo state -----------------------------------------


def test_the_golden_evaluation_is_the_same_with_and_without_the_demo(
    seeded: TestClient, schema: SchemaUrls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.cli import eval as golden

    cases = Path(__file__).resolve().parents[2] / "eval" / "cases"
    picked = tmp_path / "golden"
    picked.mkdir()
    for path in sorted(cases.glob("*.yaml"))[:3]:
        (picked / path.name).write_text(path.read_text())

    def results(out: str) -> list[tuple[str, str, list[Any]]]:
        rows, _ = golden.run(picked, tmp_path / out)
        fields = (*golden.TURN_FIELDS, *golden.POLICY_FIELDS)
        return [
            (r.id, r.status, [[t.actual.get(f) for f in fields] for t in r.turns]) for r in rows
        ]

    # Pointed at the database that holds the demo, then at the same database with the demo
    # emptied: each golden case runs in a schema of its own either way.
    monkeypatch.setenv("ADMIN_DATABASE_URL", schema.admin)
    monkeypatch.setenv("DATABASE_URL", schema.app)
    with_demo = results("with")
    assert all(status == "pass" for _, status, _ in with_demo)
    engine = create_engine(schema.admin)
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.role', 'analyst', true)"))
        conn.execute(text("SELECT demo_reset()"))
        conn.execute(text("DELETE FROM demo_roles"))
    engine.dispose()
    without_demo = results("without")
    assert with_demo == without_demo

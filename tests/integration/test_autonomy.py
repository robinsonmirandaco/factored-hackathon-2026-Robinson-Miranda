"""Autonomy by cell with the Wilson rule (TRZ-30) through the API, on Postgres.

Twenty analyst decisions on cases handed over with a registration recommended close the block of
the unrecognized charge x ES cell. With 10 reversals the cell goes down to A1 (CA1 to CA4) and
the block is in the audit log with r, W, N, the threshold and the reversed cases (CA8). The next
case of the cell then waits for an analyst's approval, and its trace says why (CA6, CA9). At A2
the system records what it would have recommended without showing it, and the analyst's decision
is compared against it (CA7). The level lives in the database: a new app keeps it. The metrics
rebuild the same cells from the audit log alone (TRZ-37 CA2), and the Estado de autonomía tab
shows each cell, its thresholds and its reversed cases (TRZ-31).
"""

import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError

from app.adapters.db.session import Database, SchemaUrls
from app.api.deps import get_analyst_session
from app.core.config import Settings
from app.main import create_app
from tests.agent_support import still_not_recognized
from tests.auth_support import analyst_headers, customer_headers
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 17, 23, 59)
CELL = ("unrecognized_charge", "es")
# C1's ten charges are approved and C2's ten rejected: both are in the 500 to 1000 USD band, so
# each case waits for an analyst with a registration recommended. C3 and C4 have a small charge.
APPROVED = [f"C1A{i}" for i in range(10)]
REJECTED = [f"C2R{i}" for i in range(10)]


def _rows() -> list[dict[str, Any]]:
    rows = [
        transaction(
            tx,
            tx[:2],
            f"P{tx[1]}",
            NOW - timedelta(hours=10 + i),
            amount=800.0,
            currency="USD",
            merchant_name=f"Tienda {tx}",
        )
        for i, tx in enumerate(APPROVED + REJECTED)
    ]
    rows += [
        transaction(
            f"C{n}N",
            f"C{n}",
            f"P{n}",
            NOW - timedelta(hours=5),
            amount=120.0,
            currency="USD",
            merchant_name="Netflix",
        )
        for n in (3, 4)
    ]
    return rows


def _settings(database_url: str) -> Settings:
    return Settings(database_url=database_url, llm_enabled=False, log_level="WARNING")


@pytest.fixture
def client(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    load(
        schema.admin,
        [customer(c) for c in ("C1", "C2", "C3", "C4")],
        [card(f"P{n}", f"C{n}") for n in (1, 2, 3, 4)],
        _rows(),
    )
    with TestClient(create_app(_settings(database_url)), raise_server_exceptions=False) as c:
        yield c


def _query(schema: SchemaUrls, sql: str, **params: Any) -> list[tuple]:
    engine = create_engine(schema.admin)
    with engine.begin() as conn:
        result = conn.execute(text(sql), params)
        rows = [tuple(r) for r in result] if result.returns_rows else []
    engine.dispose()
    return rows


def _cell(schema: SchemaUrls) -> list[tuple]:
    return _query(
        schema,
        "SELECT level, block_reviews, block_reversals, good_blocks FROM autonomy_cells "
        "WHERE intent = :i AND language = :l",
        i=CELL[0],
        l=CELL[1],
    )


def _handed(client: TestClient, who: dict[str, str], transaction_id: str) -> dict[str, Any]:
    """Presses "No lo reconozco" on a charge and keeps not recognizing it."""
    first = client.post(
        "/chat", json={"message": "No lo reconozco", "transaction_id": transaction_id}, headers=who
    )
    assert first.status_code == 200, first.text
    return still_not_recognized(client, first.json()["case_id"], who)


def _decide(client: TestClient, analyst: dict[str, str], case_id: str, **body: Any) -> Any:
    r = client.post(f"/cases/{case_id}/decision", json=body, headers=analyst)
    assert r.status_code == 200, r.text
    return r.json()


def _nineteen_then(client: TestClient, last: str) -> str:
    """Nineteen decisions with nine reversals, then the twentieth; returns its case."""
    analyst = analyst_headers(client)
    c1, c2 = customer_headers(client, "C1"), customer_headers(client, "C2")
    order = [(c1, tx, "approve") for tx in APPROVED] + [(c2, tx, "reject") for tx in REJECTED]
    # The twentieth decision is the last approval or the last rejection.
    order.sort(key=lambda o: o[2] == last)
    case_id = ""
    for who, tx, decision in order:
        case_id = _handed(client, who, tx)["case_id"]
        if decision == "approve":
            _decide(client, analyst, case_id, decision="approve")
        else:
            _decide(client, analyst, case_id, decision="reject", reason="wrong_charge")
    return case_id


def test_ten_reversals_in_a_block_of_20_take_the_cell_down_to_a1(
    client: TestClient, schema: SchemaUrls
) -> None:
    last = _nineteen_then(client, "reject")

    # CA4: 10 of 20 gives W >= 0.30; the cell is at A1 with a new, empty block.
    assert _cell(schema) == [("A1", 0, 0, 0)]
    # CA8: one row for the closed block, belonging to no customer, on the case that closed it.
    [(case_id, customer_id, result)] = _query(
        schema,
        "SELECT case_id, customer_id, result FROM audit_log "
        "WHERE actor = 'system' AND action = 'autonomy_block'",
    )
    assert (case_id, customer_id) == (last, None)
    assert {k: result[k] for k in ("level_before", "level_after", "changed", "n", "reversals")} == {
        "level_before": "A0",
        "level_after": "A1",
        "changed": True,
        "n": 20,
        "reversals": 10,
    }
    assert (result["r"], result["w"], result["z"]) == (0.5, 0.3274, 1.645)
    assert (result["threshold"], result["threshold_value"]) == ("demote_if_wilson_lower_gte", 0.3)
    reversed_cases = _query(
        schema,
        "SELECT id FROM cases WHERE customer_id = 'C2' ORDER BY created_at",
    )
    assert [r["case_id"] for r in result["reversed"]] == [c for (c,) in reversed_cases]
    assert {r["reason"] for r in result["reversed"]} == {"wrong_charge"}

    history = client.get(f"/cases/{last}/history", headers=analyst_headers(client)).json()
    assert any("La celda pasa de A0 a A1" in e["text"] for e in history)


def test_nine_reversals_keep_the_cell_and_start_a_new_block(
    client: TestClient, schema: SchemaUrls
) -> None:
    analyst = analyst_headers(client)
    c1, c2 = customer_headers(client, "C1"), customer_headers(client, "C2")
    for tx in REJECTED[:9]:
        _decide(
            client, analyst, _handed(client, c2, tx)["case_id"], decision="reject", reason="other"
        )
    for tx in APPROVED:
        _decide(client, analyst, _handed(client, c1, tx)["case_id"], decision="approve")
    # CA2: nothing is computed before the block closes.
    assert _cell(schema) == [("A0", 19, 9, 0)]
    _decide(client, analyst, _handed(client, c2, REJECTED[9])["case_id"], decision="approve")
    assert _cell(schema) == [("A0", 0, 0, 0)]
    [(result,)] = _query(schema, "SELECT result FROM audit_log WHERE action = 'autonomy_block'")
    assert (result["changed"], result["w"], result["threshold"]) == (False, 0.2841, None)


def test_the_next_case_of_a_cell_at_a1_waits_for_approval_and_its_trace_says_why(
    client: TestClient, schema: SchemaUrls, database_url: str
) -> None:
    _nineteen_then(client, "reject")
    [(block_id,)] = _query(schema, "SELECT id FROM audit_log WHERE action = 'autonomy_block'")

    # CA9, CA6: a 120 USD charge the cell registered on its own at A0 now needs an analyst.
    c3 = customer_headers(client, "C3")
    turn = _handed(client, c3, "C3N")
    assert turn["outcome"] == "pending_analyst_approval", turn
    analyst = analyst_headers(client)
    trace = client.get(f"/cases/{turn['case_id']}/trace", headers=analyst).json()
    [decide] = [e["result"] for e in trace if (e["actor"], e["action"]) == ("policy", "decide")]
    assert (decide["rule"], decide["autonomy_level"]) == ("approval.autonomy_a1", "A1")
    change = decide["autonomy_change"]
    assert (change["n"], change["reversals"], change["r"], change["w"]) == (20, 10, 0.5, 0.3274)
    assert change["audit_id"] == block_id

    dossier = client.get(f"/cases/{turn['case_id']}/dossier", headers=analyst).json()
    rule = dossier["policy_rule_triggered"]
    assert rule["autonomy_change"]["source"] == {"table": "audit_log", "id": str(block_id)}
    history = client.get(f"/cases/{turn['case_id']}/history", headers=analyst).json()
    assert any("La celda está en A1 desde un bloque" in e["text"] for e in history)

    # In A1 each case of the cell is a review: approving it opens the next block.
    _decide(client, analyst, turn["case_id"], decision="approve")
    assert _cell(schema) == [("A1", 1, 0, 0)]

    # The level lives in the database: a new app, as after a restart, still reads A1.
    with TestClient(create_app(_settings(database_url)), raise_server_exceptions=False) as again:
        # C3 has an open dispute now, which would escalate first: C4 has none.
        later = _handed(again, customer_headers(again, "C4"), "C4N")
        assert later["outcome"] == "pending_analyst_approval", later


def test_the_metrics_rebuild_every_cell_from_the_audit_log_as_autonomy_cells_keeps_it(
    client: TestClient, schema: SchemaUrls
) -> None:
    # TRZ-37 CA2: a closed block that changed the level, then one review of the next block.
    _nineteen_then(client, "reject")
    analyst = analyst_headers(client)
    _decide(
        client,
        analyst,
        _handed(client, customer_headers(client, "C3"), "C3N")["case_id"],
        decision="approve",
    )
    metrics = client.get("/metrics", headers=analyst).json()["autonomy"]
    stored = _query(
        schema,
        "SELECT intent, language, level, block_reviews, block_reversals, good_blocks "
        "FROM autonomy_cells ORDER BY 1, 2",
    )
    rebuilt = [
        (
            c["intent"],
            c["language"],
            c["level"],
            c["block_reviews"],
            c["block_reversals"],
            c["good_blocks"],
        )
        for c in metrics
    ]
    assert rebuilt == stored == [("unrecognized_charge", "es", "A1", 1, 0, 0)]


def test_the_same_decision_sent_again_is_not_counted_twice(
    client: TestClient, schema: SchemaUrls
) -> None:
    analyst = analyst_headers(client)
    case_id = _handed(client, customer_headers(client, "C2"), REJECTED[0])["case_id"]
    _decide(client, analyst, case_id, decision="reject", reason="wrong_charge")
    _decide(client, analyst, case_id, decision="reject", reason="wrong_charge")
    assert _cell(schema) == [("A0", 1, 1, 0)]


def test_at_a2_the_recommendation_is_recorded_not_shown_and_compared(
    client: TestClient, schema: SchemaUrls
) -> None:
    _query(
        schema,
        "INSERT INTO autonomy_cells (intent, language, level) VALUES (:i, :l, 'A2')",
        i=CELL[0],
        l=CELL[1],
    )
    turn = _handed(client, customer_headers(client, "C3"), "C3N")
    assert turn["outcome"] == "escalated", turn
    analyst = analyst_headers(client)
    case_id = turn["case_id"]
    trace = client.get(f"/cases/{case_id}/trace", headers=analyst).json()
    [decide] = [e["result"] for e in trace if (e["actor"], e["action"]) == ("policy", "decide")]
    # CA7: what it would have recommended is recorded...
    assert decide["rule"] == "escalate.autonomy_a2"
    assert decide["recommended"] == "register_and_offer_block"
    # ...and not shown in the queue, the case or the dossier.
    item = next(i for i in client.get("/queue", headers=analyst).json()["items"])
    assert (item["recommended_action"], item["can_approve"]) == (None, True)
    assert client.get(f"/cases/{case_id}", headers=analyst).json()["recommended_action"] is None
    dossier = client.get(f"/cases/{case_id}/dossier", headers=analyst).json()
    assert (dossier["recommended_action"], dossier["recommendation_hidden"]) == (None, True)

    # The analyst registers it: agreement with the hidden recommendation, one review of the cell.
    _decide(client, analyst, case_id, decision="approve")
    [(review,)] = _query(
        schema,
        "SELECT result->'review' FROM audit_log WHERE actor = 'human' AND action = 'decision'",
    )
    assert review["system_action"] == "register_and_offer_block"
    assert review["reversal"] is False
    assert _cell(schema) == [("A2", 1, 0, 0)]


def test_a_customer_session_reads_the_cell_but_cannot_change_it(
    client: TestClient, schema: SchemaUrls, database_url: str
) -> None:
    _query(
        schema,
        "INSERT INTO autonomy_cells (intent, language, level) VALUES (:i, :l, 'A1')",
        i=CELL[0],
        l=CELL[1],
    )
    db = Database(database_url)
    try:
        with db.session(customer_id="C3") as s:
            assert s.execute(text("SELECT level FROM autonomy_cells")).scalars().all() == ["A1"]
            assert s.execute(text("UPDATE autonomy_cells SET level = 'A0'")).rowcount == 0
        with pytest.raises(DBAPIError), db.session(customer_id="C3") as s:
            s.execute(
                text(
                    "INSERT INTO autonomy_cells (intent, language, level) VALUES ('x', 'es', 'A0')"
                )
            )
    finally:
        db.dispose()
    assert _cell(schema) == [("A1", 0, 0, 0)]


# ---- the Estado de autonomía tab (TRZ-31) ---------------------------------------------------


def _tab(client: TestClient) -> dict[str, Any]:
    r = client.get("/autonomy", headers=analyst_headers(client))
    assert r.status_code == 200, r.text
    return r.json()


def _es(tab: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in tab["cells"] if (c["intent"], c["language"]) == CELL)


def test_the_tab_lists_every_cell_with_the_thresholds_before_any_review(
    client: TestClient,
) -> None:
    tab = _tab(client)
    # CA2: the thresholds of the policy.
    assert tab["thresholds"] == {
        "window_n": 20,
        "z": 1.645,
        "demote_if_wilson_lower_gte": 0.3,
        "promote_if_rate_lt": 0.15,
        "promote_after_consecutive_windows": 2,
        "audit_sample_rate": 0.1,
    }
    # CA1: one row per dispute intent and language, at the initial level with no row yet.
    assert [(c["intent"], c["language"]) for c in tab["cells"]] == [
        (i, lang)
        for i in ("unrecognized_charge", "billing_error_amount", "billing_error_duplicate")
        for lang in ("es", "pt")
    ]
    cell = _es(tab)
    assert (cell["level"], cell["block_reviews"], cell["rate"], cell["last_block"]) == (
        "A0",
        0,
        None,
        None,
    )


def test_the_tab_shows_the_open_block_and_then_the_block_that_changed_the_level(
    client: TestClient, schema: SchemaUrls
) -> None:
    analyst = analyst_headers(client)
    c1, c2 = customer_headers(client, "C1"), customer_headers(client, "C2")
    for tx in REJECTED[:9]:
        _decide(
            client,
            analyst,
            _handed(client, c2, tx)["case_id"],
            decision="reject",
            reason="wrong_charge",
        )
    for tx in APPROVED:
        _decide(client, analyst, _handed(client, c1, tx)["case_id"], decision="approve")

    # Nineteen reviews: r of the open block, no W yet (CA1), its nine reversals (CA3).
    cell = _es(_tab(client))
    assert (cell["level"], cell["block_reviews"], cell["block_reversals"]) == ("A0", 19, 9)
    assert cell["rate"] == 0.4737 and cell["last_block"] is None
    assert len(cell["reversed_open_block"]) == 9 and cell["reversed_last_block"] == []

    # CA4: the twentieth review closes the block; the next read shows the change.
    last = _handed(client, c2, REJECTED[9])["case_id"]
    _decide(client, analyst, last, decision="reject", reason="should_not_act")
    cell = _es(_tab(client))
    assert (cell["level"], cell["block_reviews"], cell["rate"]) == ("A1", 0, None)
    block = cell["last_block"]
    assert block == cell["last_change"]
    assert (block["closed_by"], block["n"], block["reversals"], block["r"], block["w"]) == (
        last,
        20,
        10,
        0.5,
        0.3274,
    )
    assert (block["level_before"], block["level_after"], block["changed"]) == ("A0", "A1", True)
    reversed_cases = [
        c
        for (c,) in _query(
            schema, "SELECT id FROM cases WHERE customer_id = 'C2' ORDER BY created_at"
        )
    ]
    assert [r["case_id"] for r in cell["reversed_last_block"]] == reversed_cases
    assert [r["reason"] for r in cell["reversed_last_block"]] == ["wrong_charge"] * 9 + [
        "should_not_act"
    ]
    assert not any(r["simulated"] for r in cell["reversed_last_block"])
    assert cell["reversed_open_block"] == []

    # CA3: a reversal in the new block is listed apart from those of the closed block.
    next_case = _handed(client, customer_headers(client, "C3"), "C3N")["case_id"]
    _decide(client, analyst, next_case, decision="reject", reason="insufficient_data")
    cell = _es(_tab(client))
    assert (cell["level"], cell["block_reviews"], cell["block_reversals"]) == ("A1", 1, 1)
    assert cell["reversed_open_block"] == [
        {"case_id": next_case, "reason": "insufficient_data", "simulated": False}
    ]
    assert len(cell["reversed_last_block"]) == 10


def test_the_tab_returns_no_real_clock_date(client: TestClient) -> None:
    _nineteen_then(client, "reject")
    body = client.get("/autonomy", headers=analyst_headers(client)).text
    # Design 10.2 rule 7: the audit log's created_at is the real clock.
    assert "created_at" not in body and "updated_at" not in body
    assert re.search(r"\d{4}-\d{2}-\d{2}", body) is None


def test_outside_demo_mode_the_tab_is_not_labeled_simulated(
    schema: SchemaUrls, database_url: str, client: TestClient
) -> None:
    settings = Settings(
        database_url=database_url, llm_enabled=False, log_level="WARNING", demo_mode=False
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        assert _tab(c)["simulated"] is False
    assert _tab(client)["simulated"] is True


def test_the_tab_is_for_the_analyst_only(client: TestClient) -> None:
    assert client.get("/autonomy").status_code == 401
    r = client.get("/autonomy", headers=customer_headers(client, "C1"))
    assert r.status_code == 403 and r.json()["error_code"] == "forbidden"


class _BrokenSession:
    info: dict[str, Any] = {}

    def execute(self, *_args: object) -> None:
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    scalars = execute


def test_the_tab_with_the_database_down_is_503(client: TestClient) -> None:
    app: FastAPI = client.app  # type: ignore[assignment]
    app.dependency_overrides[get_analyst_session] = lambda: _BrokenSession()
    r = client.get("/autonomy")
    app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["error_code"] == "db_unavailable"

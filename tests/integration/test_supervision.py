"""Supervision (TRZ-29, TRZ-32, TRZ-35) through the API, on Postgres.

TRZ-29: every case the system registers on its own is drawn with the seed of the policy; the
draw is in the trace and a selected case enters the queue as an audit sample (CA1, CA2, CA3).
Confirming an audit changes nothing for the customer; reversing it puts the clarification in
review by an analyst, notifies the customer and leaves a review TRZ-30 can count (CA5).
TRZ-32: each decision of the analyst writes one notification for the customer, read and marked
read through /me/notifications (CA1, CA2, CA4). TRZ-35: the global switch, read on every
decision, sends every dispute to the queue; each change is in the audit log (CA1 to CA4).
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.adapters.db.session import Database, SchemaUrls
from app.api.deps import get_analyst_session, get_customer_session
from app.core.config import Settings
from app.main import create_app
from tests.agent_support import still_not_recognized
from tests.auth_support import analyst_headers, customer_headers
from tests.integration.test_analyst_queue import BrokenSession
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 17, 23, 59)
SEED = 20260934
NETFLIX = "No reconozco un cargo de 120 dólares en Netflix"
LIVERPOOL = "No reconozco un cargo de 800 dólares en Liverpool"
SEARS = "No reconozco un cargo de 1500 dólares en Sears"


def _charges(customer_id: str, product: str) -> list[dict[str, Any]]:
    rows = [("N", 120.0, "Netflix", 20), ("L", 800.0, "Liverpool", 30), ("S", 1500.0, "Sears", 40)]
    return [
        transaction(
            f"{customer_id}{tx}",
            customer_id,
            product,
            NOW - timedelta(hours=hours),
            amount=amount,
            currency="USD",
            merchant_name=merchant,
        )
        for tx, amount, merchant, hours in rows
    ]


@pytest.fixture
def client(schema: SchemaUrls, database_url: str) -> Iterator[TestClient]:
    load(
        schema.admin,
        [customer("C1"), customer("C2")],
        [card("P1", "C1"), card("P2", "C2")],
        _charges("C1", "P1") + _charges("C2", "P2"),
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


def _next_draw(schema: SchemaUrls, n: int) -> None:
    """Makes n the number of the next draw."""
    engine = create_engine(schema.admin)
    with engine.begin() as conn:
        conn.execute(text("SELECT setval('audit_draw_seq', :n, false)"), {"n": n})
    engine.dispose()


def _pending(client: TestClient, who: dict[str, str], message: str) -> dict[str, Any]:
    first = client.post("/chat", json={"message": message}, headers=who)
    assert first.status_code == 200, first.text
    return still_not_recognized(client, first.json()["case_id"], who)


def _confirm(client: TestClient, who: dict[str, str], pending: dict[str, Any]) -> dict[str, Any]:
    r = client.post(
        "/chat",
        json={
            "message": "sí",
            "case_id": pending["case_id"],
            "confirm_action_id": pending["pending_action"]["action_id"],
        },
        headers=who,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _registered(client: TestClient, who: dict[str, str], message: str = NETFLIX) -> dict:
    pending = _pending(client, who, message)
    assert pending["outcome"] == "awaiting_confirmation", pending
    done = _confirm(client, who, pending)
    assert done["outcome"] == "registered_verified", done
    return done


def _queue_item(client: TestClient, analyst: dict[str, str], case_id: str) -> dict | None:
    items = client.get("/queue", headers=analyst).json()["items"]
    return next((i for i in items if i["case_id"] == case_id), None)


def _decide(client: TestClient, analyst: dict[str, str], case_id: str, **body: Any) -> Any:
    return client.post(f"/cases/{case_id}/decision", json=body, headers=analyst)


def _notes(client: TestClient, who: dict[str, str]) -> dict[str, Any]:
    r = client.get("/me/notifications", headers=who)
    assert r.status_code == 200, r.text
    return r.json()


# ---- TRZ-29: audit sample -------------------------------------------------------------------


def test_the_first_case_resolved_alone_is_drawn_and_enters_the_queue_as_audit(
    client: TestClient, schema: SchemaUrls
) -> None:
    c1, analyst = customer_headers(client, "C1"), analyst_headers(client)
    case_id = _registered(client, c1)["case_id"]

    # CA1, CA2: the draw is in the trace with its seed, number, rho and the number drawn.
    trace = client.get(f"/cases/{case_id}/trace", headers=analyst).json()
    [drawn] = [e for e in trace if (e["actor"], e["action"]) == ("policy", "audit_draw")]
    assert drawn["payload"] == {"seed": SEED, "n": 1, "rho": 0.1}
    assert drawn["result"]["selected"] is True and drawn["result"]["u"] < 0.1

    # CA2, CA3: the case is in the queue as an audit sample, and the case itself stays as it is.
    queue = client.get("/queue", headers=analyst).json()
    item = next(i for i in queue["items"] if i["case_id"] == case_id)
    assert (item["kind"], item["reason"], item["status"], item["priority"]) == (
        "audit_sample",
        "audit.sample",
        "registered_verified",
        "normal",
    )
    assert queue["counts"]["audit"] == 1 and queue["audit_sample_rate"] == 0.1
    audit_only = client.get("/queue?filter=audit", headers=analyst).json()["items"]
    assert [i["case_id"] for i in audit_only] == [case_id]

    dossier = client.get(f"/cases/{case_id}/dossier", headers=analyst).json()
    assert dossier["case_kind"] == "audit_sample"
    assert (dossier["audit_draw"]["seed"], dossier["audit_draw"]["n"]) == (SEED, 1)
    history = [h["text"] for h in client.get(f"/cases/{case_id}/history", headers=analyst).json()]
    assert (
        f"Sorteo de auditoría (n = 1, semilla {SEED}): el caso salió en la muestra (ρ = 0,10)."
        in (history)
    )


def test_a_case_not_drawn_stays_out_of_the_queue(client: TestClient, schema: SchemaUrls) -> None:
    _next_draw(schema, 3)  # u = 0.81 with the seed of the policy
    c1, analyst = customer_headers(client, "C1"), analyst_headers(client)
    case_id = _registered(client, c1)["case_id"]
    [(payload, result)] = _query(
        schema, "SELECT payload, result FROM audit_log WHERE action = 'audit_draw'"
    )
    assert payload["n"] == 3 and result["selected"] is False
    assert _queue_item(client, analyst, case_id) is None
    r = client.get(f"/cases/{case_id}/dossier", headers=analyst)
    assert r.status_code == 409


def test_a_replayed_confirmation_draws_once(client: TestClient, schema: SchemaUrls) -> None:
    c1 = customer_headers(client, "C1")
    pending = _pending(client, c1, NETFLIX)
    _confirm(client, c1, pending)
    _confirm(client, c1, pending)
    assert _query(schema, "SELECT count(*) FROM audit_log WHERE action = 'audit_draw'") == [(1,)]
    assert _query(schema, "SELECT count(*) FROM case_queue WHERE kind = 'audit_sample'") == [(1,)]


def test_confirming_an_audit_changes_nothing_for_the_customer(
    client: TestClient, schema: SchemaUrls
) -> None:
    c1, analyst = customer_headers(client, "C1"), analyst_headers(client)
    case_id = _registered(client, c1)["case_id"]
    before = client.get("/me/clarifications", headers=c1).json()

    r = _decide(client, analyst, case_id, decision="approve")
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["dispute_folio"]) == ("registered_verified", None)

    assert _queue_item(client, analyst, case_id) is None
    assert client.get("/me/clarifications", headers=c1).json() == before
    assert _notes(client, c1) == {"items": [], "unread": 0}
    [(result,)] = _query(
        schema, "SELECT result FROM audit_log WHERE action = 'decision' AND case_id = :c", c=case_id
    )
    assert result["review"] == {
        "of": "audit_sample",
        "system_action": "register_and_offer_block",
        "reversal": False,
        "reason": None,
        "cell": {"intent": "unrecognized_charge", "language": "es"},
    }


def test_reversing_an_audit_puts_it_in_review_notifies_and_keeps_the_dispute(
    client: TestClient, schema: SchemaUrls
) -> None:
    c1, analyst = customer_headers(client, "C1"), analyst_headers(client)
    done = _registered(client, c1)
    case_id = done["case_id"]

    r = _decide(client, analyst, case_id, decision="reject", reason="wrong_charge")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "in_review"

    # The dispute is not annulled: no tool annuls one (declared limit).
    assert _query(schema, "SELECT folio, status FROM disputes") == [
        (done["dispute_folio"], "opened")
    ]
    mine = next(c for c in client.get("/me/clarifications", headers=c1).json())
    assert (mine["id"], mine["status"]) == (done["dispute_folio"], "in_review")
    notes = _notes(client, c1)
    [note] = notes["items"]
    assert (note["kind"], note["case_id"], note["read"], notes["unread"]) == (
        "audit_reversed",
        case_id,
        False,
        1,
    )
    assert note["text"] == "Tu aclaración está en revisión por una analista."

    # What TRZ-30 counts: a reversal of an automatic action, with its reason and cell.
    [(payload, result)] = _query(
        schema,
        "SELECT payload, result FROM audit_log WHERE action = 'decision' AND case_id = :c",
        c=case_id,
    )
    assert (payload["kind"], payload["reason"]) == ("audit_sample", "wrong_charge")
    assert result["review"]["reversal"] is True and result["review"]["reason"] == "wrong_charge"
    assert result["dispute_annulled"] is False

    # A second send of the same decision repeats nothing.
    again = _decide(client, analyst, case_id, decision="reject", reason="wrong_charge")
    assert again.status_code == 200 and again.json()["status"] == "in_review"
    assert _notes(client, c1)["unread"] == 1
    # The customer cannot reopen it from the chat: a new message is a new case.
    assert _queue_item(client, analyst, case_id) is None


def test_an_audit_is_not_asked_about_and_a_reversal_needs_a_reason(
    client: TestClient, schema: SchemaUrls
) -> None:
    c1, analyst = customer_headers(client, "C1"), analyst_headers(client)
    case_id = _registered(client, c1)["case_id"]
    asked = _decide(client, analyst, case_id, decision="need_info", question="¿Seguro?")
    assert (asked.status_code, asked.json()["error_code"]) == (409, "decision_not_allowed")
    bare = _decide(client, analyst, case_id, decision="reject")
    assert (bare.status_code, bare.json()["error_code"]) == (400, "reason_required")
    assert _queue_item(client, analyst, case_id) is not None


# ---- TRZ-32: notifications ------------------------------------------------------------------


def test_each_decision_of_the_analyst_notifies_the_customer(
    client: TestClient, schema: SchemaUrls
) -> None:
    c2, analyst = customer_headers(client, "C2"), analyst_headers(client)
    approval = _pending(client, c2, LIVERPOOL)
    assert approval["outcome"] == "pending_analyst_approval", approval
    large = _pending(client, c2, SEARS)
    assert large["outcome"] == "escalated", large

    approved = _decide(client, analyst, approval["case_id"], decision="approve").json()
    asked = _decide(client, analyst, large["case_id"], decision="need_info", question="¿Cuándo?")
    assert asked.status_code == 200

    notes = _notes(client, c2)
    kinds = {n["kind"]: n for n in notes["items"]}
    assert set(kinds) == {"approved", "info_requested"} and notes["unread"] == 2
    assert approved["dispute_folio"] in kinds["approved"]["text"]
    assert "24 de junio de 2026" in kinds["info_requested"]["text"]
    # CA4: every text passed the fact checker, and the audit log says so.
    assert _query(schema, "SELECT bool_and(checked), count(*) FROM notifications") == [(True, 2)]
    assert _query(
        schema, "SELECT count(*) FROM audit_log WHERE action = 'notify' AND customer_id = 'C2'"
    ) == [(2,)]


def test_a_rejection_notifies_its_reason_in_plain_words(
    client: TestClient, schema: SchemaUrls
) -> None:
    c2, analyst = customer_headers(client, "C2"), analyst_headers(client)
    large = _pending(client, c2, SEARS)
    r = _decide(client, analyst, large["case_id"], decision="reject", reason="insufficient_data")
    assert r.status_code == 200
    [note] = _notes(client, c2)["items"]
    assert note["text"] == (
        "Una analista revisó tu aclaración y no procedió: "
        "no hubo datos suficientes para registrarla."
    )
    mine = next(
        c
        for c in client.get("/me/clarifications", headers=c2).json()
        if c["case_id"] == large["case_id"]
    )
    assert (mine["status"], mine["reason"]) == ("rejected", "insufficient_data")


def test_opening_a_notification_marks_it_read_once(client: TestClient, schema: SchemaUrls) -> None:
    c1, analyst = customer_headers(client, "C1"), analyst_headers(client)
    case_id = _registered(client, c1)["case_id"]
    _decide(client, analyst, case_id, decision="reject", reason="should_not_act")
    [note] = _notes(client, c1)["items"]

    first = client.post(f"/me/notifications/{note['id']}/read", headers=c1)
    assert first.status_code == 200 and first.json()["read"] is True
    again = client.post(f"/me/notifications/{note['id']}/read", headers=c1)
    assert again.status_code == 200 and again.json()["read"] is True
    assert _notes(client, c1)["unread"] == 0
    assert _query(schema, "SELECT count(*) FROM notifications WHERE read_at IS NOT NULL") == [(1,)]


def test_another_customer_neither_sees_nor_reads_a_notification(
    client: TestClient, schema: SchemaUrls
) -> None:
    c1, analyst = customer_headers(client, "C1"), analyst_headers(client)
    case_id = _registered(client, c1)["case_id"]
    _decide(client, analyst, case_id, decision="reject", reason="other")
    [note] = _notes(client, c1)["items"]
    c2 = customer_headers(client, "C2")
    assert _notes(client, c2) == {"items": [], "unread": 0}
    r = client.post(f"/me/notifications/{note['id']}/read", headers=c2)
    assert (r.status_code, r.json()["error_code"]) == (404, "notification_not_found")
    assert _query(schema, "SELECT read_at FROM notifications") == [(None,)]


def test_notifications_validate_the_id_and_the_role(client: TestClient) -> None:
    c1, analyst = customer_headers(client, "C1"), analyst_headers(client)
    assert client.post("/me/notifications/abc/read", headers=c1).status_code == 422
    assert client.get("/me/notifications", headers=analyst).status_code == 403
    assert client.get("/me/notifications").status_code == 401


def test_notifications_with_the_database_down_are_503(client: TestClient) -> None:
    c1 = customer_headers(client, "C1")
    app: FastAPI = client.app  # type: ignore[assignment]
    app.dependency_overrides[get_customer_session] = lambda: BrokenSession()
    listed = client.get("/me/notifications", headers=c1)
    read = client.post("/me/notifications/1/read", headers=c1)
    app.dependency_overrides.clear()
    assert listed.status_code == 503 and listed.json()["error_code"] == "db_unavailable"
    assert read.status_code == 503 and read.json()["error_code"] == "db_unavailable"


# ---- TRZ-35: global automation switch -------------------------------------------------------


def test_the_switch_changes_without_a_redeploy_and_each_change_is_audited(
    client: TestClient, schema: SchemaUrls
) -> None:
    analyst = analyst_headers(client)
    assert client.get("/automation", headers=analyst).json()["all_to_human"] is False

    on = client.put("/automation", json={"all_to_human": True}, headers=analyst)
    assert on.status_code == 200
    assert (on.json()["all_to_human"], on.json()["changed_by"]) == (True, "analista.demo")
    # The same value again changes nothing and writes no row.
    assert (
        client.put("/automation", json={"all_to_human": True}, headers=analyst).status_code == 200
    )
    off = client.put("/automation", json={"all_to_human": False}, headers=analyst)
    assert off.json()["all_to_human"] is False

    rows = _query(
        schema,
        "SELECT actor, result, trace_id FROM audit_log WHERE action = 'automation_switch' "
        "ORDER BY id",
    )
    assert [(a, r["before"], r["after"], r["analyst"]) for a, r, _ in rows] == [
        ("human", False, True, "analista.demo"),
        ("human", True, False, "analista.demo"),
    ]
    assert all(trace for *_, trace in rows)


def test_the_switch_validates_the_body_and_the_role(client: TestClient) -> None:
    c1, analyst = customer_headers(client, "C1"), analyst_headers(client)
    assert client.put(
        "/automation", json={"all_to_human": "maybe"}, headers=analyst
    ).status_code == (422)
    assert client.put("/automation", json={}, headers=analyst).status_code == 422
    assert client.put("/automation", json={"all_to_human": True}, headers=c1).status_code == 403
    assert client.get("/automation", headers=c1).status_code == 403


def test_the_switch_with_the_database_down_is_503(client: TestClient) -> None:
    analyst = analyst_headers(client)
    app: FastAPI = client.app  # type: ignore[assignment]
    app.dependency_overrides[get_analyst_session] = lambda: BrokenSession()
    got = client.get("/automation", headers=analyst)
    put = client.put("/automation", json={"all_to_human": True}, headers=analyst)
    app.dependency_overrides.clear()
    assert got.status_code == 503 and put.status_code == 503


def test_with_the_switch_on_the_normal_flow_ends_in_the_queue(
    client: TestClient, schema: SchemaUrls
) -> None:
    # CA2, CA4: the same message registers with the switch off and goes to a person with it on.
    c1, c2, analyst = (
        customer_headers(client, "C1"),
        customer_headers(client, "C2"),
        analyst_headers(client),
    )
    assert _registered(client, c1)["dispute_folio"]

    client.put("/automation", json={"all_to_human": True}, headers=analyst)
    held = _pending(client, c2, NETFLIX)
    assert held["outcome"] == "escalated", held
    assert held["reply"].startswith("En este momento, cada aclaración la revisa una persona")
    item = _queue_item(client, analyst, held["case_id"])
    assert item is not None
    assert (item["kind"], item["reason"], item["recommended_action"]) == (
        "escalation",
        "escalate.automation_disabled",
        "register_and_offer_block",
    )
    assert _query(schema, "SELECT customer_id FROM disputes") == [("C1",)]

    # The analyst still decides: a person's approval registers.
    approved = _decide(client, analyst, held["case_id"], decision="approve").json()
    assert approved["status"] == "approved" and approved["dispute_folio"]


def test_a_confirmation_in_flight_when_the_switch_turns_on_runs_nothing(
    client: TestClient, schema: SchemaUrls
) -> None:
    c1, analyst = customer_headers(client, "C1"), analyst_headers(client)
    pending = _pending(client, c1, NETFLIX)
    assert pending["outcome"] == "awaiting_confirmation"

    client.put("/automation", json={"all_to_human": True}, headers=analyst)
    done = _confirm(client, c1, pending)
    assert (done["outcome"], done["actions_taken"]) == ("escalated", [])
    assert _query(schema, "SELECT count(*) FROM disputes") == [(0,)]
    assert _query(schema, "SELECT status FROM case_actions") == [("canceled",)]
    item = _queue_item(client, analyst, pending["case_id"])
    assert item is not None and item["reason"] == "escalate.automation_disabled"
    assert _query(schema, "SELECT count(*) FROM audit_log WHERE action = 'audit_draw'") == [(0,)]


def test_the_block_offered_after_a_registration_is_not_run_with_the_switch_on(
    client: TestClient, schema: SchemaUrls
) -> None:
    c1, analyst = customer_headers(client, "C1"), analyst_headers(client)
    done = _registered(client, c1)
    offer = done["pending_action"]
    assert offer["action"] == "block"

    client.put("/automation", json={"all_to_human": True}, headers=analyst)
    r = client.post(
        "/chat",
        json={"message": "sí", "case_id": done["case_id"], "confirm_action_id": offer["action_id"]},
        headers=c1,
    )
    assert r.status_code == 200
    assert (r.json()["outcome"], r.json()["actions_taken"]) == ("block_not_run", [])
    assert "Bloquéala de inmediato" in r.json()["reply"]
    assert _query(schema, "SELECT product_status FROM products WHERE product_id = 'P1'") == [
        ("Active",)
    ]
    assert _query(schema, "SELECT count(*) FROM card_blocks") == [(0,)]


def test_only_the_analyst_role_can_change_the_switch_in_the_database(
    client: TestClient, schema: SchemaUrls
) -> None:
    # Row level security: every session reads the switch, a customer's changes no row.
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            assert (
                s.execute(text("SELECT all_to_human FROM automation_switch")).scalar_one() is False
            )
            changed = s.execute(text("UPDATE automation_switch SET all_to_human = true"))
            assert changed.rowcount == 0
        with db.session(role="analyst") as s:
            assert s.execute(text("UPDATE automation_switch SET all_to_human = true")).rowcount == 1
    finally:
        db.dispose()

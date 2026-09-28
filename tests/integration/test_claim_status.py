"""Status of an open claim in full customer turns against Postgres (TRZ-22): claims read from
disputes and complaints of the session customer (CA1), status, last step and cited deadline
through the fact checker (CA2), a list to choose from (CA3), an overdue deadline (CA4), nothing
written (CA5), and no deadline stated without a backing passage (CA6)."""

import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from app.adapters.db.session import Database, SchemaUrls
from app.core.config import Settings
from app.services.agent import AgentDeps, AgentResponse, handle_message
from tests.agent_support import agent_deps, fake_llm, llm_settings, reading
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

ASK = {"es": "¿Cómo va mi reclamo?", "pt": "Como vai minha reclamação?"}
PASSAGE = "15 días hábiles según el §2.1 (política de demostración, no del banco)"


@pytest.fixture
def settings(schema: SchemaUrls, database_url: str) -> Settings:
    settings = llm_settings(database_url)
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1")],
        [
            transaction(
                "TX1",
                "C1",
                "P1",
                settings.trazo_now - timedelta(hours=20),
                amount=120.0,
                currency="USD",
                merchant_name="Netflix",
            )
        ],
    )
    return settings


def _complaint(schema: SchemaUrls, complaint_id: str, created: datetime, **extra: Any) -> None:
    row = {
        "complaint_id": complaint_id,
        "customer_id": "C1",
        "creation_date": created,
        "process_date": created.date(),
        "case_type": "Claim",
        "category": "Transactions",
        "subcategory": "Cargo no reconocido",
        "reception_channel": "App",
        "has_affected_product": True,
        "priority": "Medium",
        "status": "Open",
        "sla_breached": False,
        "is_repeat_complainer": False,
        **extra,
    }
    engine = create_engine(schema.admin)
    with engine.begin() as conn:
        cols, params = ", ".join(row), ", ".join(f":{k}" for k in row)
        conn.execute(text(f"INSERT INTO complaints ({cols}) VALUES ({params})"), row)
    engine.dispose()


def _writes(facts: dict[str, Any]) -> str:
    """What the simulated LLM writes: only figures of the facts it is given."""
    claims = facts.get("claims") or []
    if facts["outcome"] == "identifying":
        return f"Tienes {len(claims)} reclamos abiertos."
    if claims:
        c = claims[0]
        return f"Tu reclamo {c['claim_id']} se abrió el {c['opened_on']}."
    return "No encuentro reclamos abiertos."


def _deps(settings: Settings, language: str = "es", sent: list[str] | None = None) -> AgentDeps:
    answer = reading("claim_status", "es-CO" if language == "es" else "pt-BR")
    return agent_deps(settings, fake_llm(settings, answer, sent, reply=_writes))


def _turns(schema: SchemaUrls, deps: AgentDeps, *turns: dict[str, Any]) -> list[AgentResponse]:
    """Sends the claim question, then each turn on the same case."""
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            first = handle_message(s, deps, "C1", ASK["es"])
            out = [first]
            for t in turns:
                out.append(handle_message(s, deps, "C1", "esta", case_id=first.case_id, **t))
            return out
    finally:
        db.dispose()


def _ask(schema: SchemaUrls, deps: AgentDeps, language: str = "es") -> AgentResponse:
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            return handle_message(s, deps, "C1", ASK[language])
    finally:
        db.dispose()


def _scalar(schema: SchemaUrls, sql: str, **params: Any) -> Any:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        value = conn.execute(text(sql), params).scalar_one()
    engine.dispose()
    return value


def _written(schema: SchemaUrls) -> tuple[int, int, int, int]:
    """Rows a read-only turn must not add: disputes, card blocks, pending actions, and audit rows
    of the tools that change state."""
    return (
        _scalar(schema, "SELECT count(*) FROM disputes"),
        _scalar(schema, "SELECT count(*) FROM card_blocks"),
        _scalar(schema, "SELECT count(*) FROM case_actions"),
        _scalar(
            schema,
            "SELECT count(*) FROM audit_log WHERE actor = 'tool' AND action IN "
            "('register_dispute', 'block_card', 'escalate_to_human')",
        ),
    )


def _fact_check_passed(schema: SchemaUrls, case_id: str) -> bool:
    return _scalar(
        schema,
        "SELECT (result->>'passed')::boolean FROM audit_log WHERE case_id = :c "
        "AND action = 'fact_check' ORDER BY id DESC LIMIT 1",
        c=case_id,
    )


# ---- one claim ------------------------------------------------------------------------------


def test_no_open_claim_is_said_and_nothing_is_written(
    schema: SchemaUrls, settings: Settings
) -> None:
    sent: list[str] = []
    r = _ask(schema, _deps(settings, sent=sent))

    assert (r.intent, r.outcome, r.autonomy_level) == ("claim_status", "informed", "L0")
    assert r.facts["claim"] is None and r.actions_taken == []
    assert r.reply == "No encuentro reclamos abiertos."
    # The LLM is told that no claim is open, not left to guess.
    user = json.loads(sent[-1])["messages"][1]["content"]
    assert json.loads(user.split("Facts (JSON): ", 1)[1])["claims"] == []
    assert _written(schema) == (0, 0, 0, 0)


def test_one_claim_gets_status_last_step_and_the_cited_deadline(
    schema: SchemaUrls, settings: Settings
) -> None:
    now = settings.trazo_now
    _complaint(
        schema,
        "CMP-TEST000000000000001",
        now - timedelta(days=5),
        assignment_date=now - timedelta(days=4),
        first_response_date=now + timedelta(days=1),
    )

    r = _ask(schema, _deps(settings))

    claim = r.facts["claim"]
    assert (claim["source"], claim["status"]) == ("complaints", "in_review")
    # A step after the simulated now has not happened yet.
    assert claim["last_step"] == {
        "step": "assigned",
        "on": (now - timedelta(days=4)).date().isoformat(),
    }
    assert claim["passage_id"] == "§2.1" and claim["overdue"] is False
    assert "Fuente: registro del reclamo CMP-TEST000000000000001." in r.reply
    assert "Plazo de respuesta: a más tardar el" in r.reply and PASSAGE in r.reply
    assert (r.outcome, r.llm_fallback) == ("informed", False)
    assert _fact_check_passed(schema, r.case_id) is True
    assert _written(schema) == (0, 0, 0, 0)


def test_an_overdue_deadline_is_said(schema: SchemaUrls, settings: Settings) -> None:
    _complaint(schema, "CMP-TEST000000000000002", settings.trazo_now - timedelta(days=40))

    r = _ask(schema, _deps(settings))

    assert r.facts["claim"]["overdue"] is True
    assert "El plazo de respuesta venció el" in r.reply and PASSAGE in r.reply
    assert _fact_check_passed(schema, r.case_id) is True


def test_without_a_backing_passage_no_deadline_is_stated_and_a_person_is_offered(
    schema: SchemaUrls, settings: Settings
) -> None:
    _complaint(
        schema,
        "CMP-TEST000000000000003",
        settings.trazo_now - timedelta(days=3),
        category="Fees",
        subcategory="Cobro de comisión",
    )

    r = _ask(schema, _deps(settings, "pt"), "pt")

    claim = r.facts["claim"]
    assert claim["due_date"] is None and claim["due_unsupported"]
    assert "Não tenho um prazo de resposta respaldado pela política" in r.reply
    assert (
        "uma pessoa" in r.reply and "§" not in r.reply and "prazo de resposta: até" not in r.reply
    )
    assert _fact_check_passed(schema, r.case_id) is True


@pytest.mark.parametrize(
    "extra",
    [
        {"status": "Rejected"},
        {"resolution_date": datetime(2026, 6, 10)},
        {"closing_date": datetime(2026, 6, 11)},
        {"creation_date": datetime(2026, 6, 18, 10), "process_date": datetime(2026, 6, 18)},
    ],
    ids=["rejected", "resolved", "closed", "created after now"],
)
def test_a_complaint_that_is_not_open_at_the_simulated_now_is_not_a_claim(
    schema: SchemaUrls, settings: Settings, extra: dict[str, Any]
) -> None:
    _complaint(schema, "CMP-TEST000000000000004", datetime(2026, 6, 1), **extra)

    assert _ask(schema, _deps(settings)).facts["claim"] is None


def test_a_dispute_registered_by_trazo_is_a_claim_with_its_registered_deadline(
    schema: SchemaUrls, settings: Settings
) -> None:
    dispute = reading(
        "unrecognized_charge",
        amount={"value": 120, "currency": "USD", "approximate": False, "evidence": "120 dólares"},
        merchant_hint={"value": "Netflix", "evidence": "Netflix"},
    )
    charge_deps = agent_deps(settings, fake_llm(settings, dispute))
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            shown = handle_message(s, charge_deps, "C1", "No reconozco 120 dólares en Netflix")
            pending = handle_message(
                s,
                charge_deps,
                "C1",
                "Sigo sin reconocerlo",
                case_id=shown.case_id,
                recognition="not_recognized",
            )
            registered = handle_message(
                s,
                charge_deps,
                "C1",
                "sí",
                case_id=shown.case_id,
                confirm_action_id=pending.facts["pending_action"]["action_id"],
            )
    finally:
        db.dispose()
    folio = registered.facts["dispute"]["folio"]

    r = _ask(schema, _deps(settings))

    claim = r.facts["claim"]
    assert (claim["claim_id"], claim["source"]) == (folio, "disputes")
    assert claim["due_date"] == registered.facts["dispute"]["due_date"]
    assert claim["last_step"]["step"] == "registered"
    assert f"Fuente: registro del reclamo {folio}." in r.reply and PASSAGE in r.reply
    assert _fact_check_passed(schema, r.case_id) is True


# ---- several claims -------------------------------------------------------------------------


@pytest.fixture
def two_claims(schema: SchemaUrls, settings: Settings) -> tuple[str, str]:
    newer, older = "CMP-TEST000000000000005", "CMP-TEST000000000000006"
    _complaint(schema, older, settings.trazo_now - timedelta(days=9))
    _complaint(schema, newer, settings.trazo_now - timedelta(days=2))
    return newer, older


def test_several_claims_are_listed_newest_first_and_the_chosen_one_is_reported(
    schema: SchemaUrls, settings: Settings, two_claims: tuple[str, str]
) -> None:
    newer, older = two_claims

    listed, chosen = _turns(schema, _deps(settings), {"option": older})

    assert (listed.outcome, listed.autonomy_level) == ("identifying", "L0")
    assert [c["claim_id"] for c in listed.facts["claims"]] == [newer, older]
    assert listed.reply == "Tienes 2 reclamos abiertos."
    assert _fact_check_passed(schema, listed.case_id) is True
    assert (chosen.outcome, chosen.facts["claim"]["claim_id"]) == ("informed", older)
    assert f"Fuente: registro del reclamo {older}." in chosen.reply
    assert _written(schema) == (0, 0, 0, 0)


def test_none_of_the_claims_shown_offers_a_person(
    schema: SchemaUrls, settings: Settings, two_claims: tuple[str, str]
) -> None:
    _, none = _turns(schema, _deps(settings), {"option": "none"})

    assert (none.outcome, none.facts["claim"]) == ("informed", None)
    assert none.reply.endswith("Si quieres, te comunico con una persona.")


def test_a_claim_that_was_not_shown_stops_the_case_for_security(
    schema: SchemaUrls, settings: Settings, two_claims: tuple[str, str]
) -> None:
    _, stopped = _turns(schema, _deps(settings), {"option": "CMP-NOTSHOWN0000000000"})

    assert stopped.outcome == "security_blocked"

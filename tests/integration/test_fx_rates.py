"""Exchange rates read from Postgres, and policy thresholds compared in USD (TRZ-14 CA1-CA3)."""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select, text

from app.adapters.db.models import AuditRecord
from app.adapters.db.rates import rates_near
from app.adapters.db.session import Database, SchemaUrls
from app.domain.fx import convert
from app.services.agent import handle_message
from tests.agent_support import agent_deps, fake_llm, llm_settings, reading
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

COP_PER_USD = 1000.0


def _insert_rates(admin_url: str, rows: list[tuple[date, str, str, float]]) -> None:
    owner = create_engine(admin_url)
    with owner.begin() as conn:
        for d, s, t, r in rows:
            conn.execute(
                text(
                    "INSERT INTO exchange_rates (date, source_currency, target_currency, "
                    "exchange_rate) VALUES (:d, :s, :t, :r)"
                ),
                {"d": d, "s": s, "t": t, "r": r},
            )
    owner.dispose()


def test_rates_near_reads_the_allowed_days_under_the_customer_context(
    schema: SchemaUrls,
) -> None:
    on = date(2026, 6, 10)
    _insert_rates(
        schema.admin,
        [
            (on - timedelta(days=2), "USD", "COP", 4100.0),
            (on - timedelta(days=5), "USD", "COP", 3900.0),
            (on + timedelta(days=1), "USD", "COP", 4200.0),
        ],
    )
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            rates = rates_near(s, on, {("USD", "COP"), ("COP", "COP")})
    finally:
        db.dispose()

    assert rates == {(on - timedelta(days=2), "USD", "COP"): 4100.0}
    c = convert(10.0, "USD", "COP", on, rates)
    assert c is not None and c.amount == 41000.0


def _turn(schema: SchemaUrls, database_url: str, rates: list[tuple[date, str, str, float]]):
    settings = llm_settings(database_url)
    at = settings.trazo_now - timedelta(days=1)
    # 800,000 COP is 800 USD at the fixture rate: between the 500 and 1000 USD bands, so an
    # analyst approves. Compared in pesos it would cross 1000 and escalate.
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1")],
        [transaction("TX1", "C1", "P1", at, amount=800_000.0)],
    )
    _insert_rates(schema.admin, [(at.date(), *r[1:]) for r in rates])
    answer = reading(
        "unrecognized_charge",
        amount={"value": 800000, "currency": "COP", "approximate": False, "evidence": "800000"},
        merchant_hint={"value": "Exito", "evidence": "en Exito"},
    )
    deps = agent_deps(settings, fake_llm(settings, answer))
    db = Database(schema.app)
    try:
        with db.session(customer_id="C1") as s:
            result = handle_message(s, deps, "C1", "No reconozco el cargo de 800000 en Exito")
    finally:
        db.dispose()
    owner = Database(schema.admin)
    try:
        with owner.session() as s:
            policy_row = s.execute(
                select(AuditRecord).where(
                    AuditRecord.case_id == result.case_id, AuditRecord.action == "decide"
                )
            ).scalar_one()
    finally:
        owner.dispose()
    return result, policy_row


def test_policy_compares_the_usd_amount_of_the_transaction_date(
    schema: SchemaUrls, database_url: str
) -> None:
    result, policy_row = _turn(schema, database_url, [(date.min, "COP", "USD", 1 / COP_PER_USD)])

    assert policy_row.payload["amount_usd"] == 800.0
    assert policy_row.result["rule"] == "approval.amount_above_auto_register"
    tx = result.facts["transaction"]
    assert tx["amount_usd"] == 800.0
    # A Colombian customer sees the charge in pesos only: there is no secondary figure.
    assert tx["amount_display"]["converted_amount"] is None
    assert tx["amount_display"]["convertible"]


def test_a_charge_without_rate_is_marked_not_convertible(
    schema: SchemaUrls, database_url: str
) -> None:
    result, policy_row = _turn(schema, database_url, [])

    assert policy_row.payload["amount_usd"] is None
    # Not convertible is never read as zero: a person decides.
    assert policy_row.result["rule"] == "escalate.amount_unknown"
    assert result.facts["transaction"]["amount_usd"] is None

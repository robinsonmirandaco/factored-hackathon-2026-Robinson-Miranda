"""Exchange rates read from Postgres, and policy thresholds compared in USD (TRZ-14 CA1-CA3)."""

import json
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select, text

from app.adapters.db.models import AuditRecord
from app.adapters.db.rates import rates_near
from app.adapters.db.session import Database, SchemaUrls
from app.adapters.llm import EXTRACT_SYSTEM, VALIDATE_SYSTEM, LLMClient
from app.core.config import Settings
from app.domain.clock import SimulatedClock
from app.domain.fx import convert
from app.domain.policy import PolicyEngine
from app.services.agent import AgentDeps, handle_message
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


def _fake_llm(settings: Settings, amount: float) -> LLMClient:
    def handler(request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content)["messages"][0]["content"]
        if system.startswith(EXTRACT_SYSTEM[:40]):
            content = json.dumps(
                {
                    "intent": "unrecognized_charge",
                    "amount": amount,
                    "merchant": "Exito",
                    "language": "es",
                    "customer_claims_legitimate": False,
                    "confidence": 0.9,
                }
            )
        elif system.startswith(VALIDATE_SYSTEM[:40]):
            content = '{"ok": true, "reason": ""}'
        else:
            content = "Revisaremos el cargo."
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    http = httpx.Client(base_url=settings.llm_base_url, transport=httpx.MockTransport(handler))
    return LLMClient(settings, http_client=http)


def _turn(schema: SchemaUrls, database_url: str, rates: list[tuple[date, str, str, float]]):
    settings = Settings(
        database_url=database_url,
        llm_enabled=True,
        llm_provider="local",
        llm_base_url="http://llm.test/v1",
        llm_model_primary="test-model",
        anthropic_api_key="",
    )
    at = settings.trazo_now - timedelta(days=1)
    # 800,000 COP is 800 USD at the fixture rate: above l1_max, below l2_max. Compared in
    # pesos it would cross l2_max and escalate.
    load(
        schema.admin,
        [customer("C1")],
        [card("P1", "C1")],
        [transaction("TX1", "C1", "P1", at, amount=800_000.0)],
    )
    _insert_rates(schema.admin, [(at.date(), *r[1:]) for r in rates])
    deps = AgentDeps(
        policy=PolicyEngine.from_file(settings.policy_path),
        llm=_fake_llm(settings, 800_000.0),
        clock=SimulatedClock(settings.trazo_now),
    )
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

    assert policy_row.payload["amount"] == 800.0
    assert policy_row.result["rule"] != "over_l2_amount"
    tx = result.facts["transaction"]
    assert tx["amount_usd"] == 800.0
    # A Colombian customer sees the charge in pesos only: there is no secondary figure.
    assert tx["amount_display"]["converted_amount"] is None
    assert tx["amount_display"]["convertible"]


def test_a_charge_without_rate_is_marked_not_convertible(
    schema: SchemaUrls, database_url: str
) -> None:
    result, policy_row = _turn(schema, database_url, [])

    assert policy_row.payload["amount"] is None
    assert result.facts["transaction"]["amount_usd"] is None

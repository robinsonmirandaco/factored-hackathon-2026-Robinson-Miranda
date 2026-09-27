"""Made-up generator context for the case tests (TRZ-42): no dataset, no DuckDB, no LLM."""

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from app.adapters.llm import LLMCallStats
from pipeline.cases.labels import LabelRules
from pipeline.cases.noise import NoiseConfig
from pipeline.cases.sampling import Claim, Context, Txn, assign_buckets

ROOT = Path(__file__).resolve().parent.parent
CONFIG: dict[str, Any] = yaml.safe_load((ROOT / "config/cases.yaml").read_text("utf-8"))
LOCAL = {"AR": "ARS", "CO": "COP", "MX": "MXN"}
MERCHANTS = {
    "Food": ["Mercado Lunar", "Súper Cometa"],
    "Health": ["Farmacia Norte", "Clínica Faro"],
    "Transport": ["Taxi Veloz", "Rueda Ya"],
    "Services": ["Cable Sol", "Red Nube"],
}
ALL_MERCHANTS = sorted(m for ms in MERCHANTS.values() for m in ms)
# A merchant nobody in the fixture buys from, so every customer has one for "no match".
UNUSED_MERCHANT = "Teatro Brisa"
SMALL_MIX = {
    "normal": 2,
    "fraud_card_lost": 1,
    "billing_amount": 1,
    "duplicate_pending": 1,
    "ambiguous": 1,
    "high_amount": 1,
    "no_match": 1,
    "out_of_scope": 1,
    "injection": 1,
    "other_customer": 1,
    "claim_status": 1,
}


def _txns(cid: str, country: str, segment: str, rng: random.Random) -> list[Txn]:
    out = []
    day = date(2023, 7, 1)
    n = 0
    while day < date(2026, 6, 10):
        n += 1
        category = rng.choice(sorted(MERCHANTS))
        big = rng.random()
        amount = round(rng.uniform(1200, 3000) if big < 0.1 else rng.uniform(10, 450), 2)
        if 0.1 <= big < 0.18:
            amount = round(rng.uniform(550, 950), 2)
        ts = datetime(day.year, day.month, day.day, rng.randint(8, 20), rng.randint(0, 59))
        out.append(
            Txn(
                transaction_id=f"TX-{cid}-{n:04d}",
                customer_id=cid,
                product_id=f"PR-{cid}",
                ts=ts,
                day=day,
                country_code=country,
                segment=segment,
                product_type="credit_card",
                transaction_type="Purchase",
                status="Approved",
                channel=rng.choice(["POS", "Web", "App"]),
                amount=amount,
                currency="USD",
                amount_usd=None,
                merchant_name=rng.choice(MERCHANTS[category]),
                merchant_category=category,
                transaction_country=country,
                city="Ciudad",
            )
        )
        day += timedelta(days=rng.randint(3, 8))
    return out


def make_context(seed: int = 42, customers: int = 90) -> Context:
    """Builds a context with customers in three countries and two segments.

    Every customer buys in USD with a credit card every 3 to 8 days from July 2023 to June 2026,
    some charges are above 500 or 1000 USD, and one customer in five has an open dispute
    complaint in each split's period.

    Args:
        seed: Seed of the run.
        customers: Number of customers.

    Returns:
        The context.
    """
    rng = random.Random(7)
    ids = {
        f"C{i:03d}": (["AR", "CO", "MX"][i % 3], ["Basic", "Plus"][i % 2]) for i in range(customers)
    }
    by_customer = {cid: _txns(cid, c, s, rng) for cid, (c, s) in ids.items()}
    claims: dict[str, list[Claim]] = {}
    for i, cid in enumerate(sorted(ids)):
        if i % 5:
            continue
        claims[cid] = [
            Claim(
                complaint_id=f"CMP-{cid}-{k}",
                customer_id=cid,
                subcategory="Cargo no reconocido",
                created=datetime(*start, 10, 0),
                first_response=None,
                resolved=None,
                status="Open",
            )
            for k, start in enumerate([(2024, 3, 10), (2025, 9, 10), (2026, 3, 10)])
        ]
    fx = {}
    day = date(2023, 6, 1)
    while day <= date(2026, 6, 30):
        for cur, rate in (("MXN", 17.0), ("COP", 4000.0), ("ARS", 900.0)):
            fx[(day, "USD", cur)] = rate
            fx[(day, cur, "USD")] = 1 / rate
        day += timedelta(days=1)
    periods = {
        s: (CONFIG["periods"][s]["start"], CONFIG["periods"][s]["end"])
        for s in ("dev", "calibration", "test")
    }
    return Context(
        seed=seed,
        rules=LabelRules.from_config(CONFIG),
        noise=NoiseConfig.from_config(CONFIG),
        presence={
            (True, True): 0.22,
            (True, False): 0.11,
            (False, True): 0.44,
            (False, False): 0.23,
        },
        periods=periods,
        buckets=assign_buckets(ids, CONFIG["customer_shares"], seed),
        customers=ids,
        by_customer=by_customer,
        claims=claims,
        fx=fx,
        local_currency=LOCAL,
        merchants=sorted([*ALL_MERCHANTS, UNUSED_MERCHANT]),
    )


class EchoLLM:
    """Fake LLM that returns each draft unchanged and records every prompt it gets."""

    def __init__(self) -> None:
        """Starts with no recorded prompts."""
        self.prompts: list[tuple[str, str]] = []

    def __call__(
        self, system: str, user: str, max_tokens: int, temperature: float
    ) -> tuple[str, LLMCallStats]:
        """Answers with the drafts as the paraphrases.

        Args:
            system: System prompt.
            user: PromptFacts as JSON.
            max_tokens: Output cap.
            temperature: Sampling temperature.

        Returns:
            The JSON answer and made-up usage.
        """
        self.prompts.append((system, user))
        facts = json.loads(user)
        answer = {v: d["draft"] for v, d in facts["variants"].items()}
        return json.dumps(answer, ensure_ascii=False), LLMCallStats(900, 300)

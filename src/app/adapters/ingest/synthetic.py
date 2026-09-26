"""Synthetic adapter. Generates coherent customers, transactions and interactions.

Includes deliberate dirty rows (negative amounts, unknown customers, bad currencies) so the
validator and quarantine path are exercised on every run. Deterministic per seed.
"""

import random
from datetime import datetime, timedelta
from typing import Any

from app.core.time import utcnow

MERCHANTS = [
    ("Amazon", "electronics", "online"),
    ("Walmart", "grocery", "pos"),
    ("Shell", "fuel", "pos"),
    ("Uber Eats", "restaurant", "online"),
    ("Netflix", "subscription", "online"),
    ("Best Buy", "electronics", "pos"),
    ("Delta Airlines", "travel", "online"),
    ("Steam", "electronics", "online"),
    ("Bet365", "gambling", "online"),
    ("Coinbase", "crypto", "online"),
    ("Zara", "other", "pos"),
    ("Apple", "electronics", "online"),
]
COUNTRIES = ["US", "US", "US", "US", "CO", "MX", "BR", "GB", "NG", "RU"]

TEMPLATES = {
    "blocked_purchase": [
        "My purchase of ${amount} at {merchant} was declined but it was me, please unblock it",
        "Me bloquearon una compra de {amount} dolares en {merchant} y si fui yo",
        "Why was my card blocked? I tried to pay {amount} at {merchant} an hour ago",
    ],
    "unrecognized_charge": [
        "There is a charge of ${amount} from {merchant} that I never made",
        "No reconozco un cargo de {amount} de {merchant}, yo no compre eso",
        "Someone used my card at {merchant} for ${amount}, I want it disputed",
    ],
    "duplicate_charge": [
        "I was charged twice by {merchant}, ${amount} each time",
        "{merchant} me cobro dos veces {amount}",
    ],
    "lost_or_stolen_card": [
        "I lost my card yesterday, please block it",
        "Me robaron la tarjeta, necesito bloquearla ya",
    ],
    "general_inquiry": [
        "What is my current balance?",
        "How do I change my mailing address?",
        "Cual es la tarifa por retiro en cajero internacional?",
    ],
}


def generate(
    seed: int = 42, n_customers: int = 200, tx_per_customer: int = 25, dirty: bool = True
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Generates customers, transactions and interactions.

    Args:
        seed: Random seed; the same seed always yields the same rows.
        n_customers: Number of customers.
        tx_per_customer: Transactions per customer.
        dirty: Whether to append deliberately invalid rows.

    Returns:
        Raw customer, transaction and interaction rows, not yet validated.
    """
    rng = random.Random(seed)
    now = utcnow().replace(microsecond=0)
    customers: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []
    interactions: list[dict[str, Any]] = []

    for i in range(n_customers):
        cid = f"C{i:05d}"
        home = rng.choice(["US", "US", "US", "CO", "MX", "BR"])
        avg = round(rng.lognormvariate(6.5, 0.6), 2)  # ~ 665 median
        customers.append(
            {
                "id": cid,
                "segment": rng.choice(["retail", "retail", "premium", "student"]),
                "country": home,
                "tenure_months": rng.randint(1, 180),
                "avg_monthly_spend": avg,
                "risk_tier": rng.choices(["standard", "elevated", "restricted"], [0.9, 0.08, 0.02])[
                    0
                ],
                "card_status": "active",
            }
        )
        for j in range(tx_per_customer):
            merchant, cat, channel = rng.choice(MERCHANTS)
            is_fraud = rng.random() < 0.02
            ts = now - timedelta(days=rng.uniform(0, 30), hours=rng.uniform(0, 24))
            if is_fraud:
                amount = round(avg * rng.uniform(0.8, 4.0), 2)
                country = rng.choice(["NG", "RU", "GB", "MX"])
                ts = ts.replace(hour=rng.choice([1, 2, 3, 4, 23]))
                cat = rng.choice(["electronics", "gambling", "crypto", "travel"])
                channel = "online"
            else:
                amount = round(max(3.0, rng.gauss(avg / 10, avg / 15)), 2)
                country = home if rng.random() < 0.95 else rng.choice(COUNTRIES)
            blocked = is_fraud and rng.random() < 0.7 or (not is_fraud and rng.random() < 0.04)
            transactions.append(
                {
                    "id": f"T{i:05d}{j:03d}",
                    "customer_id": cid,
                    "amount": amount,
                    "currency": "USD",
                    "merchant": merchant,
                    "merchant_category": cat,
                    "country": country,
                    "channel": channel,
                    "timestamp": ts,
                    "status": "blocked" if blocked else "approved",
                    "is_fraud": is_fraud,
                    "source": "synthetic",
                }
            )

    # Interactions: pick a transaction per template so the text refers to something real.
    k = 0
    for c in customers[: n_customers // 2]:
        intent = rng.choice(list(TEMPLATES))
        tpl = rng.choice(TEMPLATES[intent])
        tx = rng.choice([t for t in transactions if t["customer_id"] == c["id"]])
        text = tpl.format(amount=int(tx["amount"]), merchant=tx["merchant"])
        if rng.random() < 0.1:
            text += " my card is 4111 1111 1111 1111 and email john@example.com"  # PII to redact
        interactions.append(
            {
                "id": f"I{k:06d}",
                "customer_id": c["id"],
                "channel": "chat",
                "text": text,
                "product": "card",
                "issue_type": intent,
                "timestamp": now - timedelta(days=rng.uniform(0, 20)),
                "outcome": None,
                "source": "synthetic",
            }
        )
        k += 1

    if dirty:
        transactions += [
            {
                "id": "TBAD001",
                "customer_id": "C00001",
                "amount": -50.0,
                "merchant": "X",
                "timestamp": now,
                "source": "synthetic",
            },
            {
                "id": "TBAD002",
                "customer_id": "GHOST",
                "amount": 20.0,
                "merchant": "X",
                "timestamp": now,
                "source": "synthetic",
            },
            {
                "id": "TBAD003",
                "customer_id": "C00002",
                "amount": 20.0,
                "merchant": "X",
                "currency": "XXX",
                "timestamp": now,
                "source": "synthetic",
            },
            {
                "id": "TBAD004",
                "customer_id": "C00003",
                "amount": 20.0,
                "merchant": "X",
                "timestamp": datetime(1990, 1, 1),
                "source": "synthetic",
            },
        ]
        interactions += [
            {"id": "IBAD001", "customer_id": "C00001", "text": "", "timestamp": now},
        ]

    return customers, transactions, interactions

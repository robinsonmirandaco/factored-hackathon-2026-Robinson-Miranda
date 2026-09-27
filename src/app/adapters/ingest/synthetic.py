"""Synthetic adapter, the CI fixture. Generates customers, products and transactions shaped like
the dataset (same columns and domains), since CI has no access to the real data.

Every value is made up. Includes deliberate dirty rows (negative amount, unknown customer, bad
currency, implausible date, a product of another customer) so the validator and quarantine path
are exercised on every run. Timestamps count back from the simulated TRAZO_NOW passed in, never
the wall clock, so the same seed and anchor always yield the same rows and the data sits inside
the windows the agent searches.
"""

import random
from datetime import datetime, timedelta
from typing import Any

# Home country, ISO code, time zone, currency and coherent document types.
COUNTRIES = [
    ("Argentina", "AR", "America/Argentina/Buenos_Aires", "ARS", ("DNI",)),
    ("Colombia", "CO", "America/Bogota", "COP", ("CC", "CE")),
    # The dataset records every Mexican transaction in USD.
    ("México", "MX", "America/Mexico_City", "USD", ("Pasaporte",)),
]
SEGMENTS = ["Basic", "Basic", "Basic", "Plus", "Premium", "Student"]
MERCHANTS = [
    ("Mercado Libre", "Other", "Web"),
    ("Exito", "Food", "POS"),
    ("Oxxo", "Food", "POS"),
    ("Rappi", "Food", "App"),
    ("Netflix", "Entertainment", "Web"),
    ("Cinepolis", "Entertainment", "POS"),
    ("Avianca", "Transport", "Web"),
    ("Uber", "Transport", "App"),
    ("Farmacias Guadalajara", "Health", "POS"),
    ("Claro", "Services", "Web"),
]
Rows = list[dict[str, Any]]


def generate(
    now: datetime,
    seed: int = 42,
    n_customers: int = 200,
    tx_per_customer: int = 25,
    dirty: bool = True,
) -> tuple[Rows, Rows, Rows]:
    """Generates customers, products and transactions.

    Args:
        now: Simulated "now" (TRAZO_NOW); every timestamp falls before it.
        seed: Random seed; with the same `now`, the same seed always yields the same rows.
        n_customers: Number of customers.
        tx_per_customer: Transactions per customer.
        dirty: Whether to append deliberately invalid rows.

    Returns:
        Raw customer, product and transaction rows, not yet validated.
    """
    rng = random.Random(seed)
    now = now.replace(microsecond=0)
    customers: Rows = []
    products: Rows = []
    transactions: Rows = []

    for i in range(n_customers):
        cid = f"C{i:05d}"
        country, code, zone, currency, documents = rng.choice(COUNTRIES)
        customers.append(
            {
                "customer_id": cid,
                "document_type": rng.choice(documents),
                "document_number": f"SYN{i:07d}",
                "first_name": f"Cliente{i}",
                "country": country,
                "country_code": code,
                "timezone": zone,
                "segment": rng.choice(SEGMENTS),
                "customer_status": "Active" if rng.random() < 0.9 else "Suspended",
            }
        )
        cards = [f"P{i:05d}D", f"P{i:05d}C"]
        products += [
            {
                "product_id": cards[0],
                "customer_id": cid,
                "product_type": "debit_card",
                "product_number_last4": f"{rng.randint(0, 9999):04d}",
                "currency": currency,
                "current_balance": round(rng.uniform(0, 5000), 2),
                "product_status": "Active",
            },
            {
                "product_id": cards[1],
                "customer_id": cid,
                "product_type": "credit_card",
                "product_number_last4": f"{rng.randint(0, 9999):04d}",
                "currency": currency,
                "current_balance": round(rng.uniform(0, 2000), 2),
                "credit_limit": float(rng.choice([1000, 2500, 5000])),
                "product_status": "Active",
            },
        ]
        for j in range(tx_per_customer):
            merchant, category, channel = rng.choice(MERCHANTS)
            ts = now - timedelta(days=rng.uniform(0, 30), hours=rng.uniform(0, 24))
            transactions.append(
                {
                    "transaction_id": f"T{i:05d}{j:03d}",
                    "customer_id": cid,
                    "product_id": rng.choice(cards),
                    "transaction_date": ts,
                    "process_date": ts.date(),
                    "transaction_type": "Purchase",
                    "amount": round(max(3.0, rng.lognormvariate(3.5, 0.9)), 2),
                    "currency": currency,
                    "channel": channel,
                    "merchant_name": merchant,
                    "merchant_category": category,
                    "transaction_country": code,
                    "transaction_status": "Declined" if rng.random() < 0.04 else "Approved",
                }
            )

    if dirty:
        good = transactions[0]
        transactions += [
            {**good, "transaction_id": "TBAD001", "amount": -50.0},
            {**good, "transaction_id": "TBAD002", "customer_id": "GHOST"},
            {**good, "transaction_id": "TBAD003", "currency": "XXX"},
            {**good, "transaction_id": "TBAD004", "transaction_date": datetime(1990, 1, 1)},
            # A product that belongs to another customer.
            {**good, "transaction_id": "TBAD005", "product_id": products[-1]["product_id"]},
        ]

    return customers, products, transactions

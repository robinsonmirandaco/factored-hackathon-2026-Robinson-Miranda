"""ULB Credit Card Fraud adapter (Kaggle mlg-ulb/creditcardfraud).

Columns: Time, V1..V28 (PCA, anonymized), Amount, Class. There is no merchant, country or
customer id, so this adapter synthesizes those consistently from the row index and keeps the
real Amount, relative Time and Class label. Use it to exercise the training pipeline on a real
imbalanced label, not as a source of realistic merchants.
"""

import csv
import random
from datetime import timedelta
from typing import Any

from app.core.time import utcnow


def load(
    path: str, limit: int | None = 50000, n_customers: int = 500, seed: int = 7
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Reads the ULB CSV into canonical rows.

    Args:
        path: Path to creditcard.csv.
        limit: Maximum rows to read, or None for all.
        n_customers: Number of synthetic customers the rows are spread over.
        seed: Seed for the synthesized fields.

    Returns:
        Raw customer and transaction rows, and an empty interaction list.
    """
    rng = random.Random(seed)
    base = utcnow() - timedelta(days=30)
    customers = [
        {
            "id": f"ULB{i:05d}",
            "segment": "retail",
            "country": "EU"[:2],
            "tenure_months": rng.randint(1, 120),
            "avg_monthly_spend": round(rng.uniform(300, 3000), 2),
        }
        for i in range(n_customers)
    ]
    transactions: list[dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if limit and i >= limit:
                break
            cid = f"ULB{i % n_customers:05d}"
            is_fraud = row["Class"].strip() == "1"
            amount = float(row["Amount"])
            if amount <= 0:
                amount = 0.01  # ULB has zero-amount rows; validator would reject them
            transactions.append(
                {
                    "id": f"TULB{i:07d}",
                    "customer_id": cid,
                    "amount": round(amount, 2),
                    "currency": "EUR",
                    "merchant": "unknown",
                    "merchant_category": rng.choice(["other", "electronics", "grocery", "travel"]),
                    "country": "GB" if is_fraud and rng.random() < 0.5 else "EU"[:2],
                    "channel": "online",
                    "timestamp": base + timedelta(seconds=float(row["Time"])),
                    "status": "approved",
                    "is_fraud": is_fraud,
                    "source": "ulb",
                }
            )
    return customers, transactions, []

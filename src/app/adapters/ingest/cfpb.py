"""CFPB Consumer Complaint Database adapter.

Source: https://www.consumerfinance.gov/data-research/consumer-complaints/
Download the CSV (complaints.csv). Only rows with a narrative are useful.
Columns used: Date received, Product, Issue, Consumer complaint narrative, Company response
to consumer, Complaint ID.

Maps to `interactions`. Creates one synthetic PII-free customer per complaint so foreign keys hold.
"""

import csv
from datetime import datetime
from typing import Any

from app.core.time import utcnow

ISSUE_MAP = {
    "unauthorized": "unrecognized_charge",
    "fraud": "unrecognized_charge",
    "not authorized": "unrecognized_charge",
    "closed account": "general_inquiry",
    "fees": "general_inquiry",
    "duplicate": "duplicate_charge",
    "billing": "duplicate_charge",
    "declined": "blocked_purchase",
    "lost": "lost_or_stolen_card",
    "stolen": "lost_or_stolen_card",
}


def _issue_type(issue: str) -> str:
    s = issue.lower()
    for k, v in ISSUE_MAP.items():
        if k in s:
            return v
    return "general_inquiry"


def load(
    path: str, limit: int | None = 5000
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Reads the CFPB CSV into canonical rows, keeping only complaints with a narrative.

    Args:
        path: Path to complaints.csv.
        limit: Maximum complaints to keep, or None for all.

    Returns:
        Raw customer rows, an empty transaction list and interaction rows.
    """
    customers: list[dict[str, Any]] = []
    interactions: list[dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        n = 0
        for row in reader:
            text = (row.get("Consumer complaint narrative") or "").strip()
            if not text:
                continue
            cid = f"CFPB{row.get('Complaint ID', n)}"
            try:
                ts = datetime.strptime(row["Date received"], "%Y-%m-%d")
            except (KeyError, ValueError):
                ts = utcnow()
            customers.append(
                {
                    "id": cid,
                    "segment": "retail",
                    "country": "US",
                    "tenure_months": 24,
                    "avg_monthly_spend": 800.0,
                }
            )
            interactions.append(
                {
                    "id": f"I{cid}",
                    "customer_id": cid,
                    "channel": "complaint",
                    "text": text,
                    "product": (row.get("Product") or "card")[:64],
                    "issue_type": _issue_type(row.get("Issue") or ""),
                    "timestamp": ts,
                    "outcome": (row.get("Company response to consumer") or None),
                    "source": "cfpb",
                }
            )
            n += 1
            if limit and n >= limit:
                break
    return customers, [], interactions

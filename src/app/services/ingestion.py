"""Validates adapter output against the canonical contracts, quarantines rejects, reports.

Nothing is dropped silently: every rejected row lands in quarantine with its reason and is
counted in the report.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.adapters.db.models import Customer, Interaction, QuarantineRecord, Transaction
from app.domain.pii import redact
from app.schemas.ingest import CustomerRow, InteractionRow, TransactionRow


@dataclass
class IngestReport:
    """Counts of one ingestion run, the Data Quality artifact.

    Attributes:
        source: Adapter name.
        accepted: Accepted rows per table.
        rejected: Rejected rows per table.
        reasons: Rejected rows per reason.
        pii_redactions: Redactions per placeholder in interaction text.
    """

    source: str
    accepted: Counter[str] = field(default_factory=Counter)
    rejected: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    pii_redactions: Counter[str] = field(default_factory=Counter)

    def to_dict(self) -> dict[str, Any]:
        """Returns the report as a JSON-serializable dict."""
        return {
            "source": self.source,
            "accepted": dict(self.accepted),
            "rejected": dict(self.rejected),
            "reject_reasons": dict(self.reasons.most_common()),
            "pii_redactions": dict(self.pii_redactions),
        }

    def write(self, out_dir: str | Path) -> Path:
        """Writes the report as ingest_<source>.json.

        Args:
            out_dir: Directory for the report; created if missing.

        Returns:
            Path of the written file.
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        p = out / f"ingest_{self.source}.json"
        p.write_text(json.dumps(self.to_dict(), indent=2))
        return p


def _reason(e: ValidationError) -> str:
    err = e.errors()[0]
    loc = ".".join(str(x) for x in err.get("loc", []))
    return f"{loc}: {err.get('msg')}"[:256]


def _quarantine(
    session: Session,
    source: str,
    table: str,
    raw: dict[str, Any],
    reason: str,
    report: IngestReport,
) -> None:
    session.add(
        QuarantineRecord(source=source, table_name=table, reason=reason, raw=_jsonable(raw))
    )
    report.rejected[table] += 1
    report.reasons[reason] += 1


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in d.items()}


def ingest_rows(
    session: Session,
    source: str,
    customers: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    interactions: list[dict[str, Any]],
) -> IngestReport:
    """Validates and loads raw rows; rejects go to quarantine with their reason.

    Interaction text is PII-redacted before it is stored.

    Args:
        session: Open database session.
        source: Adapter name, stored with every row and quarantine record.
        customers: Raw customer rows.
        transactions: Raw transaction rows.
        interactions: Raw interaction rows.

    Returns:
        The ingestion report.
    """
    report = IngestReport(source=source)
    known_customers: set[str] = set()

    for raw in customers:
        try:
            row = CustomerRow(**raw)
        except ValidationError as e:
            _quarantine(session, source, "customers", raw, _reason(e), report)
            continue
        session.merge(Customer(**row.model_dump()))
        known_customers.add(row.id)
        report.accepted["customers"] += 1
    session.flush()

    for raw in transactions:
        try:
            row = TransactionRow(**raw)
        except ValidationError as e:
            _quarantine(session, source, "transactions", raw, _reason(e), report)
            continue
        if (
            row.customer_id not in known_customers
            and session.get(Customer, row.customer_id) is None
        ):
            _quarantine(
                session, source, "transactions", raw, "customer_id: unknown customer", report
            )
            continue
        session.merge(Transaction(**row.model_dump()))
        report.accepted["transactions"] += 1
    session.flush()

    for raw in interactions:
        try:
            row = InteractionRow(**raw)
        except ValidationError as e:
            _quarantine(session, source, "interactions", raw, _reason(e), report)
            continue
        if (
            row.customer_id not in known_customers
            and session.get(Customer, row.customer_id) is None
        ):
            _quarantine(
                session, source, "interactions", raw, "customer_id: unknown customer", report
            )
            continue
        text, counts = redact(row.text)
        for k, n in counts.items():
            report.pii_redactions[k] += n
        data = row.model_dump()
        data["text"] = text
        session.merge(Interaction(**data))
        report.accepted["interactions"] += 1
    session.flush()

    return report

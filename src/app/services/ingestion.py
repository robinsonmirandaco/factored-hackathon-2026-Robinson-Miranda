"""Validates fixture rows against the row contracts, quarantines rejects, reports.

Nothing is dropped silently: every rejected row lands in quarantine with its reason and is
counted in the report. Identity document numbers are replaced by their keyed hash on the way in.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.adapters.db.models import Customer, Product, QuarantineRecord, Transaction
from app.domain.pii import document_hash
from app.schemas.ingest import CustomerRow, ProductRow, TransactionRow


@dataclass
class IngestReport:
    """Counts of one ingestion run, the Data Quality artifact.

    Attributes:
        source: Adapter name.
        accepted: Accepted rows per table.
        rejected: Rejected rows per table.
        reasons: Rejected rows per reason.
    """

    source: str
    accepted: Counter[str] = field(default_factory=Counter)
    rejected: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)

    def to_dict(self) -> dict[str, Any]:
        """Returns the report as a JSON-serializable dict."""
        return {
            "source": self.source,
            "accepted": dict(self.accepted),
            "rejected": dict(self.rejected),
            "reject_reasons": dict(self.reasons.most_common()),
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
    # A rejected customer row still carries the plain document number; it is not kept.
    kept = {k: v for k, v in raw.items() if k != "document_number"}
    session.add(
        QuarantineRecord(source=source, table_name=table, reason=reason, raw=_jsonable(kept))
    )
    report.rejected[table] += 1
    report.reasons[reason] += 1


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in d.items()}


def ingest_rows(
    session: Session,
    source: str,
    document_key: str,
    customers: list[dict[str, Any]],
    products: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
) -> IngestReport:
    """Validates and loads raw rows; rejects go to quarantine with their reason.

    Runs as the schema owner: fixtures are loaded before any customer context exists.

    Args:
        session: Open database session of the schema owner.
        source: Adapter name, stored as source_file of every row and with every rejection.
        document_key: DOCUMENT_HASH_KEY, for the hash that replaces document numbers.
        customers: Raw customer rows.
        products: Raw product rows.
        transactions: Raw transaction rows.

    Returns:
        The ingestion report.

    Raises:
        ValueError: If document_key is empty.
    """
    report = IngestReport(source=source)
    known_customers: set[str] = set()
    product_owner: dict[str, str] = {}

    for raw in customers:
        try:
            c = CustomerRow(**raw)
        except ValidationError as e:
            _quarantine(session, source, "customers", raw, _reason(e), report)
            continue
        data = c.model_dump(exclude={"document_number"})
        data["document_hash"] = document_hash(document_key, c.document_type, c.document_number)
        session.merge(Customer(**data, source_file=source))
        known_customers.add(c.customer_id)
        report.accepted["customers"] += 1
    session.flush()

    for raw in products:
        try:
            p = ProductRow(**raw)
        except ValidationError as e:
            _quarantine(session, source, "products", raw, _reason(e), report)
            continue
        if p.customer_id not in known_customers:
            _quarantine(session, source, "products", raw, "customer_id: unknown customer", report)
            continue
        session.merge(Product(**p.model_dump(), source_file=source))
        product_owner[p.product_id] = p.customer_id
        report.accepted["products"] += 1
    session.flush()

    for raw in transactions:
        try:
            t = TransactionRow(**raw)
        except ValidationError as e:
            _quarantine(session, source, "transactions", raw, _reason(e), report)
            continue
        if t.customer_id not in known_customers:
            reason = "customer_id: unknown customer"
        elif product_owner.get(t.product_id) != t.customer_id:
            reason = "product_id: not a product of the customer"
        else:
            session.merge(Transaction(**t.model_dump(), source_file=source))
            report.accepted["transactions"] += 1
            continue
        _quarantine(session, source, "transactions", raw, reason, report)
    session.flush()

    return report

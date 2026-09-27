"""Made-up serving rows for integration tests, valid against the row contracts."""

from datetime import datetime
from typing import Any

from app.adapters.db.session import Database
from app.services.ingestion import ingest_rows

KEY = "integration-test-key"


def customer(customer_id: str, **extra: Any) -> dict[str, Any]:
    """A valid customer row with made-up values."""
    return {
        "customer_id": customer_id,
        "document_type": "CC",
        "document_number": f"{customer_id}-DOC",
        "first_name": "Test",
        "country": "Colombia",
        "country_code": "CO",
        "timezone": "America/Bogota",
        "segment": "Basic",
        **extra,
    }


def card(product_id: str, customer_id: str, **extra: Any) -> dict[str, Any]:
    """A valid active credit card row."""
    return {
        "product_id": product_id,
        "customer_id": customer_id,
        "product_type": "credit_card",
        "currency": "COP",
        "product_status": "Active",
        **extra,
    }


def transaction(
    transaction_id: str, customer_id: str, product_id: str, at: datetime, **extra: Any
) -> dict[str, Any]:
    """A valid approved purchase row."""
    return {
        "transaction_id": transaction_id,
        "customer_id": customer_id,
        "product_id": product_id,
        "transaction_date": at,
        "process_date": at.date(),
        "transaction_type": "Purchase",
        "amount": 100.0,
        "currency": "COP",
        "channel": "POS",
        "merchant_name": "Exito",
        "transaction_status": "Approved",
        **extra,
    }


def load(admin_url: str, customers: list, products: list, transactions: list) -> None:
    """Loads fixture rows as the schema owner and fails if any row is rejected."""
    db = Database(admin_url)
    try:
        with db.session() as s:
            report = ingest_rows(s, "test", KEY, customers, products, transactions)
    finally:
        db.dispose()
    assert not report.rejected, report.to_dict()

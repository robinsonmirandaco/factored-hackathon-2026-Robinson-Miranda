"""The synthetic fixture is dated back from TRAZO_NOW, is reproducible and follows the contracts."""

from datetime import datetime, timedelta

from app.adapters.ingest.synthetic import generate
from app.schemas.ingest import CustomerRow, ProductRow, TransactionRow

TRAZO_NOW = datetime(2026, 6, 17, 23, 59)


def test_valid_rows_fall_in_the_31_days_before_trazo_now():
    _, _, transactions = generate(TRAZO_NOW, dirty=False)
    stamps = [r["transaction_date"] for r in transactions]
    assert max(stamps) <= TRAZO_NOW
    assert min(stamps) >= TRAZO_NOW - timedelta(days=31)


def test_same_seed_and_anchor_give_identical_rows():
    assert generate(TRAZO_NOW, seed=7) == generate(TRAZO_NOW, seed=7)


def test_anchor_moves_every_timestamp():
    _, _, before = generate(TRAZO_NOW, dirty=False)
    _, _, after = generate(TRAZO_NOW + timedelta(days=10), dirty=False)
    shifted = {
        a["transaction_date"] - b["transaction_date"] for a, b in zip(after, before, strict=True)
    }
    assert shifted == {timedelta(days=10)}


def test_clean_rows_pass_the_row_contracts():
    customers, products, transactions = generate(TRAZO_NOW, dirty=False)
    for row in customers:
        CustomerRow(**row)
    for row in products:
        ProductRow(**row)
    for row in transactions:
        TransactionRow(**row)

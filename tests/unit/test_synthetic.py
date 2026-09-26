"""The synthetic fixture is dated back from TRAZO_NOW and is reproducible."""

from datetime import datetime, timedelta

from app.adapters.ingest.synthetic import generate

TRAZO_NOW = datetime(2026, 6, 17, 23, 59)


def test_valid_rows_fall_in_the_30_days_before_trazo_now():
    _, transactions, interactions = generate(TRAZO_NOW, dirty=False)
    stamps = [r["timestamp"] for r in transactions + interactions]
    assert max(stamps) <= TRAZO_NOW
    assert min(stamps) >= TRAZO_NOW - timedelta(days=31)


def test_same_seed_and_anchor_give_identical_rows():
    assert generate(TRAZO_NOW, seed=7) == generate(TRAZO_NOW, seed=7)


def test_anchor_moves_every_timestamp():
    _, before, _ = generate(TRAZO_NOW, dirty=False)
    _, after, _ = generate(TRAZO_NOW + timedelta(days=10), dirty=False)
    # Fraud rows clamp their hour at the anchor, so compare up to that clamp.
    shifted = [a["timestamp"] - b["timestamp"] for a, b in zip(after, before, strict=True)]
    assert all(timedelta(days=9) <= d <= timedelta(days=11) for d in shifted)

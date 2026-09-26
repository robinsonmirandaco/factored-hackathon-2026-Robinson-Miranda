"""Candidate density counting on a small synthetic dataset laid out like the raw partitions."""

import csv
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pytest

from pipeline.density import (
    WINDOW_DAYS,
    GroupStats,
    check_totals,
    pick_reference_dates,
    run,
    to_tenths,
)

ANCHOR = date(2026, 6, 17)
PERIOD_START = ANCHOR - timedelta(days=400)
TX_HEADER = [
    "transaction_id",
    "transaction_date",
    "customer_id",
    "transaction_type",
    "amount",
    "transaction_status",
]
Tx = tuple[str, date, str, str]


def _write_dataset(root: Path, customers: list[tuple[str, str, str]], txs: list[Tx]) -> None:
    raw = root / "raw"
    raw.mkdir(parents=True)
    with (raw / "customers.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_id", "country", "segment"])
        writer.writerows(customers)
    # A deposit on the first and last day sets the data period without adding candidates.
    period = [(customers[0][0], d, "Deposit", "Approved") for d in (PERIOD_START, ANCHOR)]
    by_day: dict[date, list[Tx]] = defaultdict(list)
    for tx in [*txs, *period]:
        by_day[tx[1]].append(tx)
    for day, rows in by_day.items():
        folder = raw / "transactions" / f"year={day:%Y}" / f"month={day:%m}" / f"day={day:%d}"
        folder.mkdir(parents=True)
        with (folder / f"transactions_{day:%Y%m%d}.csv").open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(TX_HEADER)
            for i, (customer, _, tx_type, status) in enumerate(rows):
                writer.writerow(
                    [f"TRX-{day:%Y%m%d}-{i}", f"{day} 10:00:00", customer, tx_type, "10.00", status]
                )


def _run(tmp_path: Path, customers: list[tuple[str, str, str]], txs: list[Tx]) -> dict:
    _write_dataset(tmp_path, customers, txs)
    source, results = run(tmp_path, ANCHOR, seed=7)
    return {"source": source, "results": results}


def _total(out: dict) -> GroupStats:
    return out["results"][ANCHOR]["Total"][0]


def test_window_includes_both_ends_and_nothing_outside(tmp_path: Path) -> None:
    first_day = ANCHOR - timedelta(days=WINDOW_DAYS - 1)
    txs = [
        ("CLI-A", ANCHOR, "Purchase", "Approved"),
        ("CLI-A", first_day, "Purchase", "Approved"),
        ("CLI-A", first_day - timedelta(days=1), "Purchase", "Approved"),
    ]
    out = _run(tmp_path, [("CLI-A", "Colombia", "Basic")], txs)

    assert _total(out).max == 2


def test_only_disputable_types_and_statuses_count(tmp_path: Path) -> None:
    day = ANCHOR - timedelta(days=5)
    txs = [
        ("CLI-A", day, "Purchase", "Approved"),
        ("CLI-A", day, "Payment", "Pending"),
        ("CLI-A", day, "Withdrawal", "Approved"),
        ("CLI-A", day, "Transfer", "Approved"),
        ("CLI-A", day, "Deposit", "Approved"),
        ("CLI-A", day, "Adjustment", "Approved"),
        ("CLI-A", day, "Purchase", "Declined"),
        ("CLI-A", day, "Purchase", "Reversed"),
    ]
    out = _run(tmp_path, [("CLI-A", "Colombia", "Basic")], txs)

    assert _total(out).max == 3
    assert out["source"].disputable_rows == 3
    assert out["source"].rows == 10


def test_buckets_quantiles_and_customers_without_transactions(tmp_path: Path) -> None:
    candidates = {"CLI-0": 0, "CLI-1": 1, "CLI-2": 2, "CLI-3": 3, "CLI-4": 4}
    customers = [(c, "México", "Plus") for c in candidates]
    txs = [
        (c, ANCHOR - timedelta(days=i), "Purchase", "Approved")
        for c, n in candidates.items()
        for i in range(n)
    ]
    out = _run(tmp_path, customers, txs)
    total = _total(out)

    assert total.customers == 5
    assert total.bucket_counts == (1, 1, 2, 1)
    assert total.bucket_tenths == (200, 200, 400, 200)
    assert (total.p50, total.p90, total.max) == (2, 4, 4)


def test_breakdowns_cover_every_customer(tmp_path: Path) -> None:
    customers = [
        ("CLI-A", "Colombia", "Basic"),
        ("CLI-B", "México", "Basic"),
        ("CLI-C", "México", "Premium"),
    ]
    txs = [("CLI-B", ANCHOR, "Payment", "Approved")]
    out = _run(tmp_path, customers, txs)
    tables = out["results"][ANCHOR]

    assert [(r.group, r.customers) for r in tables["Country"]] == [("Colombia", 1), ("México", 2)]
    assert [(r.group, r.customers) for r in tables["Segment"]] == [("Basic", 2), ("Premium", 1)]
    assert len(out["results"]) == 6


def test_reference_dates_are_seeded_and_keep_the_window_inside_the_period() -> None:
    first, last = date(2023, 6, 17), ANCHOR
    drawn = pick_reference_dates(first, last, ANCHOR, seed=42)

    assert drawn == pick_reference_dates(first, last, ANCHOR, seed=42)
    assert drawn == sorted(drawn) and len(set(drawn)) == 5
    assert ANCHOR not in drawn
    assert all(first + timedelta(days=WINDOW_DAYS - 1) <= d <= last for d in drawn)


def test_to_tenths_always_sums_to_one_hundred_percent() -> None:
    assert to_tenths((1, 1, 1)) == (334, 333, 333)
    assert to_tenths((2, 1, 0, 0)) == (667, 333, 0, 0)
    assert sum(to_tenths((123, 4567, 89, 10))) == 1000


def test_check_totals_rejects_a_table_that_misses_customers() -> None:
    row = GroupStats("All customers", 4, 1.0, 1, 2, 2, (1, 1, 2, 0), (250, 250, 500, 0))

    with pytest.raises(ValueError, match="expected 5"):
        check_totals({ANCHOR: {"Total": [row]}}, expected_customers=5)

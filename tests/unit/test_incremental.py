"""Lineage and incremental, idempotent loading (TRZ-04)."""

import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from pipeline.contracts import PIPELINE_VERSION
from pipeline.run import RunResult
from pipeline.silver import TableResult
from pipeline.state import load_state, save_state
from tests.pipeline_data import build_dataset, partition_path, run_pipeline, write_dataset

DAYS = [date(2024, 1, 1) + timedelta(days=i) for i in range(10)]
LINEAGE = ("source_file", "partition_date", "batch_id", "ingested_at", "pipeline_version")


def _query(sql: str) -> list[tuple]:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    return con.execute(sql).fetchall()


def _rows(result: TableResult) -> tuple[str, int, int, int]:
    return (result.table, result.bronze_rows, result.silver_rows, result.quarantine_rows)


def _read(result: RunResult, table: str) -> int:
    return next(t.files_read for t in result.tables if t.table == table)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    write_dataset(tmp_path, build_dataset(DAYS))
    return tmp_path


def test_every_silver_and_gold_row_carries_lineage(data_dir: Path) -> None:
    result = run_pipeline(data_dir, reprocess_days=3)

    nulls = " + ".join(f"count(*) FILTER (WHERE {c} IS NULL)" for c in LINEAGE)
    for path in (
        "silver/transactions/*/*.parquet",
        "silver/customers/data.parquet",
        "gold/case_generator_input.parquet",
        "gold/demand_interactions.parquet",
        "gold/service_products.parquet",
    ):
        rows, missing = _query(f"SELECT count(*), {nulls} FROM read_parquet('{data_dir / path}')")[
            0
        ]
        assert rows > 0 and missing == 0, path
    got = _query(
        f"SELECT DISTINCT batch_id, pipeline_version "
        f"FROM read_parquet('{data_dir}/silver/transactions/*/*.parquet')"
    )
    assert got == [(result.lineage.batch_id, PIPELINE_VERSION)]


def test_second_run_reads_only_the_reprocessing_window(data_dir: Path) -> None:
    first = run_pipeline(data_dir, reprocess_days=3)
    second = run_pipeline(data_dir, reprocess_days=3)

    assert _read(first, "transactions") == 10
    assert _read(second, "transactions") == 3
    assert _read(second, "customers") == 0
    assert second.outputs == first.outputs
    assert second.lineage == first.lineage


@pytest.mark.parametrize(("days", "expected"), [(0, 0), (5, 5), (7, 7)])
def test_reprocessing_window_is_configurable(data_dir: Path, days: int, expected: int) -> None:
    run_pipeline(data_dir, reprocess_days=days)

    result = run_pipeline(data_dir, reprocess_days=days)

    assert _read(result, "transactions") == expected


def test_reprocessing_a_partition_does_not_duplicate_and_gives_the_same_result(
    data_dir: Path,
) -> None:
    first = run_pipeline(data_dir, reprocess_days=0)
    path = "raw/transactions/year=2024/month=01/day=04/transactions_20240104.csv"
    state = load_state(data_dir)
    del state[path]
    save_state(data_dir, state)

    second = run_pipeline(data_dir, reprocess_days=0)

    assert _read(second, "transactions") == 1
    assert second.outputs == first.outputs
    assert [_rows(t) for t in second.tables] == [_rows(t) for t in first.tables]
    ids = _query(
        f"SELECT count(*), count(DISTINCT transaction_id) "
        f"FROM read_parquet('{data_dir}/silver/transactions/*/*.parquet')"
    )
    assert ids == [(30, 30)]


def test_changed_partition_outside_the_window_is_read_again(data_dir: Path) -> None:
    run_pipeline(data_dir, reprocess_days=0)
    rows = build_dataset(DAYS)
    for row in rows["transactions"]:
        if row["_partition"] == "2024-01-02":
            row["amount"] = "99.99"
    write_dataset(data_dir, {"transactions": rows["transactions"]})

    result = run_pipeline(data_dir, reprocess_days=0)

    assert _read(result, "transactions") == 1
    got = _query(
        f"SELECT DISTINCT amount::VARCHAR "
        f"FROM read_parquet('{data_dir}/silver/transactions/partition_date=2024-01-02/*.parquet')"
    )
    assert got == [("99.99",)]


def test_snapshot_change_reads_every_dependent_partition_again(data_dir: Path) -> None:
    run_pipeline(data_dir, reprocess_days=0)
    rows = build_dataset(DAYS)
    rows["branches"][1]["branch_name"] = "Renamed"
    write_dataset(data_dir, {"branches": rows["branches"]})

    result = run_pipeline(data_dir, reprocess_days=0)

    assert _read(result, "branches") == 1
    assert _read(result, "transactions") == 10
    assert _read(result, "call_transcripts") == 10


def test_removed_bronze_file_drops_its_partition(data_dir: Path) -> None:
    run_pipeline(data_dir, reprocess_days=0)
    partition_path(data_dir / "raw", "transactions", DAYS[4]).unlink()

    result = run_pipeline(data_dir, reprocess_days=0)

    silver = data_dir / "silver" / "transactions"
    assert not (silver / "partition_date=2024-01-05").exists()
    transactions = next(t for t in result.tables if t.table == "transactions")
    assert (transactions.files, transactions.bronze_rows, transactions.silver_rows) == (9, 27, 27)
    assert "raw/transactions/year=2024/month=01/day=05/transactions_20240105.csv" not in (
        load_state(data_dir)
    )


def test_key_already_in_silver_is_quarantined_in_a_new_partition(data_dir: Path) -> None:
    run_pipeline(data_dir, reprocess_days=0)
    late = build_dataset([date(2024, 1, 11)])
    late["transactions"][0]["transaction_id"] = "TRX-20240102-1"
    write_dataset(data_dir, {"transactions": late["transactions"]})

    result = run_pipeline(data_dir, reprocess_days=0)

    got = _query(
        f"SELECT record_key, violations[1].rule "
        f"FROM read_parquet('{data_dir}/quarantine/transactions/*/*.parquet')"
    )
    assert got == [("TRX-20240102-1", "duplicate_key")]
    transactions = next(t for t in result.tables if t.table == "transactions")
    assert (transactions.silver_rows, transactions.quarantine_rows) == (32, 1)


def test_state_records_counts_per_file(data_dir: Path) -> None:
    result = run_pipeline(data_dir, reprocess_days=0)

    raw = json.loads((data_dir / "state" / "partitions.json").read_text())
    entry = raw["raw/transactions/year=2024/month=01/day=01/transactions_20240101.csv"]
    assert entry["bronze_rows"] == entry["silver_rows"] == 3
    assert entry["batch_id"] == result.lineage.batch_id

"""Bronze, silver and gold on the small synthetic dataset (TRZ-02)."""

from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from pipeline.manifest import hash_file, load_manifest
from pipeline.run import RunResult, run
from tests.pipeline_data import build_dataset, partition_path, write_csv, write_dataset

NORMALIZATION = Path("config/normalization.yaml")
NOW = datetime(2026, 9, 26, 12, 0)
DAYS = [date(2024, 1, 10), date(2024, 1, 11)]


def _run(data_dir: Path) -> RunResult:
    return run(data_dir, NORMALIZATION, NOW)


def _query(sql: str) -> list[tuple]:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    return con.execute(sql).fetchall()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    write_dataset(tmp_path, build_dataset(DAYS))
    return tmp_path


def test_two_runs_give_identical_output_hashes(data_dir: Path) -> None:
    first = _run(data_dir).outputs
    second = _run(data_dir).outputs

    assert first == second
    assert any(p.startswith("gold/") for p in first)


def test_silver_plus_quarantine_equals_bronze_for_every_table(tmp_path: Path) -> None:
    rows = build_dataset(DAYS)
    rows["transactions"][0]["amount"] = "not-a-number"
    rows["customers"][0]["segment"] = ""
    write_dataset(tmp_path, rows)

    result = _run(tmp_path)

    for table in result.tables:
        assert table.silver_rows + table.quarantine_rows == table.bronze_rows, table.table
    by_table = {t.table: t for t in result.tables}
    assert by_table["transactions"].quarantine_rows == 1
    assert by_table["customers"].quarantine_rows == 1


def test_quarantine_row_records_rule_value_file_and_partition(tmp_path: Path) -> None:
    rows = build_dataset(DAYS)
    rows["transactions"][0]["amount"] = "not-a-number"
    write_dataset(tmp_path, rows)

    _run(tmp_path)

    got = _query(
        f"""
        SELECT table_name, record_key, source_file, partition_date::VARCHAR,
               violations[1].rule, violations[1].column_name, violations[1].value
        FROM read_parquet('{tmp_path}/quarantine/transactions/*/*.parquet')
        """
    )
    assert got == [
        (
            "transactions",
            rows["transactions"][0]["transaction_id"],
            "raw/transactions/year=2024/month=01/day=10/transactions_20240110.csv",
            "2024-01-10",
            "invalid_type",
            "amount",
            "not-a-number",
        )
    ]


def test_missing_required_value_goes_to_quarantine(tmp_path: Path) -> None:
    rows = build_dataset(DAYS)
    rows["complaints"][0]["status"] = ""
    write_dataset(tmp_path, rows)

    _run(tmp_path)

    got = _query(
        f"SELECT violations[1].rule, violations[1].column_name "
        f"FROM read_parquet('{tmp_path}/quarantine/complaints/*/*.parquet')"
    )
    assert got == [("missing_required", "status")]


def test_timestamps_are_local_time_of_the_customer_country(data_dir: Path) -> None:
    _run(data_dir)

    got = _query(
        f"""
        SELECT customer_id, timezone, transaction_date::VARCHAR, event_date_local::VARCHAR
        FROM read_parquet('{data_dir}/silver/transactions/partition_date=2024-01-10/*.parquet')
        ORDER BY customer_id
        """
    )
    # Source value is 10:00 local; UTC is 10:00 plus the country's offset (no DST in 2024).
    assert got == [
        ("CUS-1", "America/Argentina/Buenos_Aires", "2024-01-10 13:00:00+00", "2024-01-10"),
        ("CUS-2", "America/Bogota", "2024-01-10 15:00:00+00", "2024-01-10"),
        ("CUS-3", "America/Mexico_City", "2024-01-10 16:00:00+00", "2024-01-10"),
    ]


def test_bronze_files_are_registered_and_left_unchanged(data_dir: Path) -> None:
    source = partition_path(data_dir / "raw", "complaints", DAYS[0])
    before = hash_file(source)[0]

    _run(data_dir)

    entry = load_manifest(data_dir)[
        "raw/complaints/year=2024/month=01/day=10/complaints_20240110.csv"
    ]
    assert hash_file(source)[0] == before == entry.sha256
    assert entry.table == "complaints"
    assert entry.partition_date == "2024-01-10"
    assert entry.size == source.stat().st_size
    assert entry.loaded_at == NOW.isoformat(timespec="seconds")
    assert entry.header[:2] == ["complaint_id", "creation_date"]
    assert entry.source == "local"


def test_out_of_scope_tables_are_not_processed(data_dir: Path) -> None:
    raw = data_dir / "raw"
    write_csv(partition_path(raw, "digital_events", DAYS[0]), ["event_id"], [{"event_id": "E1"}])
    write_csv(raw / "marketing_campaigns.csv", ["campaign_id"], [{"campaign_id": "C1"}])

    result = _run(data_dir)

    tables = {e.table for e in load_manifest(data_dir).values()}
    assert "digital_events" not in tables and "marketing_campaigns" not in tables
    assert not any("digital_events" in p or "marketing" in p for p in result.outputs)


def test_gold_tables_are_written(data_dir: Path) -> None:
    result = _run(data_dir)

    assert result.gold["case_generator_input"] == 6
    assert result.gold["service_customers"] == 3
    assert result.gold["demand_interactions"] == 2
    got = _query(
        "SELECT product_number_last4 "
        f"FROM read_parquet('{data_dir}/gold/service_products.parquet') ORDER BY product_id"
    )
    assert got == [("6781",), ("6782",), ("6783",)]

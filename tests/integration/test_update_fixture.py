"""Incremental load against the update fixture: a late partition and a new column (TRZ-05)."""

import shutil
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from pipeline.run import RunResult
from pipeline.silver import TableResult
from tests.pipeline_data import build_dataset, run_pipeline, write_dataset

pytestmark = pytest.mark.integration

FIXTURE = Path("tests/fixtures/update")
# 2024-01-05 is missing from the base load: the fixture delivers it late.
BASE_DAYS = [date(2024, 1, 1) + timedelta(days=i) for i in range(11) if i != 4]
WINDOW_DAYS = 3


def _table(result: RunResult, name: str) -> TableResult:
    return next(t for t in result.tables if t.table == name)


def _query(sql: str) -> list[tuple]:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    return con.execute(sql).fetchall()


def _deliver(folder: str, data_dir: Path) -> None:
    shutil.copytree(FIXTURE / folder, data_dir / "raw", dirs_exist_ok=True)


@pytest.fixture
def loaded(tmp_path: Path) -> Path:
    write_dataset(tmp_path, build_dataset(BASE_DAYS))
    run_pipeline(tmp_path, reprocess_days=WINDOW_DAYS)
    return tmp_path


def test_late_partition_is_integrated_without_duplicates(loaded: Path) -> None:
    _deliver("late_partition", loaded)

    result = run_pipeline(loaded, reprocess_days=WINDOW_DAYS)
    again = run_pipeline(loaded, reprocess_days=WINDOW_DAYS)

    transactions = _table(result, "transactions")
    # The late day is outside the window, so it is read because it is new, next to the window.
    assert transactions.files_read == 1 + WINDOW_DAYS
    assert (transactions.bronze_rows, transactions.silver_rows) == (33, 33)
    assert transactions.quarantine_rows == 0
    silver = loaded / "silver" / "transactions"
    assert _query(
        f"SELECT count(*), count(DISTINCT transaction_id) FROM read_parquet('{silver}/*/*.parquet')"
    ) == [(33, 33)]
    assert _query(
        f"SELECT count(*) FROM read_parquet('{silver}/partition_date=2024-01-05/*.parquet')"
    ) == [(3,)]
    assert again.outputs == result.outputs
    assert _table(again, "transactions").silver_rows == 33


def test_partition_with_a_new_column_goes_to_quarantine_as_schema_mismatch(loaded: Path) -> None:
    _deliver("new_column", loaded)

    result = run_pipeline(loaded, reprocess_days=WINDOW_DAYS)

    complaints = _table(result, "complaints")
    assert (complaints.bronze_rows, complaints.silver_rows, complaints.quarantine_rows) == (
        11,
        10,
        1,
    )
    assert not (loaded / "silver" / "complaints" / "partition_date=2024-01-12").exists()
    got = _query(
        f"""
        SELECT source_file, violations[1].rule, violations[1].value
        FROM read_parquet('{loaded}/quarantine/complaints/partition_date=2024-01-12/*.parquet')
        """
    )
    assert got == [
        (
            "raw/complaints/year=2024/month=01/day=12/complaints_20240112.csv",
            "schema_mismatch",
            "added: escalation_level; missing: none",
        )
    ]
    report = (loaded / "calidad.md").read_text()
    assert "schema_mismatch: escalation_level" in report

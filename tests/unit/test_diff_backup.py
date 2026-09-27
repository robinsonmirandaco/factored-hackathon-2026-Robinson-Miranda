"""`make diff-backup`: partitions and rows of the current version against the backup (TRZ-08)."""

from datetime import date
from pathlib import Path

from pipeline.diff_backup import backup_dir, compare, ranges, render
from pipeline.manifest import scan_bronze
from pipeline.settings import PipelineSettings
from tests.pipeline_data import NOW, Rows, build_dataset, write_dataset

# Transactions the synthetic dataset writes per day.
PER_DAY = len(build_dataset([date(2024, 1, 10)])["transactions"])


def _version(root: Path, days: list[date], extra_transaction_on: date | None = None) -> Path:
    rows: Rows = build_dataset(days)
    if extra_transaction_on:
        day = extra_transaction_on.isoformat()
        row = next(r for r in rows["transactions"] if r["_partition"] == day)
        rows["transactions"].append({**row, "transaction_id": "TXN-EXTRA"})
    write_dataset(root, rows)
    scan_bronze(root, NOW)
    return root


def test_compare_finds_added_missing_and_changed_partitions(tmp_path: Path) -> None:
    current = _version(
        tmp_path / "current",
        [date(2024, 1, 10), date(2024, 1, 11), date(2024, 1, 12)],
        extra_transaction_on=date(2024, 1, 11),
    )
    backup = _version(tmp_path / "backup", [date(2024, 1, 9), date(2024, 1, 10), date(2024, 1, 11)])

    diffs = {d.table: d for d in compare(current, backup)}

    tx = diffs["transactions"]
    assert tx.added == ["2024-01-12"]
    assert tx.missing == ["2024-01-09"]
    assert tx.changed == [("2024-01-11", PER_DAY + 1, PER_DAY)]
    assert diffs["complaints"].changed == []
    assert diffs["customers"].current_rows == diffs["customers"].backup_rows == {"snapshot": 3}
    assert tx.shared_ids == (2 * PER_DAY, 1, 0)
    assert diffs["customers"].shared_ids == (3, 0, 0)


def test_report_lists_each_kind_of_difference(tmp_path: Path) -> None:
    current = _version(
        tmp_path / "current", [date(2024, 1, 10), date(2024, 1, 11)], date(2024, 1, 10)
    )
    backup = _version(tmp_path / "backup", [date(2024, 1, 10)])

    report = render(compare(current, backup), "s3://b/data/", "s3://b/backup/")

    assert f"| transactions | 2 | 1 | 1 | 0 | 1 | {2 * PER_DAY + 1} | {PER_DAY} |" in report
    assert "| transactions | 2024-01-11 |" in report
    assert "## Missing partitions\n\nNone." in report
    assert f"| transactions | 2024-01-10 | {PER_DAY + 1} | {PER_DAY} | +1 |" in report
    assert "<details>" in report
    assert "same records**: in daily_exchange_rates, transactions, the" in report


def test_same_ids_in_both_versions_give_the_same_records_conclusion(tmp_path: Path) -> None:
    current = _version(tmp_path / "current", [date(2024, 1, 10)])
    backup = _version(tmp_path / "backup", [date(2024, 1, 10)])

    report = render(compare(current, backup), "s3://b/data/", "s3://b/backup/")

    assert "the partitions present in both versions hold the same ids" in report
    assert "None: every partition present in both versions has the same number" in report


def test_a_table_absent_from_the_backup_counts_every_partition_as_added(tmp_path: Path) -> None:
    current = _version(tmp_path / "current", [date(2024, 1, 10)])
    backup = _version(tmp_path / "backup", [date(2024, 1, 10)])
    for path in (backup / "raw" / "call_transcripts").rglob("*.csv"):
        path.unlink()
    scan_bronze(backup, NOW)

    diffs = {d.table: d for d in compare(current, backup)}

    assert diffs["call_transcripts"].added == ["2024-01-10"]
    assert diffs["call_transcripts"].backup_rows == {}


def test_ranges_join_consecutive_days() -> None:
    days = ["2023-06-17", "2023-06-18", "2023-06-19", "2023-07-01", "snapshot"]

    assert ranges(days) == "snapshot, 2023-06-17 to 2023-06-19 (3), 2023-07-01"


def test_backup_lives_apart_from_the_current_raw_folder(tmp_path: Path) -> None:
    settings = PipelineSettings(data_dir=tmp_path, s3_backup_prefix="data_backup_20260831/")

    target = backup_dir(settings)

    assert target == tmp_path / "backup" / "data_backup_20260831"
    assert not target.is_relative_to(tmp_path / "raw")

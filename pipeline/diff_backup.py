"""`make diff-backup`: compares the current data version with the backup in the same bucket.

The backup prefix is downloaded, read-only and incrementally, with the same extraction as
`make extract` into its own folder, `DATA_DIR/backup/<prefix>/raw`, with its own manifest, so it
never mixes with `DATA_DIR/raw`. Both versions are then compared per table: partitions added,
missing and with a different number of rows. The report is `docs/reports/diferencias_versiones.md`.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import duckdb

from app.core.logging import configure_logging, get_logger
from app.core.time import utcnow
from pipeline.contracts import CONTRACTS
from pipeline.extract import extract, make_client
from pipeline.manifest import BronzeFile, load_manifest
from pipeline.report import _n, _pct, _table
from pipeline.settings import PipelineSettings
from pipeline.silver import q, sql_str

log = get_logger("pipeline.diff_backup")

SNAPSHOT = "snapshot"


@dataclass(frozen=True)
class TableDiff:
    """Differences of one table between the current version and the backup.

    Attributes:
        table: Table name.
        current_rows: Rows per partition in the current version; snapshots use `snapshot`.
        backup_rows: Rows per partition in the backup.
        key: Primary key columns of the table.
        shared_ids: (ids in both, ids only in current, ids only in backup) over the partitions
            present in both versions; None when no partition is in both.
    """

    table: str
    current_rows: dict[str, int]
    backup_rows: dict[str, int]
    key: tuple[str, ...] = ()
    shared_ids: tuple[int, int, int] | None = None

    @property
    def added(self) -> list[str]:
        """Partitions in the current version that the backup lacks."""
        return sorted(self.current_rows.keys() - self.backup_rows.keys())

    @property
    def missing(self) -> list[str]:
        """Partitions in the backup that the current version lacks."""
        return sorted(self.backup_rows.keys() - self.current_rows.keys())

    @property
    def changed(self) -> list[tuple[str, int, int]]:
        """(partition, current rows, backup rows) of partitions in both with different rows."""
        return [
            (p, self.current_rows[p], self.backup_rows[p])
            for p in sorted(self.current_rows.keys() & self.backup_rows.keys())
            if self.current_rows[p] != self.backup_rows[p]
        ]


def backup_dir(settings: PipelineSettings) -> Path:
    """Folder that holds the backup version, apart from `DATA_DIR/raw`.

    Args:
        settings: Pipeline settings.

    Returns:
        `DATA_DIR/backup/<backup prefix without slashes>`.
    """
    return settings.data_dir / "backup" / settings.s3_backup_prefix.strip("/")


def _read(root: Path, files: list[BronzeFile]) -> str:
    paths = "[" + ", ".join(sql_str(str((root / f.path).resolve())) for f in files) + "]"
    return (
        f"read_csv({paths}, header = true, delim = ',', quote = '\"', escape = '\"', "
        "all_varchar = true, union_by_name = true, filename = true, hive_partitioning = false)"
    )


def id_overlap(
    current_root: Path,
    current_files: list[BronzeFile],
    backup_root: Path,
    backup_files: list[BronzeFile],
    key: tuple[str, ...],
) -> tuple[int, int, int]:
    """Compares the primary key values of two sets of files. Only counts leave DuckDB.

    Args:
        current_root: Directory the current manifest paths are relative to.
        current_files: Files of the current version.
        backup_root: Directory the backup manifest paths are relative to.
        backup_files: Files of the backup.
        key: Primary key columns.

    Returns:
        (distinct ids in both, only in the current files, only in the backup files).
    """
    id_expr = "concat_ws('|', " + ", ".join(q(c) for c in key) + ")"
    row = (
        duckdb.connect()
        .execute(
            f"""
            WITH c AS (SELECT DISTINCT {id_expr} AS id FROM {_read(current_root, current_files)}),
                 b AS (SELECT DISTINCT {id_expr} AS id FROM {_read(backup_root, backup_files)})
            SELECT (SELECT count(*) FROM c SEMI JOIN b USING (id)),
                   (SELECT count(*) FROM c ANTI JOIN b USING (id)),
                   (SELECT count(*) FROM b ANTI JOIN c USING (id))
            """
        )
        .fetchone()
    )
    return (row[0], row[1], row[2]) if row else (0, 0, 0)


def count_rows(root: Path, files: list[BronzeFile]) -> dict[str, int]:
    """Counts the data rows of each CSV file, header excluded.

    Every column is read as text and headers are matched by name, so a file with a different
    header is still counted.

    Args:
        root: Directory the manifest paths are relative to.
        files: Files of one table.

    Returns:
        Rows per partition (`snapshot` for a snapshot file).
    """
    if not files:
        return {}
    by_path = {str((root / f.path).resolve()): f.partition_date or SNAPSHOT for f in files}
    rows = (
        duckdb.connect().execute(f"SELECT filename, count(*) FROM {_read(root, files)} GROUP BY 1")
    ).fetchall()
    counts = dict.fromkeys(by_path.values(), 0)
    for filename, n in rows:
        counts[by_path[str(Path(filename).resolve())]] += n
    return counts


def compare(current_root: Path, backup_root: Path) -> list[TableDiff]:
    """Compares the manifests of both versions, table by table, in contract order.

    Args:
        current_root: Data directory of the current version (manifest and `raw/`).
        backup_root: Data directory of the backup (manifest and `raw/`).

    Returns:
        One entry per in-scope table.
    """
    current, backup = load_manifest(current_root), load_manifest(backup_root)
    diffs = []
    for contract in CONTRACTS:
        cur = [f for f in current.values() if f.table == contract.table]
        bak = [f for f in backup.values() if f.table == contract.table]
        both = {f.partition_date for f in cur} & {f.partition_date for f in bak}
        shared = (
            id_overlap(
                current_root,
                [f for f in cur if f.partition_date in both],
                backup_root,
                [f for f in bak if f.partition_date in both],
                contract.primary_key,
            )
            if both
            else None
        )
        diffs.append(
            TableDiff(
                table=contract.table,
                current_rows=count_rows(current_root, cur),
                backup_rows=count_rows(backup_root, bak),
                key=contract.primary_key,
                shared_ids=shared,
            )
        )
    return diffs


def ranges(partitions: list[str]) -> str:
    """Joins consecutive days into ranges.

    Args:
        partitions: Sorted ISO days, or `snapshot`.

    Returns:
        For example `2023-06-17 to 2023-06-20 (4), 2023-07-01`; the count is the days in
        the range.
    """
    parts = [SNAPSHOT] if SNAPSHOT in partitions else []
    runs: list[list[date]] = []
    for day in sorted(date.fromisoformat(p) for p in partitions if p != SNAPSHOT):
        if runs and day == runs[-1][-1] + timedelta(days=1):
            runs[-1].append(day)
        else:
            runs.append([day])
    parts += [f"{r[0]} to {r[-1]} ({len(r)})" if len(r) > 1 else str(r[0]) for r in runs]
    return ", ".join(parts)


def _listing(rows: list[list[object]]) -> list[str]:
    return _table(["Table", "Partitions"], rows) if rows else ["None."]


def _shared(diffs: list[TableDiff]) -> list[str]:
    rows, different = [], []
    for d in diffs:
        if d.shared_ids is None:
            continue
        both, only_current, only_backup = d.shared_ids
        rows.append(
            [
                d.table,
                ", ".join(d.key),
                _n(len(d.current_rows.keys() & d.backup_rows.keys())),
                _n(both),
                _n(only_current),
                _n(only_backup),
                _pct(both, both + only_backup),
            ]
        )
        if only_current or only_backup:
            different.append(d.table)
    header = [
        "Table",
        "Key",
        "Partitions in both",
        "Ids in both",
        "Only current",
        "Only backup",
        "Backup ids also in current",
    ]
    conclusion = (
        "**Conclusion: the backup is not an earlier version of the same records**: in "
        + ", ".join(different)
        + ", the partitions present in both versions hold different ids."
        if different
        else "**Conclusion:** the partitions present in both versions hold the same ids."
    )
    return [
        *_table(header, rows),
        "",
        "Source: the CSV files of the partitions present in both versions, distinct values of the "
        "primary key of the table contract. Ids are counted, never shown.",
        "",
        conclusion,
    ]


def render(diffs: list[TableDiff], current_source: str, backup_source: str) -> str:
    """Renders the version differences report.

    Args:
        diffs: Differences per table.
        current_source: Where the current version comes from.
        backup_source: Where the backup comes from.

    Returns:
        Markdown text.
    """
    summary = [
        [
            d.table,
            _n(len(d.current_rows)),
            _n(len(d.backup_rows)),
            _n(len(d.added)),
            _n(len(d.missing)),
            _n(len(d.changed)),
            _n(sum(d.current_rows.values())),
            _n(sum(d.backup_rows.values())),
        ]
        for d in diffs
    ]
    lines = [
        "# Differences between data versions",
        "",
        "Generated by `make diff-backup` (story TRZ-08; design §9.4). Counts only: no row is "
        "shown.",
        "",
        f"- Current version: `{current_source}`, downloaded by `make extract` into `DATA_DIR/raw`.",
        f"- Backup: `{backup_source}`, downloaded read-only into `DATA_DIR/backup/`, apart "
        "from the current version.",
        "- Only the in-scope tables are compared. A partition is a daily file; a snapshot table "
        "has one partition, `snapshot`. Rows are the data rows of each CSV, header excluded.",
        "- Added: partition in the current version and not in the backup. Missing: partition in "
        "the backup and not in the current version. Different rows: partition in both with a "
        "different number of rows.",
        "",
        "## Summary",
        "",
        *_table(
            [
                "Table",
                "Partitions current",
                "Partitions backup",
                "Added",
                "Missing",
                "Different rows",
                "Rows current",
                "Rows backup",
            ],
            summary,
        ),
        "",
        "## Shared ids in the partitions of both versions",
        "",
        *_shared(diffs),
        "",
        "## Added partitions",
        "",
        *_listing([[d.table, ranges(d.added)] for d in diffs if d.added]),
        "",
        "## Missing partitions",
        "",
        *_listing([[d.table, ranges(d.missing)] for d in diffs if d.missing]),
        "",
        "## Partitions with a different number of rows",
        "",
    ]
    changed = [
        [d.table, p, _n(cur), _n(bak), f"{cur - bak:+,}"]
        for d in diffs
        for p, cur, bak in d.changed
    ]
    if not changed:
        lines.append("None: every partition present in both versions has the same number of rows.")
    else:
        per_table = ", ".join(f"{d.table} {_n(len(d.changed))}" for d in diffs if d.changed)
        lines += [
            f"{_n(len(changed))} partitions present in both versions differ in rows: {per_table}.",
            "",
            "<details>",
            f"<summary>All {_n(len(changed))} partitions</summary>",
            "",
            *_table(["Table", "Partition", "Rows current", "Rows backup", "Difference"], changed),
            "",
            "</details>",
        ]
    return "\n".join(lines) + "\n"


def main() -> None:
    """Downloads the backup if needed, compares both versions and writes the report."""
    settings = PipelineSettings()
    configure_logging(settings.log_level)
    target = backup_dir(settings)
    client = make_client(settings.aws_profile, settings.aws_region)
    stats = extract(client, settings.s3_bucket, settings.s3_backup_prefix, target, utcnow())
    log.info("backup_extracted", listed=stats.listed, downloaded=stats.downloaded)
    diffs = compare(settings.data_dir, target)
    settings.versions_report_path.parent.mkdir(parents=True, exist_ok=True)
    settings.versions_report_path.write_text(
        render(
            diffs,
            f"s3://{settings.s3_bucket}/{settings.s3_prefix}",
            f"s3://{settings.s3_bucket}/{settings.s3_backup_prefix}",
        )
    )
    log.info(
        "diff_backup_done",
        added=sum(len(d.added) for d in diffs),
        missing=sum(len(d.missing) for d in diffs),
        changed=sum(len(d.changed) for d in diffs),
    )


if __name__ == "__main__":
    main()

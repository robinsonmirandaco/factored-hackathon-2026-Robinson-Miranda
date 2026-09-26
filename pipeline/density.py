"""Candidate density test: disputable transactions per customer in the 120-day dispute window.

Reads every `transactions` partition with DuckDB and writes `docs/reports/densidad.md`. The
numbers decide the size and bias of the serving cohort and whether plan B applies (design §17).

Run with `make density`.
"""

import random
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

import duckdb

from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.core.time import utcnow

WINDOW_DAYS = 120
DISPUTABLE_TYPES = ("Purchase", "Payment", "Withdrawal")
DISPUTABLE_STATUSES = ("Approved", "Pending")
RANDOM_REFERENCE_DATES = 5
BUCKETS = ("0", "1", "2-3", ">3")
DIMENSIONS = {"Total": None, "Country": "country", "Segment": "segment"}
REPORT_PATH = Path("docs/reports/densidad.md")
# Percentages are kept as integer tenths so each row sums to exactly 100.0.
TENTHS_TOTAL = 1000

log = get_logger("pipeline.density")


@dataclass(frozen=True)
class GroupStats:
    """Candidate distribution for one group of customers at one reference date.

    Attributes:
        group: Group label, for example "Colombia" or "All customers".
        customers: Number of customers in the group, including those with zero candidates.
        mean: Mean candidates per customer.
        p50: Median candidates per customer (discrete quantile).
        p90: 90th percentile of candidates per customer (discrete quantile).
        max: Maximum candidates for a single customer.
        bucket_counts: Customers with 0, 1, 2 to 3 and more than 3 candidates.
        bucket_tenths: The same buckets as percentages in tenths of a point, summing to 1000.
    """

    group: str
    customers: int
    mean: float
    p50: int
    p90: int
    max: int
    bucket_counts: tuple[int, int, int, int]
    bucket_tenths: tuple[int, ...]


@dataclass(frozen=True)
class SourceStats:
    """What was read from the raw files.

    Attributes:
        partitions: Distinct transaction files read.
        rows: Transaction rows read.
        disputable_rows: Rows with a disputable type and status, over the whole period.
        orphan_rows: Rows whose customer_id is not in customers.csv.
        customers: Rows in customers.csv.
        first_partition: Earliest partition date.
        last_partition: Latest partition date.
    """

    partitions: int
    rows: int
    disputable_rows: int
    orphan_rows: int
    customers: int
    first_partition: date
    last_partition: date


def transactions_glob(data_dir: Path) -> str:
    """Returns the glob that matches every daily transactions partition.

    Args:
        data_dir: Root data directory that contains `raw/`.

    Returns:
        A glob over `raw/transactions/year=*/month=*/day=*/*.csv`.
    """
    return str(data_dir / "raw" / "transactions" / "year=*" / "month=*" / "day=*" / "*.csv")


def register_sources(con: duckdb.DuckDBPyConnection, data_dir: Path) -> SourceStats:
    """Registers `transactions` and `customers` views with only the columns this test needs.

    The window uses the partition date, which is the local business day, because
    `transaction_date` is stored in another time zone and runs a few hours past the partition day.

    Args:
        con: DuckDB connection.
        data_dir: Root data directory that contains `raw/`.

    Returns:
        Counts of what was read, used in the report header.
    """
    con.execute(
        f"""
        CREATE OR REPLACE VIEW transactions AS
        SELECT customer_id, transaction_type, transaction_status, filename,
               make_date(CAST(year AS INTEGER), CAST(month AS INTEGER), CAST(day AS INTEGER))
                   AS partition_date
        FROM read_csv('{transactions_glob(data_dir)}', header = true, all_varchar = true,
                      hive_partitioning = true, filename = true)
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE VIEW customers AS
        SELECT customer_id, coalesce(country, '(missing)') AS country,
               coalesce(segment, '(missing)') AS segment
        FROM read_csv('{data_dir / "raw" / "customers.csv"}', header = true, all_varchar = true)
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE disputable_daily AS
        SELECT customer_id, partition_date, count(*) AS n
        FROM transactions
        WHERE transaction_type IN ? AND transaction_status IN ?
        GROUP BY ALL
        """,
        [list(DISPUTABLE_TYPES), list(DISPUTABLE_STATUSES)],
    )
    row = con.execute(
        """
        SELECT count(DISTINCT filename), count(*),
               count(*) FILTER (WHERE customer_id NOT IN (SELECT customer_id FROM customers)),
               min(partition_date), max(partition_date)
        FROM transactions
        """
    ).fetchone()
    disputable = con.execute("SELECT coalesce(sum(n), 0) FROM disputable_daily").fetchone()
    customers = con.execute("SELECT count(*) FROM customers").fetchone()
    assert row is not None and disputable is not None and customers is not None
    return SourceStats(
        partitions=row[0],
        rows=row[1],
        disputable_rows=int(disputable[0]),
        orphan_rows=row[2],
        customers=customers[0],
        first_partition=row[3],
        last_partition=row[4],
    )


def pick_reference_dates(
    first: date, last: date, anchor: date, seed: int, n: int = RANDOM_REFERENCE_DATES
) -> list[date]:
    """Draws reference dates whose whole window falls inside the data period.

    Args:
        first: Earliest partition date.
        last: Latest partition date.
        anchor: The main reference date (TRAZO_NOW), excluded from the draw.
        seed: Seed for the draw.
        n: Number of dates to draw.

    Returns:
        The drawn dates, sorted ascending.
    """
    earliest = first + timedelta(days=WINDOW_DAYS - 1)
    candidates = [
        earliest + timedelta(days=i)
        for i in range((last - earliest).days + 1)
        if earliest + timedelta(days=i) != anchor
    ]
    return sorted(random.Random(seed).sample(candidates, n))


def count_candidates(con: duckdb.DuckDBPyConnection, reference_dates: list[date]) -> None:
    """Builds `candidate_counts`: one row per customer and reference date, zeros included.

    The window is the 120 calendar days that end on the reference date, both ends included.

    Args:
        con: DuckDB connection with the views from `register_sources`.
        reference_dates: Reference dates to evaluate.
    """
    con.execute(
        """
        CREATE OR REPLACE TABLE candidate_counts AS
        WITH refs AS (SELECT unnest(?::DATE[]) AS reference_date)
        SELECT c.customer_id, c.country, c.segment, r.reference_date,
               coalesce(sum(d.n), 0)::INTEGER AS candidates
        FROM customers c
        CROSS JOIN refs r
        LEFT JOIN disputable_daily d
          ON d.customer_id = c.customer_id
         AND d.partition_date BETWEEN r.reference_date - ? AND r.reference_date
        GROUP BY ALL
        """,
        [reference_dates, WINDOW_DAYS - 1],
    )


def to_tenths(counts: tuple[int, ...]) -> tuple[int, ...]:
    """Converts counts to percentages in tenths of a point that sum to exactly 1000.

    Uses the largest remainder method; ties go to the earlier bucket so the result is stable.

    Args:
        counts: Non-negative counts with a positive sum.

    Returns:
        One integer per count, in tenths of a percentage point.
    """
    total = sum(counts)
    scaled = [c * TENTHS_TOTAL for c in counts]
    floors = [s // total for s in scaled]
    short = TENTHS_TOTAL - sum(floors)
    order = sorted(range(len(counts)), key=lambda i: (-(scaled[i] % total), i))
    for i in order[:short]:
        floors[i] += 1
    return tuple(floors)


def summarize(
    con: duckdb.DuckDBPyConnection, reference_date: date, column: str | None
) -> list[GroupStats]:
    """Computes the candidate distribution per group for one reference date.

    Args:
        con: DuckDB connection with `candidate_counts` built.
        reference_date: Reference date to summarize.
        column: `country`, `segment`, or None for all customers together.

    Returns:
        One entry per group, sorted by group label.
    """
    group_expr = column if column else "'All customers'"
    rows = con.execute(
        f"""
        SELECT {group_expr} AS grp, count(*), avg(candidates),
               quantile_disc(candidates, 0.5), quantile_disc(candidates, 0.9), max(candidates),
               count(*) FILTER (WHERE candidates = 0),
               count(*) FILTER (WHERE candidates = 1),
               count(*) FILTER (WHERE candidates BETWEEN 2 AND 3),
               count(*) FILTER (WHERE candidates > 3)
        FROM candidate_counts
        WHERE reference_date = ?
        GROUP BY grp
        ORDER BY grp
        """,
        [reference_date],
    ).fetchall()
    stats = []
    for grp, customers, mean, p50, p90, top, *buckets in rows:
        bucket_counts = (buckets[0], buckets[1], buckets[2], buckets[3])
        stats.append(
            GroupStats(
                group=grp,
                customers=customers,
                mean=mean,
                p50=p50,
                p90=p90,
                max=top,
                bucket_counts=bucket_counts,
                bucket_tenths=to_tenths(bucket_counts),
            )
        )
    return stats


def check_totals(results: dict[date, dict[str, list[GroupStats]]], expected_customers: int) -> None:
    """Fails if any table does not add up to every customer or any row does not add up to 100%.

    Args:
        results: Summaries per reference date and dimension.
        expected_customers: Customers in customers.csv.

    Raises:
        ValueError: If a table or row does not add up.
    """
    for reference_date, tables in results.items():
        for dimension, rows in tables.items():
            total = sum(r.customers for r in rows)
            if total != expected_customers:
                raise ValueError(
                    f"{reference_date} {dimension}: {total} customers, "
                    f"expected {expected_customers}"
                )
            for r in rows:
                if sum(r.bucket_counts) != r.customers or sum(r.bucket_tenths) != TENTHS_TOTAL:
                    raise ValueError(
                        f"{reference_date} {dimension} {r.group}: buckets do not add up"
                    )


def _pct(tenths: int) -> str:
    return f"{tenths // 10}.{tenths % 10}%"


def _table(rows: list[GroupStats], label: str) -> list[str]:
    lines = [
        f"| {label} | Customers | Mean | p50 | p90 | Max | "
        + " | ".join(f"{b} candidates" for b in BUCKETS)
        + " |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        cells = [
            r.group,
            f"{r.customers:,}",
            f"{r.mean:.2f}",
            str(r.p50),
            str(r.p90),
            str(r.max),
            *(f"{_pct(t)} ({c:,})" for t, c in zip(r.bucket_tenths, r.bucket_counts, strict=True)),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def render_report(
    source: SourceStats,
    results: dict[date, dict[str, list[GroupStats]]],
    anchor: date,
    seed: int,
    run_at: str,
) -> str:
    """Renders the Markdown report.

    Args:
        source: Counts of what was read.
        results: Summaries per reference date and dimension; the anchor date comes first.
        anchor: The TRAZO_NOW date.
        seed: Seed used to draw the random reference dates.
        run_at: Run timestamp, UTC, ISO format.

    Returns:
        The report as Markdown.
    """
    random_dates = [d for d in results if d != anchor]
    lines = [
        "# Candidate density",
        "",
        "Disputable transactions per customer in the dispute window (story TRZ-01, design §17).",
        "",
        "## Run",
        "",
        f"- Run at (UTC): {run_at}",
        f"- Seed: {seed}",
        f"- TRAZO_NOW reference date: {anchor.isoformat()}",
        f"- Random reference dates (seed {seed}): "
        + ", ".join(d.isoformat() for d in random_dates),
        f"- Transaction partitions read: {source.partitions:,} "
        f"({source.first_partition.isoformat()} to {source.last_partition.isoformat()})",
        f"- Transaction rows read: {source.rows:,}",
        f"- Disputable rows over the whole period: {source.disputable_rows:,}",
        f"- Transaction rows with a customer_id not in customers.csv: {source.orphan_rows:,}",
        f"- Customers in customers.csv: {source.customers:,}",
        "",
        "## Definitions",
        "",
        f"- Disputable: `transaction_type` in {', '.join(DISPUTABLE_TYPES)} and "
        f"`transaction_status` in {', '.join(DISPUTABLE_STATUSES)}.",
        f"- Window: the {WINDOW_DAYS} calendar days that end on the reference date, both included, "
        "by partition date (the local business day). `transaction_date` runs up to a few hours "
        "past its partition day, so it is not used for the window.",
        "- Random reference dates are drawn so the whole window falls inside the data period; "
        "the TRAZO_NOW date is excluded from the draw.",
        "- Every customer in customers.csv is counted, including those with zero candidates.",
        "- p50 and p90 are discrete quantiles (an observed value). Percentages are rounded to one "
        "decimal with the largest remainder method, so each row sums to exactly 100%.",
        "",
        "## All customers by reference date",
        "",
    ]
    overall = [replace(tables["Total"][0], group=d.isoformat()) for d, tables in results.items()]
    lines += _table(overall, "Reference date")
    for reference_date, tables in results.items():
        title = "TRAZO_NOW" if reference_date == anchor else "random date"
        lines += ["", f"## {reference_date.isoformat()} ({title})", ""]
        for dimension, rows in tables.items():
            lines += _table(rows, dimension) + [""]
        lines.pop()
    return "\n".join(lines) + "\n"


def run(
    data_dir: Path, anchor: date, seed: int
) -> tuple[SourceStats, dict[date, dict[str, list[GroupStats]]]]:
    """Reads the raw data and computes every summary.

    Args:
        data_dir: Root data directory that contains `raw/`.
        anchor: The TRAZO_NOW date.
        seed: Seed for the random reference dates.

    Returns:
        Source counts and summaries per reference date (anchor first) and dimension.
    """
    con = duckdb.connect()
    source = register_sources(con, data_dir)
    reference_dates = [
        anchor,
        *pick_reference_dates(source.first_partition, source.last_partition, anchor, seed),
    ]
    count_candidates(con, reference_dates)
    results = {
        d: {name: summarize(con, d, column) for name, column in DIMENSIONS.items()}
        for d in reference_dates
    }
    check_totals(results, source.customers)
    return source, results


def main() -> None:
    """Runs the density test with the configured data directory, TRAZO_NOW and seed."""
    settings = Settings()
    configure_logging(settings.log_level)
    anchor = settings.trazo_now.date()
    log.info("density_start", data_dir=str(settings.data_dir), anchor=anchor.isoformat())
    source, results = run(settings.data_dir, anchor, settings.seed)
    run_at = utcnow().isoformat(timespec="seconds")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_report(source, results, anchor, settings.seed, run_at))
    log.info(
        "density_done",
        report=str(REPORT_PATH),
        partitions=source.partitions,
        rows=source.rows,
        customers=source.customers,
    )


if __name__ == "__main__":
    main()

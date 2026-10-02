"""Loads one data source into the serving database: the gold cohort or the synthetic fixture.

A database holds exactly one source. `seed_runs` records it, and a seed refuses to run on a
database that already has one unless it is told to replace it; replacing empties every table
first, operational ones included, so rows of two sources never meet.
"""

import hashlib
import json
from collections.abc import Collection
from pathlib import Path
from typing import Any

import duckdb
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.pii import document_hash

# Load order follows the foreign keys.
COHORT_TABLES = ("customers", "products", "transactions", "complaints", "exchange_rates")
# Every table a seed may leave rows in; replacing a seed empties them all.
SEEDED_TABLES = (
    *COHORT_TABLES,
    "cases",
    "case_queue",
    "info_requests",
    "disputes",
    "card_blocks",
    "audit_log",
    "quarantine",
    "seed_runs",
)


class SeedError(RuntimeError):
    """The database cannot take this seed."""


def current_source(session: Session) -> str | None:
    """Returns the source of the last seed, or None for an empty database.

    Args:
        session: Session of the schema owner.

    Returns:
        "cohort", "synthetic" or None.
    """
    return session.execute(
        text("SELECT source FROM seed_runs ORDER BY id DESC LIMIT 1")
    ).scalar_one_or_none()


def prepare(session: Session, replace: bool) -> None:
    """Makes sure the database can take a new seed, emptying it when asked to.

    Args:
        session: Session of the schema owner.
        replace: Empty every seeded table first.

    Raises:
        SeedError: If the database already holds a seed and replace is False.
    """
    existing = current_source(session)
    if existing and not replace:
        raise SeedError(f"database already holds the {existing} seed; pass --replace to empty it")
    if existing:
        # TRUNCATE is not stopped by the audit trigger, which guards rows, not tables.
        session.execute(text(f"TRUNCATE {', '.join(SEEDED_TABLES)} RESTART IDENTITY CASCADE"))


def record(session: Session, source: str, detail: dict[str, Any]) -> None:
    """Writes the seed_runs row of a finished seed.

    Args:
        session: Session of the schema owner.
        source: "cohort" or "synthetic".
        detail: Row counts and file hashes of the seed.
    """
    session.execute(
        text("INSERT INTO seed_runs (source, detail) VALUES (:s, CAST(:d AS jsonb))"),
        {"s": source, "d": _json(detail)},
    )


def load_cohort(
    session: Session,
    cohort_dir: Path,
    document_key: str,
    customer_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """Copies the cohort Parquet files into their tables.

    The files carry the table columns, except that customers bring the plain document number,
    which is turned into its keyed hash here and never written.

    Args:
        session: Session of the schema owner.
        cohort_dir: `DATA_DIR/gold/cohort`.
        document_key: DOCUMENT_HASH_KEY.
        customer_ids: Only the rows of these customers (the evaluation harness loads one case
            at a time); tables without a customer, such as the rates, are loaded whole.

    Returns:
        Rows and SHA-256 per file.

    Raises:
        SeedError: If a cohort file is missing.
        ValueError: If document_key is empty.
    """
    if not document_key:
        raise ValueError("DOCUMENT_HASH_KEY is not set")
    cursor = session.connection().connection.driver_connection.cursor()
    detail: dict[str, Any] = {"rows": {}, "sha256": {}}
    # A connection of its own: duckdb's default one is shared by every thread of the process.
    con = duckdb.connect()
    for table in COHORT_TABLES:
        path = cohort_dir / f"{table}.parquet"
        if not path.exists():
            con.close()
            raise SeedError(f"{path} is missing; run make data first")
        rel = con.read_parquet(str(path))
        columns = list(rel.columns)
        if customer_ids is not None and "customer_id" in columns:
            rows = con.execute(
                "SELECT * FROM read_parquet(?) WHERE customer_id IN (SELECT unnest(?))",
                [str(path), sorted(customer_ids)],
            ).fetchall()
        else:
            rows = rel.fetchall()
        if table == "customers":
            columns, rows = _hash_documents(columns, rows, document_key)
        stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
            sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns))
        )
        with cursor.copy(stmt) as copy:
            for row in rows:
                copy.write_row(row)
        detail["rows"][table] = len(rows)
        detail["sha256"][table] = hashlib.sha256(path.read_bytes()).hexdigest()
    con.close()
    return detail


def _hash_documents(
    columns: list[str], rows: list[tuple[Any, ...]], key: str
) -> tuple[list[str], list[tuple[Any, ...]]]:
    kind, number = columns.index("document_type"), columns.index("document_number")
    out_columns = [c for c in columns if c != "document_number"] + ["document_hash"]
    out_rows = [
        (*(v for i, v in enumerate(r) if i != number), document_hash(key, r[kind], r[number]))
        for r in rows
    ]
    return out_columns, out_rows


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, default=str)

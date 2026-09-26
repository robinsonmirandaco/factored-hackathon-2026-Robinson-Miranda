"""Quality report (`docs/reports/calidad.md`), written by `make data` from silver, quarantine and
the bronze manifest.

Only counts, rates and categorical, non-personal values are printed. Quarantined values are never
shown, because a rejected value can be personal data.
"""

from collections import defaultdict
from datetime import datetime
from pathlib import Path

import duckdb

from pipeline.contracts import BY_TABLE, CONTRACTS, Contract, alert_columns
from pipeline.manifest import BronzeFile
from pipeline.silver import Normalization, TableResult, q, quarantine_dir, sql_str

DECLARED_DUPLICATE_RATE = 2.0
DECLARED_NULL_RATE = 5.0
# A category whose rows have the column empty at least this often explains those nulls.
STRUCTURAL_SHARE = 0.999


def _pct(part: int | float, total: int | float) -> str:
    if not total:
        return "n/a"
    share = 100 * part / total
    return "<0.01%" if 0 < share < 0.005 else f"{share:.2f}%"


def _table(header: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines


def _n(value: int) -> str:
    return f"{value:,}"


def quarantine_counts(con: duckdb.DuckDBPyConnection, data_dir: Path) -> list[tuple]:
    """Counts quarantined rows per table, rule and column.

    Args:
        con: DuckDB connection.
        data_dir: Root data directory.

    Returns:
        (table, rule, column, rows) sorted by table order, then rule and column.
    """
    rows = []
    for contract in CONTRACTS:
        folder = quarantine_dir(data_dir, contract.table)
        files = sorted(str(p) for p in folder.rglob("*.parquet"))
        if not files:
            continue
        rows += con.execute(
            f"""
            SELECT table_name, v.rule, coalesce(v.column_name, ''), count(*)
            FROM (SELECT table_name, unnest(violations) AS v
                  FROM read_parquet([{", ".join(sql_str(f) for f in files)}]))
            GROUP BY ALL ORDER BY 2, 3
            """
        ).fetchall()
    return rows


def _reconciliation(results: list[TableResult]) -> list[str]:
    rows = [
        [
            r.table,
            _n(r.files),
            _n(r.bronze_rows),
            _n(r.silver_rows),
            _n(r.quarantine_rows),
            "yes" if r.bronze_rows == r.silver_rows + r.quarantine_rows else "NO",
        ]
        for r in results
    ]
    return _table(
        ["Table", "Files", "Bronze rows", "Silver rows", "Quarantine rows", "Adds up"], rows
    )


def _quarantine(counts: list[tuple], results: list[TableResult]) -> list[str]:
    if not counts:
        return ["No row was quarantined."]
    bronze = {r.table: r.bronze_rows for r in results}
    rows = [[t, rule, col, _n(n), _pct(n, bronze[t])] for t, rule, col, n in counts]
    note = [
        "",
        "A row can break more than one rule, so rule counts can add up to more than the "
        "quarantined rows of the table.",
    ]
    return _table(["Table", "Rule", "Column", "Rows", "Share of bronze"], rows) + note


def _alerts(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = []
    for contract in CONTRACTS:
        view = q(f"silver_{contract.table}")
        for column in alert_columns(contract):
            n, total = con.execute(
                f"SELECT count(*) FILTER (WHERE {q(column)}), count(*) FROM {view}"
            ).fetchone() or (0, 0)
            rows.append([contract.table, column, _n(n), _pct(n, total)])
    lines = _table(["Table", "Alert column", "Flagged rows", "Share of silver"], rows)
    by_doc = con.execute(
        """
        SELECT country, document_type, count(*), count(*) FILTER (WHERE alert_document_country)
        FROM silver_customers GROUP BY ALL ORDER BY ALL
        """
    ).fetchall()
    lines += ["", "Document type by customer country:", ""]
    lines += _table(
        ["Country", "Document type", "Customers", "Flagged"],
        [[c, d, _n(n), _n(f)] for c, d, n, f in by_doc],
    )
    return lines


def _duplicates(
    counts: list[tuple], con: duckdb.DuckDBPyConnection, results: list[TableResult]
) -> list[str]:
    by_rule: dict[tuple[str, str], int] = defaultdict(int)
    for table, rule, _, n in counts:
        by_rule[(table, rule)] += n
    bronze = {r.table: r.bronze_rows for r in results}
    rows = []
    for contract in CONTRACTS:
        pk = by_rule[(contract.table, "duplicate_key")]
        if contract.partitioned:
            bk = by_rule[(contract.table, "duplicate_business_key")]
            handling = "quarantine"
        else:
            row = con.execute(
                f"SELECT count(*) FILTER (WHERE alert_duplicate_business_key) "
                f"FROM {q('silver_' + contract.table)}"
            ).fetchone()
            bk = row[0] if row else 0
            handling = "alert"
        rows.append(
            [
                contract.table,
                ", ".join(contract.primary_key),
                _n(pk),
                ", ".join(contract.business_key),
                _n(bk),
                _pct(bk, bronze[contract.table]),
                handling,
            ]
        )
    lines = _table(
        [
            "Table",
            "Primary key",
            "PK duplicates",
            "Business key",
            "Business key duplicates",
            "Rate",
            "Handling",
        ],
        rows,
    )
    return lines + [
        "",
        f"The Dataset Summary declares about {DECLARED_DUPLICATE_RATE:.0f}% duplicates. In fact "
        "tables, a repeated business key sends every occurrence after the first to quarantine; in "
        "dimension tables every row of the group is flagged and kept, because rejecting a "
        "customer, product or agent would reject every row that references it.",
    ]


def _schema(manifest: dict[str, BronzeFile]) -> list[str]:
    groups: dict[tuple[str, tuple[str, ...]], list[BronzeFile]] = defaultdict(list)
    for entry in manifest.values():
        groups[(entry.table, tuple(entry.header))].append(entry)
    rows = []
    for (table, header), files in sorted(groups.items(), key=lambda g: (g[0][0], g[0][1])):
        dates = sorted(f.partition_date for f in files if f.partition_date)
        expected = list(header) == BY_TABLE[table].header
        rows.append(
            [
                table,
                _n(len(files)),
                dates[0] if dates else "snapshot",
                dates[-1] if dates else "snapshot",
                len(header),
                "matches contract"
                if expected
                else "schema_mismatch: "
                + ", ".join(sorted(set(header) ^ set(BY_TABLE[table].header))),
            ]
        )
    lines = _table(
        ["Table", "Files", "First partition", "Last partition", "Columns", "Header"], rows
    )
    changed = [r[0] for r in rows if r[5] != "matches contract"]
    summary = (
        "No column appears, disappears or changes order across partitions."
        if not changed
        else "Header changes found in: " + ", ".join(sorted(set(changed))) + "."
    )
    return lines + ["", summary]


def _explainers(contract: Contract) -> list[str]:
    return [c.name for c in contract.columns if c.domain or c.type == "BOOLEAN" or c.mapped_to]


def null_profile(con: duckdb.DuckDBPyConnection, contract: Contract) -> list[list[object]]:
    """Splits each column's nulls into structural and residual.

    Nulls are structural when they fall in categories of another column where the value is
    always empty (at least 99.9% of the rows), for example `merchant_name` when the transaction is
    not a purchase. The rest are residual: the rate among the rows where a value was expected.

    Args:
        con: DuckDB connection with the silver views.
        contract: Table contract.

    Returns:
        Rows: column, null rate, structural rate, explaining column, residual rate.
    """
    view = q(f"silver_{contract.table}")
    total_row = con.execute(f"SELECT count(*) FROM {view}").fetchone()
    total = total_row[0] if total_row else 0
    rows: list[list[object]] = []
    if not total:
        return rows
    for column in contract.columns:
        col = q(column.name)
        nulls_row = con.execute(
            f"SELECT count(*) FILTER (WHERE {col} IS NULL) FROM {view}"
        ).fetchone()
        nulls = nulls_row[0] if nulls_row else 0
        if nulls == 0:
            continue
        best: tuple[int, str, int, int] = (0, "", total, nulls)
        for explainer in _explainers(contract):
            if explainer == column.name or nulls == total:
                continue
            structural, rest_rows, rest_nulls = con.execute(
                f"""
                SELECT coalesce(sum(k) FILTER (WHERE share >= {STRUCTURAL_SHARE}), 0),
                       coalesce(sum(k) FILTER (WHERE share < {STRUCTURAL_SHARE}), 0),
                       coalesce(sum(n) FILTER (WHERE share < {STRUCTURAL_SHARE}), 0)
                FROM (SELECT {q(explainer)}, count(*) AS k,
                             count(*) FILTER (WHERE {col} IS NULL) AS n,
                             count(*) FILTER (WHERE {col} IS NULL) / count(*) AS share
                      FROM {view} GROUP BY 1)
                """
            ).fetchone() or (0, 0, 0)
            if structural > best[0] and rest_rows:
                best = (structural, explainer, rest_rows, rest_nulls)
        structural, explainer, rest_rows, rest_nulls = best
        rows.append(
            [
                column.name,
                _pct(nulls, total),
                _pct(structural, total) if explainer else "0.00%",
                explainer or "none found",
                _pct(rest_nulls, rest_rows),
            ]
        )
    return rows


def _nulls(con: duckdb.DuckDBPyConnection) -> list[str]:
    lines = [
        "Residual rate = nulls among rows where a value was expected. The Dataset Summary "
        f"declares about {DECLARED_NULL_RATE:.0f}% nulls; compare it with the residual column. "
        "Columns with no structural explanation are shown with their whole rate as residual.",
    ]
    for contract in CONTRACTS:
        rows = null_profile(con, contract)
        if not rows:
            continue
        lines += ["", f"### {contract.table}", ""]
        lines += _table(["Column", "Null rate", "Structural", "Explained by", "Residual"], rows)
    return lines


def _time_zone(con: duckdb.DuckDBPyConnection, trazo_now: datetime) -> list[str]:
    rows = []
    for contract in CONTRACTS:
        if not contract.event_column:
            continue
        view, ev = q(f"silver_{contract.table}"), q(contract.event_column)
        for country, n, same, next_day, after_now in con.execute(
            f"""
            SELECT country_code, count(*),
                   count(*) FILTER (WHERE event_date_local = partition_date),
                   count(*) FILTER (WHERE event_date_local = partition_date + 1),
                   count(*) FILTER (WHERE timezone(timezone, {ev}) > ?)
            FROM {view} GROUP BY 1 ORDER BY 1
            """,
            [trazo_now],
        ).fetchall():
            rows.append(
                [
                    contract.table,
                    country,
                    _n(n),
                    _n(same),
                    f"{_n(next_day)} ({_pct(next_day, n)})",
                    _n(after_now),
                ]
            )
    windows = ", ".join(
        f"{c.table} D {c.cutoff_hours:02d}:00 to D+1 {c.cutoff_hours:02d}:00"
        for c in CONTRACTS
        if c.cutoff_hours is not None
    )
    lines = [
        "**Rule adopted (assumption).** Source timestamps carry no zone. A partition D is an "
        f"operational day with a fixed cutoff per table ({windows}), the same in every country; "
        "satisfaction surveys and call transcripts are filed under the partition of their "
        "interaction. Silver reads each timestamp as local wall-clock time of the customer's "
        "country and stores it with that zone; `event_date_local` is the local calendar day and "
        "`partition_date` stays as lineage.",
        "",
        "**Hypothesis in design §9.3 (event in UTC, partition in local time): not confirmed.** "
        "It would need a cutoff equal to each country's UTC offset (3 h Argentina, 5 h Colombia, "
        "6 h Mexico), but the cutoff is identical in the three countries, and 8 h matches no "
        "country. Events outside their partition window are quarantined as "
        "`event_outside_partition`.",
        "",
        f"Rows after TRAZO_NOW ({trazo_now.isoformat(sep=' ')} local) are events of the last "
        "partition that fall after the simulated clock; they are kept and are in the future "
        "for the clock.",
        "",
    ]
    return lines + _table(
        [
            "Table",
            "Country",
            "Rows",
            "Event on partition day",
            "Event on the next day",
            "After TRAZO_NOW",
        ],
        rows,
    )


def _references(con: duckdb.DuckDBPyConnection, counts: list[tuple]) -> list[str]:
    rejected = {(t, col): n for t, rule, col, n in counts if rule == "missing_reference"}
    rows = []
    for contract in CONTRACTS:
        for ref in contract.references:
            columns = ",".join(ref.columns)
            if ref.alert:
                row = con.execute(
                    f"SELECT count(*) FILTER (WHERE {q('alert_' + ref.alert)}), count(*) "
                    f"FROM {q('silver_' + contract.table)}"
                ).fetchone() or (0, 0)
                failed, handling = row[0], f"alert_{ref.alert}"
            else:
                failed, handling = rejected.get((contract.table, columns), 0), "quarantine"
            target = f"{ref.table}({', '.join(ref.target)})"
            rows.append([contract.table, columns, target, handling, _n(failed)])
    return _table(["Table", "Columns", "References", "Handling", "Rows that fail"], rows)


def _findings(con: duckdb.DuckDBPyConnection, counts: list[tuple]) -> list[str]:
    lines = ["### Referential integrity", ""]
    lines += _references(con, counts)
    lines += [
        "",
        "Branch references of customers and agents and the product of a complaint fail for "
        "almost every row, so they are flagged: rejecting them would empty those tables.",
        "",
        "### Currency by customer country (transactions)",
        "",
    ]
    lines += _table(
        ["Customer country", "Currency", "Transactions", "Flagged currency_country"],
        [
            [c, cur, _n(n), _n(f)]
            for c, cur, n, f in con.execute(
                """
                SELECT country_code, currency, count(*),
                       count(*) FILTER (WHERE alert_currency_country)
                FROM silver_transactions GROUP BY ALL ORDER BY ALL
                """
            ).fetchall()
        ],
    )
    lines += [
        "",
        "`alert_currency_country` flags a currency different from the local currency of the "
        "customer's country. Transactions of Mexican customers are all in USD, never in MXN.",
        "",
        "### Country names (transactions)",
        "",
    ]
    lines += _table(
        ["Source value", "Normalized code", "Transactions"],
        [
            [s, c, _n(n)]
            for s, c, n in con.execute(
                """
                SELECT transaction_country_source, transaction_country, count(*)
                FROM silver_transactions GROUP BY ALL ORDER BY ALL
                """
            ).fetchall()
        ],
    )
    row = con.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE abs(epoch(s.survey_date - i.interaction_date) / 3600
                                          - s.response_time_hours) < 0.01)
        FROM silver_satisfaction_surveys s
        JOIN silver_call_center_interactions i ON i.interaction_id = s.interaction_id
        WHERE s.response_time_hours IS NOT NULL
        """
    ).fetchone() or (0, 0)
    lines += [
        "",
        "### response_time_hours (satisfaction surveys)",
        "",
        f"`response_time_hours` equals the hours between the interaction and the survey in "
        f"{_n(row[1])} of {_n(row[0])} surveys ({_pct(row[1], row[0])}). It is not the delay "
        "it seems to describe and is not used as such.",
    ]
    return lines


def render(
    con: duckdb.DuckDBPyConnection,
    data_dir: Path,
    results: list[TableResult],
    manifest: dict[str, BronzeFile],
    norm: Normalization,
    header: list[str],
    trazo_now: datetime,
) -> str:
    """Renders the quality report.

    Args:
        con: DuckDB connection with every `silver_<table>` view registered.
        data_dir: Root data directory.
        results: Row counts per table.
        manifest: Bronze manifest.
        norm: Normalization mapping.
        header: Run lines shown at the top (version, batch, seed).
        trazo_now: Simulated clock.

    Returns:
        Markdown text.
    """
    counts = quarantine_counts(con, data_dir)
    lines = [
        "# Data quality",
        "",
        "Generated by `make data` (stories TRZ-02, TRZ-03 and TRZ-04; design §9). Counts only: no "
        "row with personal fields is shown.",
        "",
        "## Run",
        "",
        *header,
        f"- Normalization mapping version: {norm.version}",
        "",
        "## Rows per layer",
        "",
        *_reconciliation(results),
        "",
        "## Quarantine by rule",
        "",
        *_quarantine(counts, results),
        "",
        "## Alerts (flagged, not rejected)",
        "",
        *_alerts(con),
        "",
        "## Duplicates",
        "",
        *_duplicates(counts, con, results),
        "",
        "## Schema evolution",
        "",
        *_schema(manifest),
        "",
        "## Nulls: structural and residual",
        "",
        *_nulls(con),
        "",
        "## Time zone",
        "",
        *_time_zone(con, trazo_now),
        "",
        "## Findings",
        "",
        *_findings(con, counts),
    ]
    return "\n".join(lines) + "\n"

"""Quality report (`docs/reports/calidad.md`), written by `make data` and `make report-data` from
silver, quarantine and the bronze manifest.

Only counts, rates and categorical, non-personal values are printed. Quarantined values are never
shown, because a rejected value can be personal data.
"""

from collections import defaultdict
from datetime import datetime, time
from pathlib import Path

import duckdb

from pipeline.contracts import BY_TABLE, CONTRACTS, Contract, alert_columns
from pipeline.manifest import BronzeFile
from pipeline.silver import Normalization, TableResult, q, quarantine_dir, sql_str

DECLARED_DUPLICATE_RATE = 2.0
DECLARED_NULL_RATE = 5.0
# A category whose rows have the column empty at least this often explains those nulls.
STRUCTURAL_SHARE = 0.999
# Rows per in-scope table in the LATAM Bank Dataset Summary, version 1.0.0.
DOCUMENTED_ROWS = {
    "branches": 350,
    "service_agents": 1_200,
    "customers": 150_000,
    "products": 400_000,
    "daily_exchange_rates": 3_000,
    "transactions": 5_000_000,
    "call_center_interactions": 800_000,
    "complaints": 80_000,
    "satisfaction_surveys": 250_000,
    "call_transcripts": 200_000,
}
# Free-text columns whose number of distinct values shows they come from templates.
TEXT_COLUMNS = (
    ("call_transcripts", "customer_text"),
    ("call_transcripts", "agent_text"),
    ("call_transcripts", "full_text"),
    ("call_transcripts", "detected_intents"),
    ("complaints", "description"),
)


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


def event_offsets(con: duckdb.DuckDBPyConnection, trazo_now: datetime) -> list[tuple]:
    """Places every event relative to its partition, per table and customer country.

    Args:
        con: DuckDB connection with the silver views.
        trazo_now: Simulated clock.

    Returns:
        (table, country, rows, on partition day, on the next day, after the partition day,
        earliest local time on the partition day, latest local time on the next day, after
        TRAZO_NOW), sorted by table order and country.
    """
    rows = []
    for contract in CONTRACTS:
        if not contract.event_column:
            continue
        view, ev = q(f"silver_{contract.table}"), q(contract.event_column)
        local = f"timezone(timezone, {ev})"
        rows += con.execute(
            f"""
            SELECT {sql_str(contract.table)}, country_code, count(*),
                   count(*) FILTER (WHERE event_date_local = partition_date),
                   count(*) FILTER (WHERE event_date_local = partition_date + 1),
                   count(*) FILTER (WHERE event_date_local > partition_date),
                   min(CAST({local} AS TIME)) FILTER (WHERE event_date_local = partition_date),
                   max(CAST({local} AS TIME)) FILTER (WHERE event_date_local = partition_date + 1),
                   count(*) FILTER (WHERE {local} > ?)
            FROM {view} GROUP BY 2 ORDER BY 2
            """,
            [trazo_now],
        ).fetchall()
    return rows


def cutoff_holds(offsets: list[tuple]) -> bool:
    """Tells whether every table with a cutoff fits its operational day in every country.

    The day of partition D runs from the cutoff hour of D to the cutoff hour of D+1: no event
    before the cutoff on D, none after it on D+1 and none two or more days later.

    Args:
        offsets: Output of `event_offsets`.

    Returns:
        True when the fixed cutoff explains every event of those tables.
    """
    checked = False
    for table, _, _, _, next_day, after, earliest, latest, _ in offsets:
        cutoff = BY_TABLE[table].cutoff_hours
        if cutoff is None:
            continue
        checked = True
        start = time(cutoff)
        if (earliest is not None and earliest < start) or (latest is not None and latest > start):
            return False
        if next_day != after:
            return False
    return checked


def _clock(value: time | None) -> str:
    return value.isoformat() if value is not None else "n/a"


def _time_zone(offsets: list[tuple], trazo_now: datetime) -> list[str]:
    holds = cutoff_holds(offsets)
    windows = ", ".join(
        f"{c.table} D {c.cutoff_hours:02d}:00 to D+1 {c.cutoff_hours:02d}:00"
        for c in CONTRACTS
        if c.cutoff_hours is not None
    )
    evidence = (
        "**Evidence.** In the three countries, no event on the partition day is earlier than "
        "the cutoff hour of the table, none on the next day is later than it, and every event "
        "after the partition day falls on the next day, never later (first table below; source: "
        "`silver/<table>`, `event_date_local` against `partition_date`, time in the customer's "
        "zone). The cutoff is the same in every country."
        if holds
        else "**Evidence.** The fixed cutoff does NOT explain every event: some event falls "
        "outside its operational day in the first table below. The rule needs review."
    )
    hypothesis = (
        "**Hypothesis in design §9.3 (event in UTC, partition in local time): not confirmed.** "
        "It would need a cutoff equal to each country's UTC offset (3 h Argentina, 5 h Colombia, "
        "6 h Mexico), but each table has one cutoff hour shared by the three countries, and 8 h "
        "matches no country's offset. Events outside their partition window are quarantined as "
        "`event_outside_partition`."
        if holds
        else "**Hypothesis in design §9.3 (event in UTC, partition in local time): not "
        "settled by this run**, because the fixed cutoff does not hold."
    )
    lines = [
        "**Rule adopted (assumption).** Source timestamps carry no zone. A partition D is an "
        f"operational day with a fixed cutoff per table ({windows}), the same in every country; "
        "satisfaction surveys and call transcripts are filed under the partition of their "
        "interaction. Silver reads each timestamp as local wall-clock time of the customer's "
        "country and stores it with that zone; `event_date_local` is the local calendar day and "
        "`partition_date` stays as lineage.",
        "",
        evidence,
        "",
        hypothesis,
        "",
        f"Rows after TRAZO_NOW ({trazo_now.isoformat(sep=' ')} local) are events of the last "
        "partition that fall after the simulated clock; they are kept and are in the future "
        "for the clock.",
        "",
    ]
    rows = []
    for table, country, n, same, next_day, after, earliest, latest, after_now in offsets:
        with_cutoff = BY_TABLE[table].cutoff_hours is not None
        rows.append(
            [
                table,
                country,
                _n(n),
                _n(same),
                f"{_n(next_day)} ({_pct(next_day, n)})",
                _n(after),
                _clock(earliest) if with_cutoff else "follows interaction",
                _clock(latest) if with_cutoff else "follows interaction",
                _n(after_now),
            ]
        )
    lines += _table(
        [
            "Table",
            "Country",
            "Rows",
            "Event on partition day",
            "Event on the next day",
            "Event after partition day",
            "Earliest time on partition day",
            "Latest time on the next day",
            "After TRAZO_NOW",
        ],
        rows,
    )
    totals: dict[str, list[int]] = {}
    for table, _, n, _, _, after, _, _, _ in offsets:
        acc = totals.setdefault(table, [0, 0])
        acc[0] += n
        acc[1] += after
    lines += [
        "",
        "Events dated after their partition day, all countries (source: `silver/<table>`, filter "
        "`event_date_local > partition_date`):",
        "",
    ]
    lines += _table(
        ["Table", "Rows", "Event after partition day", "Share"],
        [[t, _n(n), _n(a), _pct(a, n)] for t, (n, a) in totals.items()],
    )
    return lines


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


def _documented(results: list[TableResult]) -> list[str]:
    rows = []
    for r in results:
        documented = DOCUMENTED_ROWS[r.table]
        diff = r.bronze_rows - documented
        rows.append(
            [r.table, _n(documented), _n(r.bronze_rows), f"{diff:+,}", _pct(diff, documented)]
        )
    return _table(["Table", "Documented rows", "Bronze rows", "Difference", "Relative"], rows) + [
        "",
        "Documented rows: LATAM Bank Dataset Summary, version 1.0.0, tables overview. Bronze rows: "
        "data rows of every CSV of the table in the manifest (see Rows per layer).",
    ]


def _templates(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = []
    for table, column in TEXT_COLUMNS:
        n, distinct = con.execute(
            f"SELECT count({q(column)}), count(DISTINCT {q(column)}) FROM {q('silver_' + table)}"
        ).fetchone() or (0, 0)
        rows.append([table, column, _n(n), _n(distinct)])
    return _table(["Table", "Column", "Non-null rows", "Distinct values"], rows) + [
        "",
        "Source: `silver/<table>`, distinct non-null values of the column. The texts are counted, "
        "never shown. So few distinct values mean the texts come from templates: no language "
        "model is trained on them, and it is reported as a limitation.",
    ]


def _product_types(con: duckdb.DuckDBPyConnection, norm: Normalization) -> list[str]:
    rows = con.execute(
        """
        SELECT product_type_source, product_type, count(*)
        FROM silver_products GROUP BY ALL ORDER BY ALL
        """
    ).fetchall()
    return _table(
        ["Source value", "Normalized value", "Products"], [[s, t, _n(n)] for s, t, n in rows]
    ) + [
        "",
        "Source: `silver/products`, `product_type_source` against `product_type`. Mapping: "
        f"`config/normalization.yaml`, version {norm.version}.",
    ]


def _one(con: duckdb.DuckDBPyConnection, sql: str) -> tuple:
    return con.execute(sql).fetchone() or ()


def _design_findings(
    con: duckdb.DuckDBPyConnection, results: list[TableResult], offsets: list[tuple]
) -> list[str]:
    bronze = {r.table: r.bronze_rows for r in results}
    customer_texts, descriptions, intents = (
        _one(con, "SELECT count(DISTINCT customer_text) FROM silver_call_transcripts")[0],
        _one(con, "SELECT count(DISTINCT description) FROM silver_complaints")[0],
        _one(con, "SELECT count(DISTINCT detected_intents) FROM silver_call_transcripts")[0],
    )
    unlinked, complaints = _one(
        con,
        "SELECT count(*) FILTER (WHERE origin_interaction_id IS NULL), count(*) "
        "FROM silver_complaints",
    )
    after = defaultdict(int)
    for table, _, _, _, _, n, _, _, _ in offsets:
        after[table] += n
    agents_bad, agents_set = _one(
        con,
        "SELECT count(*) FILTER (WHERE alert_branch_unresolved), "
        "count(assigned_branch_id) FROM silver_service_agents",
    )
    customers_bad, customers_set = _one(
        con,
        "SELECT count(*) FILTER (WHERE alert_branch_unresolved), "
        "count(registration_branch_id) FROM silver_customers",
    )
    not_owned, with_product = _one(
        con,
        "SELECT count(*) FILTER (WHERE alert_product_not_owned), "
        "count(affected_product_id) FROM silver_complaints",
    )
    mismatch, amount_only, currency_only = _one(
        con,
        """
        SELECT count(*) FILTER (WHERE alert_amount_currency_mismatch),
               count(*) FILTER (WHERE claimed_amount IS NOT NULL AND currency IS NULL),
               count(*) FILTER (WHERE claimed_amount IS NULL AND currency IS NOT NULL)
        FROM silver_complaints
        """,
    )
    mx_dni, mx_curp = _one(
        con,
        "SELECT count(*) FILTER (WHERE document_type = 'DNI'), "
        "count(*) FILTER (WHERE document_type = 'CURP') FROM silver_customers "
        "WHERE country_code = 'MX'",
    )
    mx_tx, mx_usd, mx_mxn = _one(
        con,
        "SELECT count(*), count(*) FILTER (WHERE currency = 'USD'), "
        "count(*) FILTER (WHERE currency = 'MXN') FROM silver_transactions "
        "WHERE country_code = 'MX'",
    )
    amount_nulls, product_nulls = _one(
        con,
        "SELECT count(*) FILTER (WHERE claimed_amount IS NULL), "
        "count(*) FILTER (WHERE affected_product_id IS NULL) FROM silver_complaints",
    )
    spanish = _one(
        con,
        "SELECT count(DISTINCT product_type_source) FROM silver_products "
        "WHERE product_type_source <> product_type",
    )[0]
    daily = [t for t in DOCUMENTED_ROWS if BY_TABLE[t].partitioned]
    daily_found = sum(bronze[t] for t in daily)
    daily_documented = sum(DOCUMENTED_ROWS[t] for t in daily)
    rows = [
        [
            "Template texts",
            f"{_n(customer_texts)} distinct customer_text in {_n(bronze['call_transcripts'])} "
            f"transcripts; {_n(descriptions)} distinct complaint descriptions; "
            f"{_n(intents)} detected_intents value(s)",
            "`silver/call_transcripts`, `silver/complaints`: count(DISTINCT column)",
            "Template texts",
        ],
        [
            "Complaints without link",
            f"origin_interaction_id null in {_n(unlinked)} of {_n(complaints)} "
            f"({_pct(unlinked, complaints)})",
            "`silver/complaints`: origin_interaction_id IS NULL",
            "Nulls (complaints)",
        ],
        [
            "Events dated after their partition",
            "; ".join(f"{t} {_n(n)}" for t, n in after.items()),
            "`silver/<table>`: event_date_local > partition_date",
            "Time zone",
        ],
        [
            "Broken referential integrity",
            f"service_agents.assigned_branch_id {_n(agents_bad)} of {_n(agents_set)} set; "
            f"customers.registration_branch_id {_n(customers_bad)} of {_n(customers_set)}; "
            f"complaints.affected_product_id of another customer {_n(not_owned)} of "
            f"{_n(with_product)} set",
            "`silver/<table>`: alert_branch_unresolved, alert_product_not_owned",
            "Findings, Referential integrity",
        ],
        [
            "Declared against found quality",
            "duplicates, nulls and schema per table; complaints with amount and currency "
            f"inconsistent {_n(mismatch)} ({_pct(mismatch, complaints)}): {_n(amount_only)} "
            f"amount without currency, {_n(currency_only)} currency without amount",
            "`silver/complaints`: alert_amount_currency_mismatch; quarantine; manifest headers",
            "Duplicates, Schema evolution, Nulls",
        ],
        [
            "Incoherent document",
            f"Mexican customers with DNI {_n(mx_dni)}; with CURP {_n(mx_curp)}",
            "`silver/customers`: country_code = 'MX', by document_type",
            "Alerts",
        ],
        [
            "Incoherent currency",
            f"transactions of Mexican customers {_n(mx_tx)}: USD {_n(mx_usd)}, MXN {_n(mx_mxn)}",
            "`silver/transactions`: country_code = 'MX', by currency",
            "Findings, Currency by customer country",
        ],
        [
            "Fewer rows than documented",
            f"daily tables {_n(daily_found)} against {_n(daily_documented)} documented "
            f"({_pct(daily_found - daily_documented, daily_documented)}); daily_exchange_rates "
            f"{_n(bronze['daily_exchange_rates'])} against "
            f"{_n(DOCUMENTED_ROWS['daily_exchange_rates'])}",
            "manifest rows against the Dataset Summary 1.0.0",
            "Rows against the Dataset Summary",
        ],
        [
            "Values in another language",
            f"{_n(spanish)} product_type source values in Spanish, mapped to the dictionary",
            "`silver/products`: product_type_source <> product_type",
            "Findings, product_type",
        ],
        [
            "No exact duplicates or repeated keys",
            "primary and business key duplicates per table",
            "quarantine rules duplicate_key, duplicate_business_key; alert_duplicate_business_key",
            "Duplicates",
        ],
        [
            "High nulls in optional fields",
            f"claimed_amount {_pct(amount_nulls, complaints)}, affected_product_id "
            f"{_pct(product_nulls, complaints)}",
            "`silver/complaints`: column IS NULL",
            "Nulls (complaints)",
        ],
        [
            "Candidate density",
            "distribution of disputable transactions per customer",
            "`docs/reports/densidad.md` (`make density`)",
            "densidad.md",
        ],
    ]
    return _table(["Finding", "Figure", "Source and filter", "Section"], rows)


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
    offsets = event_offsets(con, trazo_now)
    lines = [
        "# Data quality",
        "",
        "Generated by `make data` and `make report-data` (stories TRZ-02, TRZ-03, TRZ-04 and "
        "TRZ-06; design §9). Counts only: no row with personal fields is shown. Each table names "
        "its source; `silver/<table>` is the silver layer under DATA_DIR.",
        "",
        "## Run",
        "",
        *header,
        f"- Normalization mapping version: {norm.version}",
        "",
        "## Design §9.3 findings",
        "",
        *_design_findings(con, results, offsets),
        "",
        "## Rows per layer",
        "",
        *_reconciliation(results),
        "",
        "## Rows against the Dataset Summary",
        "",
        *_documented(results),
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
        *_time_zone(offsets, trazo_now),
        "",
        "## Findings",
        "",
        *_findings(con, counts),
        "",
        "### product_type (products)",
        "",
        *_product_types(con, norm),
        "",
        "## Template texts",
        "",
        *_templates(con),
    ]
    return "\n".join(lines) + "\n"

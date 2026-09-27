"""Demand report (`docs/reports/demanda.md`), written by `make report-data` from the gold demand
marts and silver complaints.

It backs design §2.1 with data: how each contact category performs, how complete dispute
complaints are, when and where complaints and disputes arrive, and the operational limits the
system has to respect. Only counts, rates and categorical values are printed.
"""

from collections import defaultdict
from pathlib import Path

import duckdb

from pipeline.contracts import BY_TABLE
from pipeline.report import _n, _pct, _table
from pipeline.silver import sql_str

# Complaint subcategories that are transaction disputes (design §2.1).
DISPUTE_SUBCATEGORIES = ("Cargo no reconocido", "Cobro indebido")
COMPLAINT_CATEGORY = "Queja"
# Largest over smallest hourly share for demand to count as flat over the day.
FLAT_HOUR_RATIO = 1.25
# Gold marts the report reads.
MARTS = ("demand_interactions",)
WEEKDAYS = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}

DISPUTES = f"subcategory IN {DISPUTE_SUBCATEGORIES}"
DISPUTE_FILTER = f"`silver/complaints`, `{DISPUTES}`"


def _num(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _mart(data_dir: Path, name: str) -> str:
    return f"read_parquet({sql_str(str(data_dir / 'gold' / f'{name}.parquet'))})"


def category_rows(con: duckdb.DuckDBPyConnection, data_dir: Path) -> list[tuple]:
    """Aggregates the interactions mart by contact category (design §2.1 table).

    Args:
        con: DuckDB connection.
        data_dir: Root data directory.

    Returns:
        (category, contacts, share of contacts, share of agent minutes, minutes per contact,
        resolved rate, follow-up rate, mean CSAT, CSAT responses), largest category first.
        Shares and rates are fractions; minutes per contact only counts contacts with a duration.
    """
    return con.execute(
        f"""
        SELECT reason_category, sum(contacts),
               sum(contacts) / sum(sum(contacts)) OVER (),
               sum(duration_seconds) / sum(sum(duration_seconds)) OVER (),
               sum(duration_seconds) / nullif(sum(contacts_with_duration), 0) / 60,
               sum(resolved) / sum(contacts),
               sum(followup) / sum(contacts),
               sum(csat_score_sum) / nullif(sum(csat_responses), 0),
               sum(csat_responses)
        FROM {_mart(data_dir, "demand_interactions")}
        GROUP BY 1 ORDER BY 2 DESC, 1
        """
    ).fetchall()


def _categories(con: duckdb.DuckDBPyConnection, data_dir: Path) -> list[str]:
    rows = [
        [
            c,
            _n(n),
            f"{100 * share:.2f}",
            f"{100 * minutes:.2f}",
            _num(per_contact),
            f"{100 * resolved:.2f}%",
            f"{100 * followup:.2f}%",
            _num(csat),
            _n(responses),
        ]
        for c, n, share, minutes, per_contact, resolved, followup, csat, responses in (
            category_rows(con, data_dir)
        )
    ]
    header = [
        "Category",
        "Contacts",
        "% contacts",
        "% agent minutes",
        "Min. per contact",
        "Resolved at first contact",
        "Requires follow-up",
        "CSAT",
        "CSAT responses",
    ]
    return _table(header, rows) + [
        "",
        "Source: `gold/demand_interactions` (from `silver/call_center_interactions` left-joined "
        "with `silver/satisfaction_surveys` where `survey_type = 'CSAT'`), grouped by "
        "`reason_category`, no filter. % agent minutes = sum of `duration_seconds`. "
        "Min. per contact = `duration_seconds` / `contacts_with_duration`: interaction types "
        "without a duration (14% of contacts, structural nulls in calidad.md) are left out of the "
        "denominator. Resolved at first contact = `was_resolved`; requires follow-up = "
        "`requires_followup`; CSAT = mean `main_score` of CSAT surveys.",
    ]


def dispute_figures(con: duckdb.DuckDBPyConnection) -> dict[str, tuple]:
    """Counts complaints and dispute complaints side by side.

    Args:
        con: DuckDB connection with the `silver_complaints` view.

    Returns:
        Per measure, (value over all complaints, value over dispute complaints).
    """
    names = (
        "rows",
        "with_amount",
        "with_product",
        "with_both",
        "with_origin",
        "sla_breached",
        "with_resolution_days",
        "median_resolution_days",
    )
    select = """
        count(*), count(claimed_amount), count(affected_product_id),
        count(*) FILTER (WHERE claimed_amount IS NOT NULL AND affected_product_id IS NOT NULL),
        count(origin_interaction_id), count(*) FILTER (WHERE sla_breached),
        count(resolution_days), median(resolution_days)
    """
    every = con.execute(f"SELECT {select} FROM silver_complaints").fetchone() or ()
    disputes = con.execute(f"SELECT {select} FROM silver_complaints WHERE {DISPUTES}").fetchone()
    return dict(zip(names, zip(every, disputes or (), strict=True), strict=True))


def _disputes(con: duckdb.DuckDBPyConnection) -> list[str]:
    f = dispute_figures(con)
    total, disputes = f["rows"]
    by_sub = con.execute(
        f"SELECT subcategory, count(*) FROM silver_complaints WHERE {DISPUTES} GROUP BY 1 "
        "ORDER BY 1"
    ).fetchall()

    def both(key: str) -> list[str]:
        a, d = f[key]
        return [f"{_n(a)} ({_pct(a, total)})", f"{_n(d)} ({_pct(d, disputes)})"]

    rows = [
        ["Complaints", _n(total), f"{_n(disputes)} ({_pct(disputes, total)} of all)", "none"],
        ["With claimed amount", *both("with_amount"), "`claimed_amount IS NOT NULL`"],
        [
            "With affected product (presence)",
            *both("with_product"),
            "`affected_product_id IS NOT NULL`",
        ],
        [
            "With amount and product",
            *both("with_both"),
            "both of the above",
        ],
        ["With origin interaction", *both("with_origin"), "`origin_interaction_id IS NOT NULL`"],
        ["SLA breached", *both("sla_breached"), "`sla_breached`"],
        [
            "Median resolution days",
            f"{_num(f['median_resolution_days'][0], 1)} (n = {_n(f['with_resolution_days'][0])})",
            f"{_num(f['median_resolution_days'][1], 1)} (n = {_n(f['with_resolution_days'][1])})",
            "`resolution_days IS NOT NULL`",
        ],
    ]
    has_transaction = "transaction_id" in BY_TABLE["complaints"].header
    return [
        "Dispute complaints are the complaints whose `subcategory` is "
        + " or ".join(f"'{s}'" for s in DISPUTE_SUBCATEGORIES)
        + ": "
        + ", ".join(f"{s} {_n(n)}" for s, n in by_sub)
        + ".",
        "",
        *_table(["Measure", "All complaints", "Dispute complaints", "Filter"], rows),
        "",
        f"Source: `silver/complaints`. The All complaints column has no filter beyond the one in "
        f"the Filter column; the Dispute complaints column adds `{DISPUTES}`. Each rate is over "
        "the rows of its own column: the SLA rate of all complaints is over every complaint, "
        "the one of disputes over dispute complaints only.",
        "",
        "The affected product is counted as presence of the field only: every non-null "
        "`affected_product_id` points to a product of another customer (alert "
        "`alert_product_not_owned`, calidad.md), so it is never used as a link or as ground "
        "truth.",
        "",
        "Complaints "
        + ("carry" if has_transaction else "have no column for")
        + f" a transaction id, and `origin_interaction_id` links {_n(f['with_origin'][0])} of "
        f"{_n(total)} complaints to their call: a dispute cannot be traced to the charge it "
        "disputes from this table alone.",
    ]


def _merge(*series: list[tuple]) -> list[list[object]]:
    merged: dict[object, list[int]] = defaultdict(lambda: [0] * len(series))
    for i, rows in enumerate(series):
        for key, n in rows:
            merged[key][i] = n
    totals = [sum(v[i] for v in merged.values()) for i in range(len(series))]
    return [
        [key, *(f"{_n(v)} ({_pct(v, t)})" for v, t in zip(values, totals, strict=True))]
        for key, values in sorted(merged.items(), key=lambda kv: str(kv[0]))
    ]


def hour_spread(rows: list[tuple]) -> tuple[float, float]:
    """Smallest and largest share of a day's demand that falls in one hour.

    Args:
        rows: (hour, count) over the 24 hours; a missing hour counts as zero.

    Returns:
        (minimum share, maximum share), as fractions.
    """
    counts = dict(rows)
    total = sum(counts.values())
    shares = [counts.get(f"{h:02d}", 0) / total for h in range(24)] if total else [0.0]
    return min(shares), max(shares)


def hour_finding(contacts: list[tuple], disputes: list[tuple]) -> str:
    """States whether demand is flat over the 24 hours of the day.

    Args:
        contacts: (hour, complaint contacts).
        disputes: (hour, dispute complaints).

    Returns:
        One paragraph with the range of hourly shares of both series.
    """
    spreads = [hour_spread(contacts), hour_spread(disputes)]
    flat = all(hi <= FLAT_HOUR_RATIO * lo for lo, hi in spreads)
    ranges = (
        f"complaint contacts {100 * spreads[0][0]:.2f}% to {100 * spreads[0][1]:.2f}% per "
        f"hour, dispute complaints {100 * spreads[1][0]:.2f}% to {100 * spreads[1][1]:.2f}%"
        f"; a flat day is {100 / 24:.2f}%"
    )
    if flat:
        return (
            f"**Finding: demand is spread evenly over the 24 hours** ({ranges}), night hours "
            "included. A bank's contact demand peaks in business hours; this flat profile is an "
            "artifact of the synthetic generator."
        )
    return f"Hourly demand is not flat: {ranges}."


def _patterns(con: duckdb.DuckDBPyConnection, data_dir: Path) -> list[str]:
    mart = _mart(data_dir, "demand_interactions")
    where = f"reason_category = {sql_str(COMPLAINT_CATEGORY)}"

    def contacts(column: str) -> list[tuple]:
        return con.execute(
            f"SELECT {column}, sum(contacts) FROM {mart} WHERE {where} GROUP BY 1"
        ).fetchall()

    def disputes(expr: str) -> list[tuple]:
        return con.execute(
            f"SELECT {expr}, count(*) FROM silver_complaints WHERE {DISPUTES} GROUP BY 1"
        ).fetchall()

    def weekday(rows: list[tuple]) -> list[tuple]:
        return [(f"{d} {WEEKDAYS[d]}", n) for d, n in rows]

    def hour(rows: list[tuple]) -> list[tuple]:
        return [(f"{h:02d}", n) for h, n in rows]

    header = ["", "Complaint contacts", "Dispute complaints"]
    by_hour_contacts = hour(contacts("hour"))
    by_hour_disputes = hour(disputes("hour(timezone(timezone, creation_date))"))
    first, last = con.execute(
        "SELECT min(partition_date), max(partition_date) FROM silver_complaints"
    ).fetchone() or (None, None)
    lines = [
        f"Complaint contacts: `gold/demand_interactions`, `reason_category = "
        f"'{COMPLAINT_CATEGORY}'`, sum of `contacts`. Dispute complaints: {DISPUTE_FILTER}, "
        "count of rows. Month, weekday and hour are local to the customer's country "
        "(`event_date_local` and the event time in its zone), not the partition: see calidad.md, "
        "Time zone. Each share is over its own column.",
        "",
        "### By month",
        "",
        f"Partitions run from {first} to {last}, so the first and last months are partial.",
        "",
        *_table(
            ["Month", *header[1:]],
            _merge(
                [(str(m), n) for m, n in contacts("strftime(month, '%Y-%m')")],
                disputes("strftime(event_date_local, '%Y-%m')"),
            ),
        ),
        "",
        "### By weekday",
        "",
        *_table(
            ["Weekday", *header[1:]],
            _merge(weekday(contacts("weekday")), weekday(disputes("isodow(event_date_local)"))),
        ),
        "",
        "### By hour of the day",
        "",
        *_table(["Hour", *header[1:]], _merge(by_hour_contacts, by_hour_disputes)),
        "",
        hour_finding(by_hour_contacts, by_hour_disputes),
        "",
        "### By country",
        "",
        *_table(
            ["Country", *header[1:]], _merge(contacts("country_code"), disputes("country_code"))
        ),
        "",
        "### By channel",
        "",
        "Contacts and complaints use different channel lists (`channel` of the interaction, "
        "`reception_channel` of the complaint), so they are shown apart.",
        "",
        *_table(["Contact channel", "Complaint contacts"], _merge(contacts("channel"))),
        "",
        *_table(["Reception channel", "Dispute complaints"], _merge(disputes("reception_channel"))),
    ]
    return lines


def _contact_limits(con: duckdb.DuckDBPyConnection, data_dir: Path) -> list[str]:
    rows = con.execute(
        f"""
        SELECT coalesce(reason_category, 'All'), sum(contacts),
               sum(wait_seconds) / nullif(sum(contacts_with_wait), 0),
               sum(duration_seconds) / nullif(sum(contacts_with_duration), 0) / 60,
               sum(escalated) / sum(contacts),
               sum(followup) / sum(contacts)
        FROM {_mart(data_dir, "demand_interactions")}
        GROUP BY ROLLUP (reason_category) ORDER BY reason_category IS NULL, 2 DESC
        """
    ).fetchall()
    return _table(
        [
            "Category",
            "Contacts",
            "Mean wait (s)",
            "Mean duration (min)",
            "Escalated",
            "Requires follow-up",
        ],
        [
            [c, _n(n), _num(wait, 1), _num(minutes), f"{100 * esc:.2f}%", f"{100 * fu:.2f}%"]
            for c, n, wait, minutes, esc, fu in rows
        ],
    ) + [
        "",
        "Source: `gold/demand_interactions`, grouped by `reason_category`, no filter. Mean wait = "
        "`wait_seconds` / `contacts_with_wait` and mean duration = `duration_seconds` / "
        "`contacts_with_duration`: interaction types without a wait or a duration are left out. "
        "Escalated = `was_escalated`. The marts keep sums, not distributions, so only means are "
        "shown for contacts.",
    ]


def _complaint_limits(con: duckdb.DuckDBPyConnection) -> list[str]:
    select = """
        count(*), count(*) FILTER (WHERE sla_breached), count(resolution_days),
        median(resolution_days), quantile_cont(resolution_days, 0.9), avg(resolution_days)
    """
    rows = con.execute(
        f"""
        SELECT 'All complaints', 'All', {select} FROM silver_complaints
        UNION ALL
        SELECT 'Dispute complaints', 'All', {select} FROM silver_complaints WHERE {DISPUTES}
        UNION ALL
        SELECT * FROM (SELECT 'Dispute complaints', country_code, {select}
                       FROM silver_complaints WHERE {DISPUTES} GROUP BY 2 ORDER BY 2)
        """
    ).fetchall()
    return _table(
        [
            "Complaints",
            "Country",
            "Rows",
            "SLA breached",
            "With resolution days",
            "Median days",
            "P90 days",
            "Mean days",
        ],
        [
            [
                label,
                country,
                _n(n),
                f"{_n(breached)} ({_pct(breached, n)})",
                _n(resolved),
                _num(median, 1),
                _num(p90, 1),
                _num(mean, 1),
            ]
            for label, country, n, breached, resolved, median, p90, mean in rows
        ],
    ) + [
        "",
        f"Source: `silver/complaints`; All complaints has no filter, Dispute complaints adds "
        f"`{DISPUTES}`. SLA breached = `sla_breached`, over the rows of the line. Resolution "
        "days = `resolution_days`, only set on resolved or closed complaints.",
    ]


def render(con: duckdb.DuckDBPyConnection, data_dir: Path, header: list[str]) -> str:
    """Renders the demand report.

    Args:
        con: DuckDB connection with the `silver_complaints` view registered.
        data_dir: Root data directory, holding the gold marts.
        header: Run lines shown at the top (version, batch, seed).

    Returns:
        Markdown text.
    """
    lines = [
        "# Demand",
        "",
        "Generated by `make report-data` (story TRZ-06; design §2.1). Counts and rates only: no "
        "row with personal fields is shown. Each table names its source and filter; `gold/` and "
        "`silver/` are the layers under DATA_DIR.",
        "",
        "## Run",
        "",
        *header,
        "",
        "## Contacts by category",
        "",
        *_categories(con, data_dir),
        "",
        "## Dispute complaints",
        "",
        *_disputes(con),
        "",
        "## Demand patterns: complaint contacts and dispute complaints",
        "",
        *_patterns(con, data_dir),
        "",
        "## Operational constraints",
        "",
        "### Contacts",
        "",
        *_contact_limits(con, data_dir),
        "",
        "### Complaints",
        "",
        *_complaint_limits(con),
    ]
    return "\n".join(lines) + "\n"

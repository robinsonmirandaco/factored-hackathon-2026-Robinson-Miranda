"""Serving cohort (TRZ-07, design 9.5): the customers loaded into Postgres, with all their history.

The cohort is stratified by country and segment with proportional allocation, so its mix matches
the population. Seats are given by the largest remainder method, which makes the sizes add up to
exactly the requested total. Inside a stratum, customers are ordered by md5(seed:customer_id)
and the first ones are taken: the choice depends only on the seed and the ids, never on row
order, so the same gold gives the same cohort.

The evaluation splits (TRZ-42) are drawn from this cohort, so every customer they name is loaded;
`missing_from_cohort` is the check.
"""

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import duckdb

from pipeline.silver import PARQUET, sql_str

Stratum = tuple[str, str]


@dataclass(frozen=True)
class CohortResult:
    """What the cohort step selected and wrote.

    Attributes:
        requested: Cohort size asked for.
        seed: Seed of the ordering inside each stratum.
        population: Customers per (country_code, segment) in gold.
        cohort: Customers per (country_code, segment) in the cohort.
        rows: Rows written per cohort table.
        ids_sha256: SHA-256 of the sorted cohort customer ids, one per line.
        without_products: Cohort customers with no product.
        without_transactions: Cohort customers with no transaction.
    """

    requested: int
    seed: int
    population: dict[Stratum, int]
    cohort: dict[Stratum, int]
    rows: dict[str, int]
    ids_sha256: str
    without_products: int
    without_transactions: int


def allocate(population: dict[Stratum, int], size: int) -> dict[Stratum, int]:
    """Splits a cohort size across strata in proportion to their population.

    Integer arithmetic keeps the remainders exact; ties go to the stratum that sorts first, so
    the result is deterministic. A size larger than the population takes everyone.

    Args:
        population: Customers per stratum.
        size: Cohort size.

    Returns:
        Seats per stratum, adding up to min(size, total population).
    """
    total = sum(population.values())
    size = min(size, total)
    seats = {k: n * size // total for k, n in population.items()}
    remainder = {k: n * size % total for k, n in population.items()}
    left = size - sum(seats.values())
    for k in sorted(population, key=lambda k: (-remainder[k], k))[:left]:
        seats[k] += 1
    return seats


def missing_from_cohort(cohort_ids: Iterable[str], split_ids: Iterable[str]) -> list[str]:
    """Returns the customers of an evaluation split that are not in the cohort.

    Args:
        cohort_ids: Customer ids of the cohort.
        split_ids: Customer ids named by a split.

    Returns:
        The missing ids, sorted; empty when the split lies inside the cohort.
    """
    return sorted(set(split_ids) - set(cohort_ids))


def _queries(gold: Path) -> dict[str, str]:
    def src(name: str) -> str:
        return f"read_parquet({sql_str(str(gold / f'{name}.parquet'))})"

    ids = "customer_id IN (SELECT customer_id FROM cohort_ids)"
    # Postgres keeps bank timestamps as naive local time, like TRAZO_NOW, so zoned values go
    # back to the wall time of the customer's country.
    local = "timezone(timezone, {0}) AS {0}"
    complaint_times = ", ".join(
        local.format(c)
        for c in (
            "creation_date",
            "assignment_date",
            "first_response_date",
            "resolution_date",
            "closing_date",
        )
    )
    return {
        "customers": f"SELECT * FROM {src('service_customers')} WHERE {ids} ORDER BY customer_id",
        "products": f"SELECT * FROM {src('service_products')} WHERE {ids} ORDER BY product_id",
        # TRAZO does not score risk (design 11.3), and is_fraud is the answer to the very
        # question a disputing customer asks, so neither reaches the service.
        "transactions": f"""
            SELECT * EXCLUDE (is_fraud, fraud_score, transaction_country_source)
                     REPLACE ({local.format("transaction_date")})
            FROM {src("service_transactions")} WHERE {ids} ORDER BY transaction_id
        """,
        "complaints": f"""
            SELECT * REPLACE ({complaint_times})
            FROM {src("service_complaints")} WHERE {ids} ORDER BY complaint_id
        """,
        "exchange_rates": f"""
            SELECT * FROM {src("service_exchange_rates")}
            ORDER BY date, source_currency, target_currency
        """,
    }


def build_cohort(
    con: duckdb.DuckDBPyConnection, data_dir: Path, size: int, seed: int
) -> CohortResult:
    """Selects the cohort from gold and writes its tables under `DATA_DIR/gold/cohort`.

    Args:
        con: DuckDB connection with its time zone set to UTC.
        data_dir: Root data directory; gold must be built.
        size: Cohort size.
        seed: Seed of the ordering inside each stratum.

    Returns:
        The selection and the rows written.
    """
    gold = data_dir / "gold"
    customers = f"read_parquet({sql_str(str(gold / 'service_customers.parquet'))})"
    population = {
        (c, s): n
        for c, s, n in con.execute(
            f"SELECT country_code, segment, count(*) FROM {customers} GROUP BY ALL"
        ).fetchall()
    }
    seats = allocate(population, size)
    con.execute("CREATE OR REPLACE TEMP TABLE seats (country_code VARCHAR, segment VARCHAR, n INT)")
    con.executemany(
        "INSERT INTO seats VALUES (?, ?, ?)", [(*k, n) for k, n in sorted(seats.items())]
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE cohort_ids AS
        SELECT c.customer_id, c.country_code, c.segment
        FROM (
            SELECT customer_id, country_code, segment,
                   row_number() OVER (
                       PARTITION BY country_code, segment
                       ORDER BY md5({sql_str(f"{int(seed)}:")} || customer_id)
                   ) AS rn
            FROM {customers}
        ) c
        JOIN seats s USING (country_code, segment)
        WHERE c.rn <= s.n
        """
    )
    folder = gold / "cohort"
    folder.mkdir(parents=True, exist_ok=True)
    rows = {}
    for name, query in _queries(gold).items():
        target = folder / f"{name}.parquet"
        con.execute(f"COPY ({query}) TO {sql_str(str(target))} ({PARQUET})")
        rows[name] = con.execute(f"SELECT count(*) FROM {sql_str(str(target))}").fetchone()[0]

    ids = [r[0] for r in con.execute("SELECT customer_id FROM cohort_ids ORDER BY 1").fetchall()]
    cohort = {
        (c, s): n
        for c, s, n in con.execute(
            "SELECT country_code, segment, count(*) FROM cohort_ids GROUP BY ALL"
        ).fetchall()
    }

    def without(table: str) -> int:
        path = sql_str(str(folder / f"{table}.parquet"))
        return con.execute(
            f"SELECT count(*) FROM cohort_ids WHERE customer_id NOT IN "
            f"(SELECT customer_id FROM read_parquet({path}))"
        ).fetchone()[0]

    return CohortResult(
        requested=size,
        seed=seed,
        population=population,
        cohort=cohort,
        rows=rows,
        ids_sha256=hashlib.sha256("\n".join(ids).encode()).hexdigest(),
        without_products=without("products"),
        without_transactions=without("transactions"),
    )


def _pct(n: int, total: int) -> float:
    return 100 * n / total if total else 0.0


def _share_rows(result: CohortResult, group: Callable[[Stratum], str]) -> tuple[list[str], float]:
    pop_total = sum(result.population.values())
    coh_total = sum(result.cohort.values())
    lines, worst = [], 0.0
    for label in sorted({group(k) for k in result.population}):
        p = sum(n for k, n in result.population.items() if group(k) == label)
        c = sum(n for k, n in result.cohort.items() if group(k) == label)
        # Adding 0.0 turns a rounded -0.0 into 0.0, so the table never prints -0.00.
        diff = round(_pct(c, coh_total) - _pct(p, pop_total), 2) + 0.0
        worst = max(worst, abs(diff))
        lines.append(
            f"| {label} | {p:,} | {_pct(p, pop_total):.2f}% | {c:,} | {_pct(c, coh_total):.2f}% "
            f"| {diff:+.2f} |"
        )
    return lines, worst


def render_report(result: CohortResult, header: list[str]) -> str:
    """Renders docs/reports/cohorte.md. Only counts and shares; no customer data.

    Args:
        result: Output of build_cohort.
        header: Run lines shared with the quality report (pipeline version, batch id, seed).

    Returns:
        The Markdown report.
    """
    size = sum(result.cohort.values())
    joint, worst_joint = _share_rows(result, " | ".join)
    countries, worst_country = _share_rows(result, lambda k: k[0])
    segments, worst_segment = _share_rows(result, lambda k: k[1])
    head = "| Population | Population share | Cohort | Cohort share | Difference (points) |"
    rule = "| ---: | ---: | ---: | ---: | ---: |"
    lines = [
        "# Serving cohort",
        "",
        "Customers loaded into the serving database (story TRZ-07, design 9.5).",
        "",
        "## Run",
        "",
        *header,
        f"- Requested size: {result.requested:,}",
        f"- Population (gold/service_customers): {sum(result.population.values()):,}",
        f"- Cohort size: {size:,}",
        f"- SHA-256 of the sorted cohort customer ids: `{result.ids_sha256}`",
        "",
        "## Method",
        "",
        "- Strata: country (`country_code`) by segment (`segment`) of gold/service_customers.",
        "- Proportional allocation; seats rounded with the largest remainder method, so the",
        "  strata add up to the cohort size exactly.",
        "- Inside a stratum, customers are ordered by md5 of `<seed>:<customer_id>` and the first",
        "  ones are taken. No filter on customer status, products or activity (plan A, TRZ-01).",
        "- Every cohort customer comes with all their products, transactions and complaints;",
        "  exchange rates are loaded whole.",
        "",
        "## Country by segment",
        "",
        "| Country | Segment " + head,
        "| --- | --- " + rule,
        *joint,
        "",
        "## By country",
        "",
        "| Country " + head,
        "| --- " + rule,
        *countries,
        "",
        "## By segment",
        "",
        "| Segment " + head,
        "| --- " + rule,
        *segments,
        "",
        f"Largest difference against the population: {worst_joint:.2f} points by stratum, "
        f"{worst_country:.2f} by country, {worst_segment:.2f} by segment (limit: 2 points).",
        "",
        "## Rows per table",
        "",
        "Source: the gold serving tables, filtered to the cohort customers (`gold/cohort/`).",
        "",
        "| Table | Rows |",
        "| --- | ---: |",
        *(f"| {t} | {n:,} |" for t, n in result.rows.items()),
        "",
        f"Cohort customers with no product: {result.without_products:,}. "
        f"With no transaction: {result.without_transactions:,}.",
        "",
        "## Evaluation splits",
        "",
        "The splits of TRZ-42 are drawn from gold/cohort/customers.parquet, so every customer",
        "they name is loaded. `pipeline.cohort.missing_from_cohort` is the check, run by the",
        "unit tests.",
        "",
    ]
    return "\n".join(lines)

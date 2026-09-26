"""Gold layer: demand marts, the case generator input and the serving tables, built from silver.

Gold is rebuilt in full on every run from the `silver_<table>` views; each file is written in a
fixed order so the same silver gives byte-identical gold. The serving tables cover the whole
population; picking the serving cohort is a separate step (TRZ-07).

Row-level tables carry the lineage of their silver row. Demand marts aggregate many rows, so each
group carries its silver table as `source_file`, its latest partition and ingestion time, and the
batch and pipeline version of the run that built it.
"""

from pathlib import Path

import duckdb

from pipeline.density import DISPUTABLE_STATUSES, DISPUTABLE_TYPES
from pipeline.silver import PARQUET, Lineage, sql_str

ROW_LINEAGE = "source_file, partition_date, batch_id, ingested_at, pipeline_version"


def _group_lineage(table: str, alias: str, lineage: Lineage) -> str:
    return (
        f"{sql_str('silver/' + table)} AS source_file, "
        f"max({alias}.partition_date) AS partition_date, "
        f"{sql_str(lineage.batch_id)} AS batch_id, max({alias}.ingested_at) AS ingested_at, "
        f"{sql_str(lineage.pipeline_version)} AS pipeline_version"
    )


def gold_queries(lineage: Lineage) -> dict[str, str]:
    """Returns the query of every gold table.

    Args:
        lineage: Batch id and pipeline version of the run, stamped on aggregated rows.

    Returns:
        SQL per gold table name.
    """
    return {
        "demand_interactions": f"""
            SELECT i.country_code, i.channel, i.interaction_type, i.reason_category,
                   date_trunc('month', i.event_date_local)::DATE AS month,
                   isodow(i.event_date_local) AS weekday,
                   hour(timezone(i.timezone, i.interaction_date)) AS hour,
                   count(*) AS contacts,
                   sum(i.duration_seconds) AS duration_seconds,
                   count(i.duration_seconds) AS contacts_with_duration,
                   sum(i.wait_time_seconds) AS wait_seconds,
                   count(i.wait_time_seconds) AS contacts_with_wait,
                   count(*) FILTER (WHERE i.was_resolved) AS resolved,
                   count(*) FILTER (WHERE i.requires_followup) AS followup,
                   count(*) FILTER (WHERE i.was_escalated) AS escalated,
                   count(s.main_score) AS csat_responses,
                   sum(s.main_score) AS csat_score_sum,
                   {_group_lineage("call_center_interactions", "i", lineage)}
            FROM silver_call_center_interactions i
            LEFT JOIN silver_satisfaction_surveys s
              ON s.interaction_id = i.interaction_id AND s.survey_type = 'CSAT'
            GROUP BY i.country_code, i.channel, i.interaction_type, i.reason_category,
                     month, weekday, hour
            ORDER BY ALL
        """,
        "demand_complaints": f"""
            SELECT c.country_code, c.reception_channel, c.case_type, c.category,
                   date_trunc('month', c.event_date_local)::DATE AS month,
                   isodow(c.event_date_local) AS weekday,
                   hour(timezone(c.timezone, c.creation_date)) AS hour,
                   count(*) AS complaints,
                   count(c.claimed_amount) AS with_amount,
                   count(c.affected_product_id) AS with_product,
                   count(*) FILTER (WHERE c.claimed_amount IS NOT NULL
                                      AND c.affected_product_id IS NOT NULL)
                       AS with_amount_and_product,
                   count(*) FILTER (WHERE c.sla_breached) AS sla_breached,
                   sum(c.resolution_days) AS resolution_days_sum,
                   count(c.resolution_days) AS with_resolution_days,
                   {_group_lineage("complaints", "c", lineage)}
            FROM silver_complaints c
            GROUP BY c.country_code, c.reception_channel, c.case_type, c.category,
                     month, weekday, hour
            ORDER BY ALL
        """,
        "case_generator_input": f"""
            SELECT t.transaction_id, t.customer_id, t.product_id, t.transaction_date,
                   t.event_date_local, t.timezone, t.country_code, c.segment, p.product_type,
                   p.currency AS product_currency, t.transaction_type, t.transaction_status,
                   t.channel, t.amount, t.currency, t.amount_usd, t.merchant_name,
                   t.merchant_category, t.transaction_country, t.transaction_city, t.is_fraud,
                   t.source_file, t.partition_date, t.batch_id, t.ingested_at, t.pipeline_version
            FROM silver_transactions t
            JOIN silver_customers c ON c.customer_id = t.customer_id
            JOIN silver_products p ON p.product_id = t.product_id
            WHERE t.transaction_type IN {DISPUTABLE_TYPES}
              AND t.transaction_status IN {DISPUTABLE_STATUSES}
            ORDER BY t.transaction_id
        """,
        "service_customers": f"""
            SELECT customer_id, document_type, document_number, first_name, country,
                   country_code, timezone, segment, customer_status, {ROW_LINEAGE}
            FROM silver_customers
            ORDER BY customer_id
        """,
        # Only the last four digits of a product number leave silver.
        "service_products": f"""
            SELECT product_id, customer_id, product_type,
                   right(product_number, 4) AS product_number_last4,
                   currency, current_balance, credit_limit, opening_date, expiration_date,
                   product_status, {ROW_LINEAGE}
            FROM silver_products
            ORDER BY product_id
        """,
        "service_transactions": """
            SELECT * EXCLUDE (latitude, longitude)
            FROM silver_transactions
            ORDER BY transaction_id
        """,
        "service_exchange_rates": """
            SELECT * FROM silver_daily_exchange_rates
            ORDER BY date, source_currency, target_currency
        """,
    }


def build_gold(con: duckdb.DuckDBPyConnection, data_dir: Path, lineage: Lineage) -> dict[str, int]:
    """Writes every gold table as one Parquet file under `DATA_DIR/gold`.

    Args:
        con: DuckDB connection with the `silver_<table>` views registered.
        data_dir: Root data directory.
        lineage: Batch id and pipeline version of the run.

    Returns:
        Row count per gold table.
    """
    folder = data_dir / "gold"
    folder.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name, query in gold_queries(lineage).items():
        target = folder / f"{name}.parquet"
        con.execute(f"COPY ({query}) TO {sql_str(str(target))} ({PARQUET})")
        row = con.execute(f"SELECT count(*) FROM read_parquet({sql_str(str(target))})").fetchone()
        counts[name] = row[0] if row else 0
    return counts

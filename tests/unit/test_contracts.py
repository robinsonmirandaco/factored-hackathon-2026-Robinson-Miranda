"""Contract rules, alerts and quarantine on the synthetic dataset (TRZ-03)."""

from datetime import date
from pathlib import Path

import duckdb
import pytest

from pipeline.contracts import BY_TABLE, CONTRACTS
from tests.pipeline_data import (
    Rows,
    build_dataset,
    partition_path,
    run_pipeline,
    write_csv,
    write_dataset,
)

DAYS = [date(2024, 1, 10), date(2024, 1, 11)]


def _query(sql: str) -> list[tuple]:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    return con.execute(sql).fetchall()


def _violations(data_dir: Path, table: str) -> set[tuple[str, str]]:
    folder = data_dir / "quarantine" / table
    glob = "*/*.parquet" if BY_TABLE[table].partitioned else "data.parquet"
    if not any(folder.rglob("*.parquet")):
        return set()
    rows = _query(
        f"SELECT v.rule, coalesce(v.column_name, '') "
        f"FROM (SELECT unnest(violations) AS v FROM read_parquet('{folder / glob}'))"
    )
    return set(rows)


def _silver(data_dir: Path, table: str, columns: str, order: str) -> list[tuple]:
    glob = "*/*.parquet" if BY_TABLE[table].partitioned else "data.parquet"
    return _query(
        f"SELECT {columns} FROM read_parquet('{data_dir / 'silver' / table / glob}') "
        f"ORDER BY {order}"
    )


def _run_with(tmp_path: Path, edit: dict[str, dict[int, dict[str, str]]]) -> Path:
    rows: Rows = build_dataset(DAYS)
    for table, by_index in edit.items():
        for index, fields in by_index.items():
            rows[table][index].update(fields)
    write_dataset(tmp_path, rows)
    run_pipeline(tmp_path)
    return tmp_path


RULE_CASES = [
    ("complaints", 0, {"status": ""}, "missing_required", "status"),
    ("transactions", 0, {"amount": "abc"}, "invalid_type", "amount"),
    ("transactions", 0, {"channel": "Fax"}, "invalid_domain", "channel"),
    ("products", 0, {"product_type": "Tarjeta Prepago"}, "unmapped_value", "product_type"),
    ("transactions", 0, {"transaction_country": "Narnia"}, "unmapped_value", "transaction_country"),
    (
        "call_center_interactions",
        0,
        {"process_date": "2024-01-11"},
        "process_date_mismatch",
        "process_date",
    ),
    (
        "transactions",
        0,
        {"transaction_date": "2024-01-10 05:59:59"},
        "event_outside_partition",
        "transaction_date",
    ),
    (
        "complaints",
        0,
        {"creation_date": "2024-01-11 08:00:01"},
        "event_outside_partition",
        "creation_date",
    ),
    ("transactions", 1, {"transaction_id": "TRX-20240110-1"}, "duplicate_key", "transaction_id"),
    (
        "transactions",
        1,
        {"customer_id": "CUS-1", "product_id": "PRD-1"},
        "duplicate_business_key",
        "customer_id,product_id,transaction_date,amount,merchant_name",
    ),
    ("call_center_interactions", 0, {"agent_id": "AGT-9"}, "missing_reference", "agent_id"),
    ("transactions", 0, {"product_id": "PRD-2"}, "missing_reference", "product_id,customer_id"),
    ("transactions", 0, {"amount": "-5.00"}, "not_positive", "amount"),
    ("transactions", 0, {"fraud_score": "150"}, "out_of_range", "fraud_score"),
    ("call_center_interactions", 0, {"wait_time_seconds": "-1"}, "negative", "wait_time_seconds"),
    ("call_center_interactions", 0, {"sentiment_score": "1.5"}, "out_of_range", "sentiment_score"),
    ("complaints", 0, {"assignment_date": "2024-01-10 10:00:00"}, "date_order", "assignment_date"),
    ("complaints", 0, {"claimed_amount": "0"}, "not_positive", "claimed_amount"),
    ("complaints", 0, {"resolution_satisfaction": "6"}, "out_of_range", "resolution_satisfaction"),
    ("satisfaction_surveys", 0, {"main_score": "9"}, "out_of_range", "main_score"),
    (
        "satisfaction_surveys",
        0,
        {"question_1_response": "7"},
        "out_of_range",
        "question_1_response",
    ),
    (
        "satisfaction_surveys",
        0,
        {"survey_date": "2024-01-10 08:30:00"},
        "survey_before_interaction",
        "survey_date",
    ),
    (
        "satisfaction_surveys",
        0,
        {"interaction_id": "INT-20240111"},
        "partition_mismatch",
        "interaction_id",
    ),
    (
        "satisfaction_surveys",
        0,
        {"customer_id": "CUS-2"},
        "missing_reference",
        "interaction_id,customer_id,agent_id",
    ),
    ("call_transcripts", 0, {"accent_confidence": "1.2"}, "out_of_range", "accent_confidence"),
    ("call_transcripts", 0, {"duration_seconds": "-3"}, "negative", "duration_seconds"),
    ("customers", 0, {"credit_score": "900"}, "out_of_range", "credit_score"),
    ("customers", 0, {"date_of_birth": "2021-01-01"}, "date_order", "date_of_birth"),
    ("customers", 0, {"estimated_monthly_income": "0"}, "not_positive", "estimated_monthly_income"),
    ("products", 0, {"interest_rate": "120"}, "out_of_range", "interest_rate"),
    ("products", 0, {"expiration_date": "2020-01-01"}, "date_order", "expiration_date"),
    ("products", 0, {"credit_limit": "0"}, "not_positive", "credit_limit"),
    ("products", 0, {"current_balance": "-1"}, "negative", "current_balance"),
    ("products", 0, {"days_past_due": "-2"}, "negative", "days_past_due"),
    ("products", 0, {"customer_id": "CUS-9"}, "missing_reference", "customer_id"),
    ("branches", 0, {"closing_time": "07:00:00"}, "date_order", "closing_time"),
    ("branches", 0, {"atm_count": "-1"}, "negative", "atm_count"),
    ("service_agents", 0, {"avg_csat": "6"}, "out_of_range", "avg_csat"),
    (
        "service_agents",
        0,
        {"total_monthly_interactions": "-1"},
        "negative",
        "total_monthly_interactions",
    ),
    ("daily_exchange_rates", 0, {"exchange_rate": "0"}, "not_positive", "exchange_rate"),
    ("daily_exchange_rates", 0, {"buy_rate": "5000.0"}, "buy_above_sell", "buy_rate"),
    ("daily_exchange_rates", 0, {"target_currency": "USD"}, "same_currency", "target_currency"),
]


@pytest.mark.parametrize(("table", "index", "fields", "rule", "column"), RULE_CASES)
def test_row_that_breaks_a_rule_goes_to_quarantine(
    tmp_path: Path, table: str, index: int, fields: dict[str, str], rule: str, column: str
) -> None:
    data_dir = _run_with(tmp_path, {table: {index: fields}})

    assert (rule, column) in _violations(data_dir, table)


def test_transcript_of_an_interaction_without_transcript_goes_to_quarantine(
    tmp_path: Path,
) -> None:
    data_dir = _run_with(tmp_path, {"call_center_interactions": {0: {"has_transcript": "False"}}})

    assert ("transcript_not_flagged", "interaction_id") in _violations(data_dir, "call_transcripts")


def test_every_contract_rule_has_a_test_case() -> None:
    tested = {rule for *_, rule, _ in RULE_CASES} | {"transcript_not_flagged"}
    declared = {check.rule for contract in CONTRACTS for check in contract.checks}

    assert declared <= tested


def test_clean_dataset_has_no_quarantine(tmp_path: Path) -> None:
    data_dir = _run_with(tmp_path, {})

    assert not (data_dir / "quarantine").exists() or not any(
        (data_dir / "quarantine").rglob("*.parquet")
    )


def test_document_and_currency_incoherence_is_flagged_not_rejected(tmp_path: Path) -> None:
    data_dir = _run_with(tmp_path, {})

    assert _silver(data_dir, "customers", "customer_id, alert_document_country", "1") == [
        ("CUS-1", False),
        ("CUS-2", False),
        ("CUS-3", True),
    ]
    flagged = _silver(
        data_dir,
        "transactions",
        "DISTINCT customer_id, currency, alert_currency_country",
        "1",
    )
    assert flagged == [("CUS-1", "ARS", False), ("CUS-2", "COP", False), ("CUS-3", "USD", True)]


def test_unresolved_branch_and_foreign_product_are_flagged(tmp_path: Path) -> None:
    data_dir = _run_with(
        tmp_path,
        {
            "service_agents": {0: {"assigned_branch_id": "SUC-404"}},
            "complaints": {0: {"affected_product_id": "PRD-2"}},
        },
    )

    agents = _silver(data_dir, "service_agents", "agent_id, alert_branch_unresolved", "1")
    assert agents == [("AGT-1", True), ("AGT-2", False)]
    complaints = _silver(data_dir, "complaints", "complaint_id, alert_product_not_owned", "1")
    assert complaints == [("CMP-20240110", True), ("CMP-20240111", False)]


def test_amount_without_currency_is_flagged_not_rejected(tmp_path: Path) -> None:
    data_dir = _run_with(tmp_path, {"complaints": {0: {"currency": ""}}})

    got = _silver(data_dir, "complaints", "complaint_id, alert_amount_currency_mismatch", "1")
    assert got == [("CMP-20240110", True), ("CMP-20240111", False)]


def test_dimension_duplicates_by_business_key_are_flagged_and_kept(tmp_path: Path) -> None:
    data_dir = _run_with(tmp_path, {"products": {1: {"product_number": "123456781"}}})

    got = _silver(data_dir, "products", "product_id, alert_duplicate_business_key", "1")
    assert got == [("PRD-1", True), ("PRD-2", True), ("PRD-3", False)]


def test_product_type_and_country_are_normalized_keeping_the_source(tmp_path: Path) -> None:
    data_dir = _run_with(tmp_path, {"transactions": {0: {"transaction_country": "Mexico"}}})

    products = _silver(data_dir, "products", "DISTINCT product_type, product_type_source", "1")
    assert products == [("savings_account", "Cuenta Ahorro")]
    countries = _silver(
        data_dir,
        "transactions",
        "DISTINCT transaction_country, transaction_country_source",
        "1, 2",
    )
    assert countries == [
        ("AR", "Argentina"),
        ("CO", "Colombia"),
        ("MX", "Mexico"),
        ("MX", "México"),
    ]


def test_header_change_sends_the_whole_partition_to_quarantine(tmp_path: Path) -> None:
    rows = build_dataset(DAYS)
    write_dataset(tmp_path, rows)
    contract = BY_TABLE["complaints"]
    day_rows = [
        dict(r, channel_v2="x") for r in rows["complaints"] if r["_partition"] == "2024-01-11"
    ]
    write_csv(
        partition_path(tmp_path / "raw", "complaints", DAYS[1]),
        [*contract.header, "channel_v2"],
        day_rows,
    )

    result = run_pipeline(tmp_path)

    complaints = next(t for t in result.tables if t.table == "complaints")
    assert (complaints.bronze_rows, complaints.silver_rows, complaints.quarantine_rows) == (2, 1, 1)
    got = _query(
        f"""
        SELECT partition_date::VARCHAR, record_key, violations[1].rule, violations[1].value
        FROM read_parquet('{tmp_path}/quarantine/complaints/*/*.parquet')
        """
    )
    assert got == [
        ("2024-01-11", "CMP-20240111", "schema_mismatch", "added: channel_v2; missing: none")
    ]


def test_quality_report_lists_rule_counts_without_personal_values(tmp_path: Path) -> None:
    data_dir = _run_with(tmp_path, {"transactions": {0: {"channel": "Fax"}}})

    report = (data_dir / "calidad.md").read_text()

    for section in (
        "## Rows per layer",
        "## Quarantine by rule",
        "## Alerts (flagged, not rejected)",
        "## Duplicates",
        "## Schema evolution",
        "## Nulls: structural and residual",
        "## Time zone",
        "## Findings",
    ):
        assert section in report
    assert "| transactions | invalid_domain | channel | 1 |" in report
    for personal in ("Name1", "Surname1", "900001", "user1@example.test", "123456781", "Fax"):
        assert personal not in report

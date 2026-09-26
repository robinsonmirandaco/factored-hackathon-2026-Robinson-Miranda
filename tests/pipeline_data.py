"""Small synthetic dataset laid out like the raw S3 export, for pipeline tests.

Every value is made up. Rows are coherent (keys resolve, domains and ranges hold), so a test can
break exactly one rule by editing one field.
"""

import csv
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

from pipeline.contracts import BY_TABLE

Rows = dict[str, list[dict[str, str]]]
PARTITION = "_partition"

CUSTOMERS = (("CUS-1", "Argentina", "DNI"), ("CUS-2", "Colombia", "CC"), ("CUS-3", "México", "DNI"))
CURRENCY = {"Argentina": "ARS", "Colombia": "COP", "México": "USD"}


def _customer(customer_id: str, country: str, document_type: str) -> dict[str, str]:
    n = customer_id.split("-")[1]
    return {
        "customer_id": customer_id,
        "document_number": f"90000{n}",
        "document_type": document_type,
        "first_name": f"Name{n}",
        "last_name": f"Surname{n}",
        "date_of_birth": "1990-01-01",
        "gender": "F",
        "email": f"user{n}@example.test",
        "mobile_phone": "",
        "landline_phone": "",
        "address": "",
        "city": "City",
        "state": "State",
        "country": country,
        "postal_code": "",
        "detected_accent": "",
        "segment": "Basic",
        "credit_score": "700.0",
        "estimated_monthly_income": "1000.50",
        "occupation": "",
        "marital_status": "",
        "education_level": "",
        "registration_date": "2020-01-01 10:00:00",
        "registration_branch_id": "SUC-1",
        "customer_status": "Active",
        "last_updated": "2026-08-01 10:00:00",
        "accepts_marketing": "True",
    }


def _product(customer_id: str, country: str) -> dict[str, str]:
    n = customer_id.split("-")[1]
    return {
        "product_id": f"PRD-{n}",
        "customer_id": customer_id,
        "product_type": "Cuenta Ahorro",
        "product_number": f"12345678{n}",
        "currency": CURRENCY[country],
        "current_balance": "500.00",
        "credit_limit": "",
        "interest_rate": "1.5",
        "opening_date": "2021-01-01",
        "expiration_date": "",
        "opening_branch_id": "SUC-1",
        "product_status": "Active",
        "opening_channel": "Branch",
        "has_linked_app": "True",
        "days_past_due": "",
        "last_transaction_date": "2026-06-01 10:00:00",
        "last_updated": "2026-08-01 10:00:00",
    }


def _branch(n: int) -> dict[str, str]:
    return {
        "branch_id": f"SUC-{n}",
        "branch_code": f"BR{n}",
        "branch_name": f"Branch {n}",
        "branch_type": "Main",
        "address": "",
        "city": "City",
        "state": "State",
        "country": "Colombia",
        "postal_code": "",
        "geographic_zone": "Center",
        "phone": "",
        "email": "",
        "opening_time": "08:00:00",
        "closing_time": "17:00:00",
        "has_atms": "True",
        "atm_count": "2",
        "has_teller_windows": "True",
        "teller_window_count": "3",
        "latitude": "4.6",
        "longitude": "-74.1",
        "branch_opening_date": "2010-01-01",
        "branch_status": "Active",
    }


def _agent(n: int) -> dict[str, str]:
    return {
        "agent_id": f"AGT-{n}",
        "employee_code": f"EMP{n}",
        "first_name": f"Agent{n}",
        "last_name": "Test",
        "email": "",
        "phone": "",
        "native_accent": "",
        "country_of_origin": "Colombia",
        "assigned_branch_id": "SUC-1",
        "agent_type": "Phone",
        "experience_level": "Senior",
        "languages": "es",
        "specialty": "",
        "hire_date": "2019-01-01",
        "avg_csat": "4.5",
        "total_monthly_interactions": "300.0",
        "agent_status": "Active",
        "work_shift": "Morning",
    }


def _rates(day: date) -> list[dict[str, str]]:
    return [
        {
            "date": day.isoformat(),
            "source_currency": src,
            "target_currency": tgt,
            "exchange_rate": rate,
            "buy_rate": rate,
            "sell_rate": rate,
            "source": "Internal",
        }
        for src, tgt, rate in (("USD", "COP", "4000.0"), ("COP", "USD", "0.00025"))
    ]


def _daily(day: date) -> Rows:
    d = f"{day:%Y%m%d}"
    iso = day.isoformat()
    rows: Rows = {t: [] for t in BY_TABLE if BY_TABLE[t].partitioned}
    for customer_id, country, _ in CUSTOMERS:
        n = customer_id.split("-")[1]
        rows["transactions"].append(
            {
                PARTITION: iso,
                "transaction_id": f"TRX-{d}-{n}",
                "transaction_date": f"{iso} 10:00:00",
                "process_date": iso,
                "product_id": f"PRD-{n}",
                "customer_id": customer_id,
                "transaction_type": "Purchase",
                "transaction_category": "Food",
                "amount": "25.50",
                "currency": CURRENCY[country],
                "amount_usd": "",
                "channel": "POS",
                "branch_id": "",
                "merchant_name": "Store",
                "merchant_category": "Food",
                "transaction_country": country,
                "transaction_city": "City",
                "transaction_status": "Approved",
                "response_code": "00",
                "is_fraud": "False",
                "fraud_score": "1.5",
                "latitude": "",
                "longitude": "",
            }
        )
    interaction_id = f"INT-{d}"
    rows["call_center_interactions"].append(
        {
            PARTITION: iso,
            "interaction_id": interaction_id,
            "interaction_date": f"{iso} 09:00:00",
            "process_date": iso,
            "customer_id": "CUS-1",
            "agent_id": "AGT-1",
            "interaction_type": "Inbound Call",
            "channel": "Phone",
            "contact_reason": "Consulta",
            "reason_category": "Queja",
            "duration_seconds": "300.0",
            "wait_time_seconds": "20.0",
            "was_resolved": "True",
            "requires_followup": "False",
            "detected_sentiment": "Neutral",
            "sentiment_score": "0.1",
            "customer_detected_accent": "",
            "agent_used_accent": "",
            "was_escalated": "False",
            "mentioned_products": "",
            "has_transcript": "True",
            "has_recording": "True",
        }
    )
    rows["complaints"].append(
        {
            PARTITION: iso,
            "complaint_id": f"CMP-{d}",
            "creation_date": f"{(day + timedelta(days=1)).isoformat()} 07:00:00",
            "process_date": iso,
            "customer_id": "CUS-1",
            "case_type": "Claim",
            "category": "Transactions",
            "subcategory": "",
            "reception_channel": "Call Center",
            "affected_product_id": "PRD-1",
            "related_branch_id": "",
            "origin_interaction_id": "",
            "description": "Text",
            "claimed_amount": "25.50",
            "currency": "ARS",
            "priority": "Medium",
            "status": "Open",
            "assigned_agent_id": "",
            "assignment_date": "",
            "first_response_date": "",
            "resolution_date": "",
            "closing_date": "",
            "sla_breached": "False",
            "resolution_days": "",
            "resolution": "",
            "compensation_granted": "",
            "resolution_satisfaction": "",
            "is_repeat_complainer": "False",
        }
    )
    rows["satisfaction_surveys"].append(
        {
            PARTITION: iso,
            "survey_id": f"SRV-{d}",
            "survey_date": f"{iso} 12:00:00",
            "process_date": iso,
            "interaction_id": interaction_id,
            "customer_id": "CUS-1",
            "agent_id": "AGT-1",
            "survey_type": "CSAT",
            "send_channel": "Email",
            "main_score": "4",
            "nps_category": "",
            "question_1_text": "",
            "question_1_response": "",
            "question_2_text": "",
            "question_2_response": "",
            "question_3_text": "",
            "question_3_response": "",
            "open_comments": "",
            "comment_sentiment": "",
            "response_time_hours": "3.0",
            "campaign_response_rate": "",
        }
    )
    rows["call_transcripts"].append(
        {
            PARTITION: iso,
            "transcript_id": f"TRN-{d}",
            "interaction_id": interaction_id,
            "process_date": iso,
            "customer_id": "CUS-1",
            "agent_id": "AGT-1",
            "full_text": "Hola, buenos dias",
            "customer_text": "Hola",
            "agent_text": "Buenos dias",
            "detected_language": "es",
            "detected_accent": "",
            "accent_confidence": "0.9",
            "detected_keywords": "",
            "mentioned_entities": "",
            "detected_intents": "",
            "main_topics": "",
            "transcription_model": "Whisper v3",
            "audio_quality": "High",
            "duration_seconds": "300.0",
        }
    )
    return rows


def build_dataset(days: Sequence[date]) -> Rows:
    """Builds a coherent dataset with daily partitions on the given days.

    Args:
        days: Partition days.

    Returns:
        Rows per table; partitioned rows carry their partition day under `_partition`.
    """
    rows: Rows = {
        "branches": [_branch(1), _branch(2)],
        "service_agents": [_agent(1), _agent(2)],
        "customers": [_customer(*c) for c in CUSTOMERS],
        "products": [_product(c[0], c[1]) for c in CUSTOMERS],
        "daily_exchange_rates": [r for day in days for r in _rates(day)],
    }
    for day in days:
        for table, daily in _daily(day).items():
            rows.setdefault(table, []).extend(daily)
    return rows


def partition_path(raw: Path, table: str, day: date) -> Path:
    """Returns the raw path of one daily partition file, as laid out in S3.

    Args:
        raw: The `raw/` folder.
        table: Table name.
        day: Partition day.

    Returns:
        `raw/<table>/year=YYYY/month=MM/day=DD/<table>_YYYYMMDD.csv`.
    """
    return (
        raw
        / table
        / f"year={day:%Y}"
        / f"month={day:%m}"
        / f"day={day:%d}"
        / f"{table}_{day:%Y%m%d}.csv"
    )


def write_csv(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    """Writes one CSV file with a UTF-8 BOM, like the source files.

    Args:
        path: Target file.
        header: Column names in order.
        rows: Rows as dicts; keys not in the header are ignored.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows([[row.get(c, "") for c in header] for row in rows])


def write_dataset(data_dir: Path, rows: Rows) -> None:
    """Writes the dataset under `data_dir/raw` with the source layout.

    Args:
        data_dir: Root data directory.
        rows: Rows per table, as returned by `build_dataset`.
    """
    raw = data_dir / "raw"
    for table, table_rows in rows.items():
        contract = BY_TABLE[table]
        if not contract.partitioned:
            write_csv(raw / f"{table}.csv", contract.header, table_rows)
            continue
        by_day: dict[str, list[dict[str, str]]] = {}
        for row in table_rows:
            by_day.setdefault(row[PARTITION], []).append(row)
        for day, day_rows in by_day.items():
            write_csv(
                partition_path(raw, table, date.fromisoformat(day)), contract.header, day_rows
            )

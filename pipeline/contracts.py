"""Per-table contracts: the exact header, column types, keys and time semantics of each source.

Only the tables in `CONTRACTS` are extracted and processed. `digital_events`, `campaign_sends` and
`marketing_campaigns` are out of scope (design §9.1) and never read.
"""

from dataclasses import dataclass
from typing import Literal, cast, get_args

ColumnType = Literal[
    "VARCHAR", "BIGINT", "DECIMAL", "DOUBLE", "DATE", "TIMESTAMP", "TIME", "BOOLEAN"
]
ZoneSource = Literal["customer", "own_country"]


@dataclass(frozen=True)
class Column:
    """One source column.

    Attributes:
        name: Column name in the CSV header.
        type: Silver type. DECIMAL is DECIMAL(18,2); BIGINT accepts "12" and "12.0".
        required: Whether an empty value sends the row to quarantine.
    """

    name: str
    type: ColumnType = "VARCHAR"
    required: bool = False


@dataclass(frozen=True)
class Contract:
    """What a valid file and a valid row of one table look like.

    Attributes:
        table: Table name, also the S3 folder or file stem.
        columns: Columns in header order. Any other header is a schema mismatch.
        primary_key: Columns that identify a row.
        partitioned: True for daily partitions under year=/month=/day=, False for a snapshot file.
        zone: Where the local time zone of the row comes from, or None if it has no timestamps.
        event_column: Timestamp that places the row in time, if any.
        cutoff_hours: Hour of day at which the partition's operational day starts. A partition D
            holds events from D + cutoff to D + 1 day + cutoff, both ends included.
    """

    table: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    partitioned: bool = False
    zone: ZoneSource | None = None
    event_column: str | None = None
    cutoff_hours: int | None = None

    @property
    def header(self) -> list[str]:
        """Column names in the expected header order."""
        return [c.name for c in self.columns]


def _cols(spec: str) -> tuple[Column, ...]:
    """Parses a compact spec: one column per line, `name type [required]`, type defaults to VARCHAR.

    Args:
        spec: Multiline column spec.

    Returns:
        The columns in order.

    Raises:
        ValueError: If a type is not a known ColumnType.
    """
    columns = []
    for line in spec.strip().splitlines():
        parts = line.split()
        name = parts[0]
        col_type = parts[1] if len(parts) > 1 and parts[1] != "required" else "VARCHAR"
        if col_type not in get_args(ColumnType):
            raise ValueError(f"unknown type {col_type} for column {name}")
        columns.append(Column(name, cast(ColumnType, col_type), required="required" in parts))
    return tuple(columns)


BRANCHES = Contract(
    table="branches",
    columns=_cols("""
        branch_id VARCHAR required
        branch_code VARCHAR required
        branch_name
        branch_type
        address
        city
        state
        country VARCHAR required
        postal_code
        geographic_zone
        phone
        email
        opening_time TIME
        closing_time TIME
        has_atms BOOLEAN
        atm_count BIGINT
        has_teller_windows BOOLEAN
        teller_window_count BIGINT
        latitude DOUBLE
        longitude DOUBLE
        branch_opening_date DATE
        branch_status
    """),
    primary_key=("branch_id",),
)

SERVICE_AGENTS = Contract(
    table="service_agents",
    columns=_cols("""
        agent_id VARCHAR required
        employee_code VARCHAR required
        first_name
        last_name
        email
        phone
        native_accent
        country_of_origin
        assigned_branch_id
        agent_type
        experience_level
        languages
        specialty
        hire_date DATE
        avg_csat DOUBLE
        total_monthly_interactions BIGINT
        agent_status
        work_shift
    """),
    primary_key=("agent_id",),
)

CUSTOMERS = Contract(
    table="customers",
    columns=_cols("""
        customer_id VARCHAR required
        document_number VARCHAR required
        document_type VARCHAR required
        first_name
        last_name
        date_of_birth DATE
        gender
        email
        mobile_phone
        landline_phone
        address
        city
        state
        country VARCHAR required
        postal_code
        detected_accent
        segment VARCHAR required
        credit_score BIGINT
        estimated_monthly_income DECIMAL
        occupation
        marital_status
        education_level
        registration_date TIMESTAMP
        registration_branch_id
        customer_status VARCHAR required
        last_updated TIMESTAMP
        accepts_marketing BOOLEAN
    """),
    primary_key=("customer_id",),
    zone="own_country",
)

PRODUCTS = Contract(
    table="products",
    columns=_cols("""
        product_id VARCHAR required
        customer_id VARCHAR required
        product_type VARCHAR required
        product_number
        currency VARCHAR required
        current_balance DECIMAL
        credit_limit DECIMAL
        interest_rate DOUBLE
        opening_date DATE
        expiration_date DATE
        opening_branch_id
        product_status VARCHAR required
        opening_channel
        has_linked_app BOOLEAN
        days_past_due BIGINT
        last_transaction_date TIMESTAMP
        last_updated TIMESTAMP
    """),
    primary_key=("product_id",),
    zone="customer",
)

DAILY_EXCHANGE_RATES = Contract(
    table="daily_exchange_rates",
    columns=_cols("""
        date DATE required
        source_currency VARCHAR required
        target_currency VARCHAR required
        exchange_rate DOUBLE required
        buy_rate DOUBLE
        sell_rate DOUBLE
        source
    """),
    primary_key=("date", "source_currency", "target_currency"),
)

TRANSACTIONS = Contract(
    table="transactions",
    columns=_cols("""
        transaction_id VARCHAR required
        transaction_date TIMESTAMP required
        process_date DATE required
        product_id VARCHAR required
        customer_id VARCHAR required
        transaction_type VARCHAR required
        transaction_category
        amount DECIMAL required
        currency VARCHAR required
        amount_usd DECIMAL
        channel VARCHAR required
        branch_id
        merchant_name
        merchant_category
        transaction_country
        transaction_city
        transaction_status VARCHAR required
        response_code
        is_fraud BOOLEAN
        fraud_score DOUBLE
        latitude DOUBLE
        longitude DOUBLE
    """),
    primary_key=("transaction_id",),
    partitioned=True,
    zone="customer",
    event_column="transaction_date",
    cutoff_hours=6,
)

CALL_CENTER_INTERACTIONS = Contract(
    table="call_center_interactions",
    columns=_cols("""
        interaction_id VARCHAR required
        interaction_date TIMESTAMP required
        process_date DATE required
        customer_id VARCHAR required
        agent_id VARCHAR required
        interaction_type VARCHAR required
        channel VARCHAR required
        contact_reason
        reason_category VARCHAR required
        duration_seconds BIGINT
        wait_time_seconds BIGINT
        was_resolved BOOLEAN required
        requires_followup BOOLEAN required
        detected_sentiment
        sentiment_score DOUBLE
        customer_detected_accent
        agent_used_accent
        was_escalated BOOLEAN required
        mentioned_products
        has_transcript BOOLEAN required
        has_recording BOOLEAN required
    """),
    primary_key=("interaction_id",),
    partitioned=True,
    zone="customer",
    event_column="interaction_date",
    cutoff_hours=8,
)

COMPLAINTS = Contract(
    table="complaints",
    columns=_cols("""
        complaint_id VARCHAR required
        creation_date TIMESTAMP required
        process_date DATE required
        customer_id VARCHAR required
        case_type VARCHAR required
        category VARCHAR required
        subcategory
        reception_channel VARCHAR required
        affected_product_id
        related_branch_id
        origin_interaction_id
        description
        claimed_amount DECIMAL
        currency
        priority VARCHAR required
        status VARCHAR required
        assigned_agent_id
        assignment_date TIMESTAMP
        first_response_date TIMESTAMP
        resolution_date TIMESTAMP
        closing_date TIMESTAMP
        sla_breached BOOLEAN required
        resolution_days DOUBLE
        resolution
        compensation_granted DECIMAL
        resolution_satisfaction BIGINT
        is_repeat_complainer BOOLEAN required
    """),
    primary_key=("complaint_id",),
    partitioned=True,
    zone="customer",
    event_column="creation_date",
    cutoff_hours=8,
)

# Surveys and transcripts are filed under the partition of their interaction, not their own date.
SATISFACTION_SURVEYS = Contract(
    table="satisfaction_surveys",
    columns=_cols("""
        survey_id VARCHAR required
        survey_date TIMESTAMP required
        process_date DATE required
        interaction_id VARCHAR required
        customer_id VARCHAR required
        agent_id VARCHAR required
        survey_type VARCHAR required
        send_channel VARCHAR required
        main_score BIGINT required
        nps_category
        question_1_text
        question_1_response BIGINT
        question_2_text
        question_2_response BIGINT
        question_3_text
        question_3_response BIGINT
        open_comments
        comment_sentiment
        response_time_hours DOUBLE
        campaign_response_rate DOUBLE
    """),
    primary_key=("survey_id",),
    partitioned=True,
    zone="customer",
    event_column="survey_date",
)

CALL_TRANSCRIPTS = Contract(
    table="call_transcripts",
    columns=_cols("""
        transcript_id VARCHAR required
        interaction_id VARCHAR required
        process_date DATE required
        customer_id VARCHAR required
        agent_id VARCHAR required
        full_text
        customer_text
        agent_text
        detected_language
        detected_accent
        accent_confidence DOUBLE
        detected_keywords
        mentioned_entities
        detected_intents
        main_topics
        transcription_model
        audio_quality
        duration_seconds BIGINT
    """),
    primary_key=("transcript_id",),
    partitioned=True,
)

# Processing order: every table comes after the tables it looks up.
CONTRACTS: tuple[Contract, ...] = (
    BRANCHES,
    SERVICE_AGENTS,
    CUSTOMERS,
    PRODUCTS,
    DAILY_EXCHANGE_RATES,
    TRANSACTIONS,
    CALL_CENTER_INTERACTIONS,
    COMPLAINTS,
    SATISFACTION_SURVEYS,
    CALL_TRANSCRIPTS,
)
BY_TABLE: dict[str, Contract] = {c.table: c for c in CONTRACTS}

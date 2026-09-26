"""Per-table contracts: the exact header, column types, keys and time semantics of each source.

Only the tables in `CONTRACTS` are extracted and processed. `digital_events`, `campaign_sends` and
`marketing_campaigns` are out of scope (design §9.1) and never read.
"""

from dataclasses import dataclass
from typing import Literal, cast, get_args

# Bump when a contract, a rule or a silver column changes: every partition is then read again.
PIPELINE_VERSION = "1.0.0"

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
        domain: Allowed values, if the column is categorical.
        mapped_to: Section of config/normalization.yaml that maps its values, if any. Silver keeps
            the mapped value and the source value in `<name>_source`.
    """

    name: str
    type: ColumnType = "VARCHAR"
    required: bool = False
    domain: tuple[str, ...] | None = None
    mapped_to: str | None = None


@dataclass(frozen=True)
class Check:
    """A row rule; a row that breaks it goes to quarantine.

    Attributes:
        rule: Rule name shown in quarantine and in the quality report.
        column: Column whose raw value is recorded as the offending value.
        violated: SQL that is true when the row breaks the rule, over `t` (typed, naive local
            timestamps) and `r` (raw text plus `partition_date`). NULL counts as not violated.
    """

    rule: str
    column: str
    violated: str


@dataclass(frozen=True)
class Reference:
    """Referential integrity: the columns must match a row of another table's silver output.

    Attributes:
        columns: Columns of this table. The check applies only when none of them is empty.
        table: Referenced table.
        target: Referenced columns, in the same order.
        alert: If set, a miss is flagged in `alert_<alert>` instead of sent to quarantine.
    """

    columns: tuple[str, ...]
    table: str
    target: tuple[str, ...]
    alert: str | None = None


@dataclass(frozen=True)
class Alert:
    """A flag kept on valid rows: the row stays in silver with `alert_<name>` set.

    Attributes:
        name: Alert name; the silver column is `alert_<name>`.
        flagged: SQL that is true when the row is flagged, over `t` (typed) and `z` (the row's
            home country: `country_code`, `timezone`, `currency`).
    """

    name: str
    flagged: str


@dataclass(frozen=True)
class Contract:
    """What a valid file and a valid row of one table look like.

    Attributes:
        table: Table name, also the S3 folder or file stem.
        columns: Columns in header order. Any other header is a schema mismatch.
        primary_key: Columns that identify a row; must be unique.
        business_key: Columns that identify the same real-world record under another id. In fact
            (partitioned) tables a repeat goes to quarantine; in dimension tables it is flagged.
        partitioned: True for daily partitions under year=/month=/day=, False for a snapshot file.
        zone: Where the local time zone of the row comes from, or None if it has no timestamps.
        event_column: Timestamp that places the row in time, if any.
        cutoff_hours: Hour of day at which the partition's operational day starts. A partition D
            holds events from D + cutoff to D + 1 day + cutoff, both ends included.
        checks: Row rules specific to the table.
        references: Referential integrity rules.
        alerts: Flags that do not reject the row.
    """

    table: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    business_key: tuple[str, ...]
    partitioned: bool = False
    zone: ZoneSource | None = None
    event_column: str | None = None
    cutoff_hours: int | None = None
    checks: tuple[Check, ...] = ()
    references: tuple[Reference, ...] = ()
    alerts: tuple[Alert, ...] = ()

    @property
    def header(self) -> list[str]:
        """Column names in the expected header order."""
        return [c.name for c in self.columns]


def alert_columns(contract: Contract) -> list[str]:
    """Returns the alert columns silver adds to a table, in a fixed order.

    Args:
        contract: Table contract.

    Returns:
        Column names starting with `alert_`.
    """
    names = [a.name for a in contract.alerts]
    names += [r.alert for r in contract.references if r.alert]
    if not contract.partitioned:
        names.append("duplicate_business_key")
    return [f"alert_{n}" for n in names]


def _cols(
    spec: str,
    domains: dict[str, tuple[str, ...]] | None = None,
    mapped: dict[str, str] | None = None,
) -> tuple[Column, ...]:
    """Parses a compact spec: one column per line, `name type [required]`, type defaults to VARCHAR.

    Args:
        spec: Multiline column spec.
        domains: Allowed values per categorical column.
        mapped: Normalization section per mapped column.

    Returns:
        The columns in order.

    Raises:
        ValueError: If a type is unknown or a domain or mapping names a column not in the spec.
    """
    domains, mapped = domains or {}, mapped or {}
    columns = []
    for line in spec.strip().splitlines():
        parts = line.split()
        name = parts[0]
        col_type = parts[1] if len(parts) > 1 and parts[1] != "required" else "VARCHAR"
        if col_type not in get_args(ColumnType):
            raise ValueError(f"unknown type {col_type} for column {name}")
        columns.append(
            Column(
                name,
                cast(ColumnType, col_type),
                required="required" in parts,
                domain=domains.get(name),
                mapped_to=mapped.get(name),
            )
        )
    unknown = (set(domains) | set(mapped)) - {c.name for c in columns}
    if unknown:
        raise ValueError(f"domain or mapping for unknown columns: {sorted(unknown)}")
    return tuple(columns)


def _range(column: str, low: float, high: float) -> Check:
    return Check("out_of_range", column, f"t.{column} NOT BETWEEN {low} AND {high}")


def _positive(column: str) -> Check:
    return Check("not_positive", column, f"t.{column} <= 0")


def _non_negative(column: str) -> Check:
    return Check("negative", column, f"t.{column} < 0")


def _not_before(column: str, earlier: str) -> Check:
    return Check("date_order", column, f"t.{column} < t.{earlier}")


CURRENCIES = ("ARS", "COP", "MXN", "USD")
HOME_COUNTRIES = ("Argentina", "Colombia", "México")
CATEGORIES = ("Entertainment", "Food", "Health", "Other", "Services", "Transport")
CURRENCY_COUNTRY = Alert("currency_country", "t.currency IS NOT NULL AND t.currency <> z.currency")


BRANCHES = Contract(
    table="branches",
    columns=_cols(
        """
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
    """,
        domains={
            "branch_type": ("Corporate", "Express", "Main", "Premium"),
            "country": HOME_COUNTRIES,
            "branch_status": ("Active", "Temporarily Closed"),
        },
    ),
    primary_key=("branch_id",),
    business_key=("branch_code",),
    checks=(
        _not_before("closing_time", "opening_time"),
        _non_negative("atm_count"),
        _non_negative("teller_window_count"),
    ),
)

SERVICE_AGENTS = Contract(
    table="service_agents",
    columns=_cols(
        """
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
    """,
        domains={
            "agent_type": ("Digital", "Hybrid", "In-Person", "Phone"),
            "experience_level": ("Junior", "Mid-Senior", "Senior", "Specialist"),
            "agent_status": ("Active", "Inactive", "Leave", "Vacation"),
        },
    ),
    primary_key=("agent_id",),
    business_key=("employee_code",),
    checks=(_range("avg_csat", 1, 5), _non_negative("total_monthly_interactions")),
    references=(
        Reference(("assigned_branch_id",), "branches", ("branch_id",), alert="branch_unresolved"),
    ),
)

CUSTOMERS = Contract(
    table="customers",
    columns=_cols(
        """
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
    """,
        domains={
            "document_type": ("CC", "CE", "DNI", "Pasaporte"),
            "gender": ("F", "M", "O"),
            "country": HOME_COUNTRIES,
            "segment": ("Basic", "Plus", "Premium", "Student"),
            "customer_status": ("Active", "Closed", "Inactive", "Suspended"),
        },
    ),
    primary_key=("customer_id",),
    business_key=("document_type", "document_number"),
    checks=(
        Check("date_order", "date_of_birth", "t.date_of_birth > t.registration_date::DATE"),
        _range("credit_score", 300, 850),
        _positive("estimated_monthly_income"),
    ),
    references=(
        Reference(
            ("registration_branch_id",), "branches", ("branch_id",), alert="branch_unresolved"
        ),
    ),
    alerts=(
        Alert(
            "document_country",
            "NOT EXISTS (SELECT 1 FROM country_documents d "
            "WHERE d.country_code = z.country_code AND d.document_type = t.document_type)",
        ),
    ),
    zone="own_country",
)

PRODUCTS = Contract(
    table="products",
    columns=_cols(
        """
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
    """,
        domains={
            "currency": CURRENCIES,
            "product_status": ("Active", "Blocked", "Closed", "Suspended"),
            "opening_channel": ("App", "Branch", "Call Center", "Web"),
        },
        mapped={"product_type": "product_types"},
    ),
    primary_key=("product_id",),
    business_key=("product_number",),
    checks=(
        _not_before("expiration_date", "opening_date"),
        _non_negative("current_balance"),
        _positive("credit_limit"),
        _range("interest_rate", 0, 100),
        _non_negative("days_past_due"),
    ),
    references=(
        Reference(("customer_id",), "customers", ("customer_id",)),
        Reference(("opening_branch_id",), "branches", ("branch_id",)),
    ),
    alerts=(CURRENCY_COUNTRY,),
    zone="customer",
)

DAILY_EXCHANGE_RATES = Contract(
    table="daily_exchange_rates",
    columns=_cols(
        """
        date DATE required
        source_currency VARCHAR required
        target_currency VARCHAR required
        exchange_rate DOUBLE required
        buy_rate DOUBLE
        sell_rate DOUBLE
        source
    """,
        domains={
            "source_currency": CURRENCIES,
            "target_currency": CURRENCIES,
            "source": ("Bloomberg", "Central Bank", "Internal", "Reuters"),
        },
    ),
    primary_key=("date", "source_currency", "target_currency"),
    business_key=("date", "source_currency", "target_currency"),
    checks=(
        _positive("exchange_rate"),
        _positive("buy_rate"),
        _positive("sell_rate"),
        Check("buy_above_sell", "buy_rate", "t.buy_rate > t.sell_rate"),
        Check("same_currency", "target_currency", "t.source_currency = t.target_currency"),
    ),
)

TRANSACTIONS = Contract(
    table="transactions",
    columns=_cols(
        """
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
    """,
        domains={
            "transaction_type": (
                "Adjustment",
                "Deposit",
                "Payment",
                "Purchase",
                "Transfer",
                "Withdrawal",
            ),
            "transaction_category": CATEGORIES,
            "currency": CURRENCIES,
            "channel": ("ATM", "App", "Branch", "POS", "Transfer", "Web"),
            "merchant_category": CATEGORIES,
            "transaction_status": ("Approved", "Declined", "Pending", "Reversed"),
            "response_code": ("00", "05", "14", "51", "54"),
        },
        mapped={"transaction_country": "countries"},
    ),
    primary_key=("transaction_id",),
    business_key=("customer_id", "product_id", "transaction_date", "amount", "merchant_name"),
    partitioned=True,
    zone="customer",
    event_column="transaction_date",
    cutoff_hours=6,
    checks=(_positive("amount"), _positive("amount_usd"), _range("fraud_score", 0, 100)),
    references=(
        Reference(("product_id", "customer_id"), "products", ("product_id", "customer_id")),
        Reference(("branch_id",), "branches", ("branch_id",)),
    ),
    alerts=(CURRENCY_COUNTRY,),
)

CALL_CENTER_INTERACTIONS = Contract(
    table="call_center_interactions",
    columns=_cols(
        """
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
    """,
        domains={
            "interaction_type": ("Chat", "Email", "Inbound Call", "Outbound Call", "Video"),
            "channel": ("App", "Email", "Phone", "Web", "Web Chat", "WhatsApp"),
            "reason_category": (
                "Comercial",
                "Producto",
                "Queja",
                "Retención",
                "Transaccional",
                "Técnico",
            ),
            "detected_sentiment": (
                "Muy Negativo",
                "Muy Positivo",
                "Negativo",
                "Neutral",
                "Positivo",
            ),
        },
    ),
    primary_key=("interaction_id",),
    business_key=("customer_id", "agent_id", "interaction_date", "channel"),
    partitioned=True,
    zone="customer",
    event_column="interaction_date",
    cutoff_hours=8,
    checks=(
        _non_negative("duration_seconds"),
        _non_negative("wait_time_seconds"),
        _range("sentiment_score", -1, 1),
    ),
    references=(
        Reference(("customer_id",), "customers", ("customer_id",)),
        Reference(("agent_id",), "service_agents", ("agent_id",)),
    ),
)

COMPLAINTS = Contract(
    table="complaints",
    columns=_cols(
        """
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
    """,
        domains={
            "case_type": ("Claim", "Complaint", "Request", "Suggestion"),
            "category": ("Branch", "Fees", "Service", "Technical", "Transactions"),
            "reception_channel": ("App", "Branch", "Call Center", "Email", "Regulator", "Web"),
            "currency": CURRENCIES,
            "priority": ("Critical", "High", "Low", "Medium"),
            "status": ("Closed", "Escalated", "In Process", "Open", "Rejected", "Resolved"),
        },
    ),
    primary_key=("complaint_id",),
    business_key=(
        "customer_id",
        "creation_date",
        "case_type",
        "category",
        "subcategory",
        "affected_product_id",
        "claimed_amount",
    ),
    partitioned=True,
    zone="customer",
    event_column="creation_date",
    cutoff_hours=8,
    checks=(
        _positive("claimed_amount"),
        _non_negative("compensation_granted"),
        _range("resolution_satisfaction", 1, 5),
        _not_before("assignment_date", "creation_date"),
        _not_before("first_response_date", "creation_date"),
        _not_before("resolution_date", "creation_date"),
        _not_before("closing_date", "creation_date"),
        _not_before("closing_date", "resolution_date"),
    ),
    references=(
        Reference(("customer_id",), "customers", ("customer_id",)),
        Reference(("affected_product_id",), "products", ("product_id",)),
        # The product exists but belongs to another customer in almost every complaint.
        Reference(
            ("affected_product_id", "customer_id"),
            "products",
            ("product_id", "customer_id"),
            alert="product_not_owned",
        ),
        Reference(("related_branch_id",), "branches", ("branch_id",)),
        Reference(("assigned_agent_id",), "service_agents", ("agent_id",)),
    ),
    # About 5% of amounts or currencies are empty at random (the injected nulls); rejecting
    # those complaints would bias the demand counts, so they are flagged.
    alerts=(
        CURRENCY_COUNTRY,
        Alert("amount_currency_mismatch", "(t.claimed_amount IS NULL) <> (t.currency IS NULL)"),
    ),
)

# Surveys and transcripts are filed under the partition of their interaction, not their own date,
# and must repeat the interaction's customer and agent.
INTERACTION_PARTITION = Check(
    "partition_mismatch",
    "interaction_id",
    "EXISTS (SELECT 1 FROM silver_call_center_interactions i "
    "WHERE i.interaction_id = t.interaction_id AND i.partition_date <> r.partition_date)",
)
INTERACTION = Reference(
    ("interaction_id", "customer_id", "agent_id"),
    "call_center_interactions",
    ("interaction_id", "customer_id", "agent_id"),
)

SATISFACTION_SURVEYS = Contract(
    table="satisfaction_surveys",
    columns=_cols(
        """
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
    """,
        domains={
            "survey_type": ("CES", "CSAT", "NPS"),
            "send_channel": ("App", "Email", "IVR", "SMS", "Web"),
            "nps_category": ("Detractor", "Passive", "Promoter"),
            "comment_sentiment": ("Negative", "Neutral", "Positive"),
        },
    ),
    primary_key=("survey_id",),
    business_key=("interaction_id", "survey_type"),
    partitioned=True,
    zone="customer",
    event_column="survey_date",
    checks=(
        Check(
            "out_of_range",
            "main_score",
            "CASE t.survey_type WHEN 'CSAT' THEN t.main_score NOT BETWEEN 1 AND 5 "
            "WHEN 'NPS' THEN t.main_score NOT BETWEEN 0 AND 10 "
            "WHEN 'CES' THEN t.main_score NOT BETWEEN 1 AND 7 END",
        ),
        _range("question_1_response", 1, 5),
        _range("question_2_response", 1, 5),
        _range("question_3_response", 1, 5),
        Check(
            "survey_before_interaction",
            "survey_date",
            "EXISTS (SELECT 1 FROM silver_call_center_interactions i "
            "WHERE i.interaction_id = t.interaction_id "
            "AND t.survey_date < timezone(i.timezone, i.interaction_date))",
        ),
        INTERACTION_PARTITION,
    ),
    references=(INTERACTION,),
)

CALL_TRANSCRIPTS = Contract(
    table="call_transcripts",
    columns=_cols(
        """
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
    """,
        domains={
            "detected_language": ("es",),
            "transcription_model": ("AWS Transcribe", "Azure Speech", "Google STT", "Whisper v3"),
            "audio_quality": ("High", "Low", "Medium"),
        },
    ),
    primary_key=("transcript_id",),
    business_key=("interaction_id",),
    partitioned=True,
    checks=(
        _range("accent_confidence", 0, 1),
        _non_negative("duration_seconds"),
        Check(
            "transcript_not_flagged",
            "interaction_id",
            "EXISTS (SELECT 1 FROM silver_call_center_interactions i "
            "WHERE i.interaction_id = t.interaction_id AND NOT i.has_transcript)",
        ),
        INTERACTION_PARTITION,
    ),
    references=(INTERACTION,),
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

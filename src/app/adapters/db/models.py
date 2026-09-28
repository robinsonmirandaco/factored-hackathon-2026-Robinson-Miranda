"""ORM mapping of the serving tables the service reads and writes.

The schema itself lives in the SQL migrations of db/migrations; these classes only map it, and a
test checks that every mapped column exists there with the same type. Tables the service does
not query through the ORM (complaints, exchange rates, the queue, seed runs) are not mapped.
"""

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Integer, Numeric, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Numeric columns come back as float: tool results are stored as JSON, which has no Decimal.
Money = Numeric(18, 2, asdecimal=False)


class Base(DeclarativeBase):
    """Declarative base for every mapped table."""


class Customer(Base):
    """A bank customer. The document number is stored only as a keyed hash."""

    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    document_type: Mapped[str] = mapped_column(Text)
    document_hash: Mapped[str] = mapped_column(Text)
    first_name: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str] = mapped_column(Text)
    country_code: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)
    segment: Mapped[str] = mapped_column(Text)
    customer_status: Mapped[str] = mapped_column(Text)
    source_file: Mapped[str | None] = mapped_column(Text)


class Product(Base):
    """A product of a customer. Only the last four digits of its number are kept."""

    __tablename__ = "products"

    product_id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text)
    product_type: Mapped[str] = mapped_column(Text)
    product_number_last4: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(Text)
    current_balance: Mapped[float | None] = mapped_column(Money)
    credit_limit: Mapped[float | None] = mapped_column(Money)
    product_status: Mapped[str] = mapped_column(Text)
    source_file: Mapped[str | None] = mapped_column(Text)


class Transaction(Base):
    """A transaction. `transaction_date` is naive local time of the customer's country."""

    __tablename__ = "transactions"

    transaction_id: Mapped[str] = mapped_column(Text, primary_key=True)
    transaction_date: Mapped[datetime] = mapped_column(DateTime)
    process_date: Mapped[date] = mapped_column(Date)
    product_id: Mapped[str] = mapped_column(Text)
    customer_id: Mapped[str] = mapped_column(Text)
    transaction_type: Mapped[str] = mapped_column(Text)
    amount: Mapped[float] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    merchant_name: Mapped[str | None] = mapped_column(Text)
    merchant_category: Mapped[str | None] = mapped_column(Text)
    transaction_country: Mapped[str | None] = mapped_column(Text)
    transaction_city: Mapped[str | None] = mapped_column(Text)
    transaction_status: Mapped[str] = mapped_column(Text)
    source_file: Mapped[str | None] = mapped_column(Text)


class Case(Base):
    """One customer contact handled by the agent. Its state machine lives in services.agent."""

    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text)
    transaction_id: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(Text)
    # open | auto_resolved | awaiting_customer | escalated | approved | rejected | closed
    status: Mapped[str] = mapped_column(Text, default="open")
    autonomy_level: Mapped[str] = mapped_column(Text, default="L0")
    recommended_action: Mapped[str | None] = mapped_column(Text)
    escalation_reason: Mapped[str | None] = mapped_column(Text)
    human_decision: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Dispute(Base):
    """A dispute registered by TRAZO on a transaction of the customer."""

    __tablename__ = "disputes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    folio: Mapped[str | None] = mapped_column(Text)
    customer_id: Mapped[str] = mapped_column(Text)
    case_id: Mapped[str] = mapped_column(Text)
    transaction_id: Mapped[str] = mapped_column(Text)
    dispute_type: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[float | None] = mapped_column(Money)
    currency: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="opened")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CardBlock(Base):
    """A card blocked by TRAZO, with the product status it had before."""

    __tablename__ = "card_blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(Text)
    product_id: Mapped[str] = mapped_column(Text)
    case_id: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    status_before: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AuditRecord(Base):
    """Append-only. Every tool call, policy decision and human action writes one row."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(Text)
    case_id: Mapped[str | None] = mapped_column(Text)
    customer_id: Mapped[str | None] = mapped_column(Text)
    # agent | tool | policy | human | system
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Null until the post-action read-back exists (TRZ-19).
    verified: Mapped[bool | None] = mapped_column(Boolean)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6, asdecimal=False))
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[str | None] = mapped_column(Text)


class QuarantineRecord(Base):
    """Rows rejected at ingestion, with the reason. Never silently dropped."""

    __tablename__ = "quarantine"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text)
    table_name: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

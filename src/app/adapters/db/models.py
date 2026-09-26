"""Canonical schema. The whole system reads these tables and nothing else.

External datasets enter through the ingestion adapters and land here after validation.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for every table."""


class Customer(Base):
    """Derived, PII-free customer profile. Names, documents and card numbers never live here."""

    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    segment: Mapped[str] = mapped_column(String(32), default="retail")
    country: Mapped[str] = mapped_column(String(2), default="US")
    tenure_months: Mapped[int] = mapped_column(Integer, default=0)
    avg_monthly_spend: Mapped[float] = mapped_column(Float, default=0.0)
    risk_tier: Mapped[str] = mapped_column(String(16), default="standard")
    card_status: Mapped[str] = mapped_column(String(16), default="active")  # active | frozen
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Transaction(Base):
    """A card transaction. `is_fraud` holds the label when the source provides one."""

    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    merchant: Mapped[str] = mapped_column(String(128))
    merchant_category: Mapped[str] = mapped_column(String(64), default="other")
    country: Mapped[str] = mapped_column(String(2), default="US")
    channel: Mapped[str] = mapped_column(String(16), default="online")  # online | pos | atm
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    # approved | blocked | reversed | disputed
    status: Mapped[str] = mapped_column(String(16), default="approved")
    is_fraud: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # label when known
    source: Mapped[str] = mapped_column(String(32), default="synthetic")

    __table_args__ = (Index("ix_tx_customer_ts", "customer_id", "timestamp"),)


class Interaction(Base):
    """A customer message or historical transcript. `text` is stored already PII-redacted."""

    __tablename__ = "interactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16), default="chat")
    text: Mapped[str] = mapped_column(Text)
    product: Mapped[str] = mapped_column(String(64), default="card")
    issue_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="synthetic")


class Case(Base):
    """One customer contact handled by the agent. Its state machine lives in services.agent."""

    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    intent: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    # open | auto_resolved | awaiting_customer | escalated | approved | rejected | closed
    autonomy_level: Mapped[str] = mapped_column(String(4), default="L0")
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_explanation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    recommended_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    escalation_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    human_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class AuditRecord(Base):
    """Append-only. Every tool call, score, policy decision and human action writes one row."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    case_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    # agent | tool | scorer | policy | human | system
    actor: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class QuarantineRecord(Base):
    """Rows rejected at ingestion, with the reason. Never silently dropped."""

    __tablename__ = "quarantine"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    table_name: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(256), index=True)
    raw: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

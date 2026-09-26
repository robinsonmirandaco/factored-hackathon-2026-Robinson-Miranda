"""Canonical row contracts. Every ingestion adapter must produce these; validation enforces them."""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.core.time import utcnow

VALID_CHANNELS = {"online", "pos", "atm"}
VALID_TX_STATUS = {"approved", "blocked", "reversed", "disputed"}
VALID_CURRENCIES = {"USD", "EUR", "COP", "MXN", "BRL", "PEN", "CLP", "ARS", "GBP"}


class CustomerRow(BaseModel):
    """One customer as it must look before it reaches the customers table."""

    id: str = Field(min_length=1, max_length=64)
    segment: str = "retail"
    country: str = Field(default="US", min_length=2, max_length=2)
    tenure_months: int = Field(default=0, ge=0, le=600)
    avg_monthly_spend: float = Field(default=0.0, ge=0)
    card_status: str = "active"


class TransactionRow(BaseModel):
    """One transaction as it must look before it reaches the transactions table."""

    id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0, le=1_000_000)
    currency: str = "USD"
    merchant: str = Field(min_length=1, max_length=128)
    merchant_category: str = "other"
    country: str = Field(default="US", min_length=2, max_length=2)
    channel: str = "online"
    timestamp: datetime
    status: str = "approved"
    source: str = "synthetic"

    @field_validator("currency")
    @classmethod
    def _cur(cls, v: str) -> str:
        v = v.upper()
        if v not in VALID_CURRENCIES:
            raise ValueError(f"currency {v} not supported")
        return v

    @field_validator("channel")
    @classmethod
    def _ch(cls, v: str) -> str:
        if v not in VALID_CHANNELS:
            raise ValueError(f"channel {v} invalid")
        return v

    @field_validator("status")
    @classmethod
    def _st(cls, v: str) -> str:
        if v not in VALID_TX_STATUS:
            raise ValueError(f"status {v} invalid")
        return v

    @field_validator("timestamp")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        now = utcnow()
        if v.year < 2000 or v > now.replace(year=now.year + 1):
            raise ValueError("timestamp out of plausible range")
        return v


class InteractionRow(BaseModel):
    """One customer message as it must look before it reaches the interactions table."""

    id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    channel: str = "chat"
    text: str = Field(min_length=3)
    product: str = "card"
    issue_type: str | None = None
    timestamp: datetime
    outcome: str | None = None
    source: str = "synthetic"

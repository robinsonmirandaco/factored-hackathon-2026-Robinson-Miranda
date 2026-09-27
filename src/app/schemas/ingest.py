"""Row contracts of the fixture loaders (synthetic generator and golden cases).

They mirror the dataset domains enforced by the pipeline contracts, so a fixture row looks like a
cohort row. Validation enforces them; a row that fails goes to quarantine with its reason.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.time import utcnow

Currency = Literal["ARS", "COP", "MXN", "USD"]


class CustomerRow(BaseModel):
    """One customer as it must look before it reaches the customers table.

    The document number is only used to compute its keyed hash; it is never stored.
    """

    customer_id: str = Field(min_length=1, max_length=64)
    document_type: Literal["CC", "CE", "DNI", "Pasaporte"]
    document_number: str = Field(min_length=1, max_length=32)
    first_name: str | None = Field(default=None, max_length=64)
    country: Literal["Argentina", "Colombia", "México"]
    country_code: Literal["AR", "CO", "MX"]
    timezone: str = Field(min_length=1, max_length=64)
    segment: Literal["Basic", "Plus", "Premium", "Student"]
    customer_status: Literal["Active", "Closed", "Inactive", "Suspended"] = "Active"


class ProductRow(BaseModel):
    """One product as it must look before it reaches the products table."""

    product_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    product_type: Literal[
        "checking_account",
        "credit_card",
        "debit_card",
        "insurance",
        "investment",
        "mortgage",
        "personal_loan",
        "savings_account",
    ]
    product_number_last4: str | None = Field(default=None, pattern=r"^\d{4}$")
    currency: Currency
    current_balance: float | None = Field(default=None, ge=0)
    credit_limit: float | None = Field(default=None, gt=0)
    product_status: Literal["Active", "Blocked", "Closed", "Suspended"] = "Active"


class TransactionRow(BaseModel):
    """One transaction as it must look before it reaches the transactions table.

    `transaction_date` is naive local time of the customer's country, like TRAZO_NOW.
    """

    transaction_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    product_id: str = Field(min_length=1, max_length=64)
    transaction_date: datetime
    process_date: date
    transaction_type: Literal[
        "Adjustment", "Deposit", "Payment", "Purchase", "Transfer", "Withdrawal"
    ]
    amount: float = Field(gt=0, le=1_000_000)
    currency: Currency
    channel: Literal["ATM", "App", "Branch", "POS", "Transfer", "Web"]
    merchant_name: str | None = Field(default=None, max_length=128)
    merchant_category: (
        Literal["Entertainment", "Food", "Health", "Other", "Services", "Transport"] | None
    ) = None
    transaction_country: str | None = Field(default=None, min_length=2, max_length=2)
    transaction_city: str | None = Field(default=None, max_length=64)
    transaction_status: Literal["Approved", "Declined", "Pending", "Reversed"] = "Approved"

    @field_validator("transaction_date")
    @classmethod
    def _plausible(cls, v: datetime) -> datetime:
        now = utcnow()
        if v.year < 2000 or v > now.replace(year=now.year + 1):
            raise ValueError("transaction_date out of plausible range")
        return v

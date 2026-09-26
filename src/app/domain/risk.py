"""Risk scoring contract: features, result and the Scorer protocol every backend implements.

The agent and the policy engine only see this contract, never which backend is running.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

FEATURE_NAMES = [
    "amount",
    "amount_vs_avg_ratio",
    "hour",
    "is_night",
    "is_weekend",
    "is_foreign",
    "is_online",
    "merchant_category_risk",
    "tenure_months",
    "tx_last_24h",
]

CATEGORY_RISK = {
    "gambling": 0.9,
    "crypto": 0.9,
    "electronics": 0.6,
    "travel": 0.5,
    "jewelry": 0.7,
    "grocery": 0.1,
    "fuel": 0.15,
    "restaurant": 0.2,
    "subscription": 0.25,
    "other": 0.4,
}


@dataclass
class TxFeatures:
    """Raw inputs needed to build the feature vector of one transaction.

    Attributes:
        amount: Transaction amount.
        customer_avg_monthly_spend: Customer's average monthly spend.
        timestamp: Transaction time (naive UTC).
        country: Transaction country code.
        customer_country: Customer home country code.
        channel: "online", "pos" or "atm".
        merchant_category: Merchant category key.
        tenure_months: Customer tenure in months.
        tx_last_24h: Customer transactions in the 24 hours before this one.
    """

    amount: float
    customer_avg_monthly_spend: float
    timestamp: datetime
    country: str
    customer_country: str
    channel: str
    merchant_category: str
    tenure_months: int
    tx_last_24h: int = 0

    def vector(self) -> list[float]:
        """Builds the feature vector in FEATURE_NAMES order.

        Returns:
            The numeric feature vector.
        """
        avg = max(self.customer_avg_monthly_spend, 1.0)
        hour = self.timestamp.hour
        return [
            float(self.amount),
            float(self.amount) / avg,
            float(hour),
            1.0 if (hour < 6 or hour >= 23) else 0.0,
            1.0 if self.timestamp.weekday() >= 5 else 0.0,
            1.0 if self.country != self.customer_country else 0.0,
            1.0 if self.channel == "online" else 0.0,
            CATEGORY_RISK.get(self.merchant_category, 0.4),
            float(self.tenure_months),
            float(self.tx_last_24h),
        ]


@dataclass
class RiskResult:
    """Score and explanation for one transaction.

    Attributes:
        score: Calibrated probability of fraud, between 0 and 1.
        top_factors: Contributions as dicts with keys feature, contribution and value.
        backend: Name of the scorer that produced the result.
        model_version: Version of the model artifact.
    """

    score: float
    top_factors: list[dict[str, Any]] = field(default_factory=list)
    backend: str = "random"
    model_version: str = "0"

    def explanation_text(self) -> str:
        """Renders the top three factors with a deterministic template.

        Returns:
            Text such as "is_foreign=1.00 raises risk; hour=3.00 raises risk".
        """
        if not self.top_factors:
            return "no explanation available"
        parts = []
        for f in self.top_factors[:3]:
            direction = "raises" if f["contribution"] > 0 else "lowers"
            parts.append(f"{f['feature']}={f['value']:.2f} {direction} risk")
        return "; ".join(parts)


class Scorer(Protocol):
    """Interface every risk scorer implements."""

    name: str

    def score(self, tx_id: str, feats: TxFeatures) -> RiskResult:
        """Scores one transaction.

        Args:
            tx_id: Transaction id.
            feats: Transaction features.

        Returns:
            The risk result.
        """
        ...

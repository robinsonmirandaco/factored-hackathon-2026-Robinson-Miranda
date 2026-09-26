"""Contract for what the LLM extracts from a customer message."""

from pydantic import BaseModel, Field

INTENTS = [
    "blocked_purchase",
    "unrecognized_charge",
    "duplicate_charge",
    "lost_or_stolen_card",
    "general_inquiry",
    "unknown",
]


class IntentExtraction(BaseModel):
    """Intent and entities proposed by the LLM, validated before any code uses them.

    Attributes:
        intent: One of INTENTS; anything else is normalized to "unknown".
        amount: Amount mentioned by the customer, if any.
        merchant: Merchant mentioned by the customer, if any.
        language: ISO 639-1 language code of the message.
        customer_claims_legitimate: True if the customer says they made the purchase.
        confidence: Extractor confidence between 0 and 1.
    """

    intent: str = Field(description="one of the allowed intents")
    amount: float | None = None
    merchant: str | None = None
    language: str = "en"
    customer_claims_legitimate: bool | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)

    def normalized(self) -> "IntentExtraction":
        """Maps an unknown intent to "unknown" so routing never receives free text.

        Returns:
            This instance, normalized in place.
        """
        if self.intent not in INTENTS:
            self.intent = "unknown"
        return self

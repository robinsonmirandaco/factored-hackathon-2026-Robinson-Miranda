"""Contract of the comprehension step (design 6.1), shared by the LLM and the rules baseline.

Every clue carries `evidence`, the literal fragment of the customer message it came from. Code
checks that the fragment is really in the message; a clue without a faithful fragment is dropped
and counted, so nothing the customer did not write can reach identification.
"""

from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Intent = Literal[
    "unrecognized_charge",
    "billing_error_amount",
    "billing_error_duplicate",
    "claim_status",
    "out_of_scope",
]
INTENTS: tuple[Intent, ...] = (
    "unrecognized_charge",
    "billing_error_amount",
    "billing_error_duplicate",
    "claim_status",
    "out_of_scope",
)
LanguageVariant = Literal["es-MX", "es-CO", "es-AR", "pt-BR"]
Channel = Literal["POS", "ATM", "Web", "App"]
CLUE_FIELDS = ("amount", "date", "merchant_hint", "channel_hint", "card_in_possession")


class _Clue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence: str = Field(min_length=1)


class AmountClue(_Clue):
    """Amount as the customer states it.

    Attributes:
        value: Stated amount, with thousands and multipliers ("1.8 mil", "390 lucas") applied.
        currency: ISO 4217 code when the message names or implies one, else None.
        approximate: The customer hedges ("como", "unos", "mais de").
        evidence: Literal fragment of the message.
    """

    value: float = Field(gt=0)
    currency: str | None = None
    approximate: bool = False


class DateClue(_Clue):
    """Date as the customer states it, resolved against the simulated clock.

    Attributes:
        expression: The words the customer used.
        resolved_from: Simulated "today" the window counts back from.
        window_days: Fewest and most days back the expression can mean, both inclusive.
        evidence: Literal fragment of the message.
    """

    expression: str
    resolved_from: date
    window_days: tuple[int, int]

    @model_validator(mode="after")
    def _ordered_window(self) -> "DateClue":
        low, high = self.window_days
        if not 0 <= low <= high:
            raise ValueError("window_days must be (low, high) with 0 <= low <= high")
        return self

    def window(self) -> tuple[date, date]:
        """Returns the window as calendar dates.

        Returns:
            First and last date the expression can mean.
        """
        low, high = self.window_days
        return self.resolved_from - timedelta(days=high), self.resolved_from - timedelta(days=low)


class TextClue(_Clue):
    """Free-text clue, such as the merchant.

    Attributes:
        value: Text the customer wrote.
        evidence: Literal fragment of the message.
    """

    value: str = Field(min_length=1)


class ChannelClue(_Clue):
    """Channel of the charge.

    Attributes:
        value: POS, ATM, Web or App, the channel values of the transactions.
        evidence: Literal fragment of the message.
    """

    value: Channel


class PossessionClue(_Clue):
    """Whether the customer still has the card.

    Attributes:
        value: True when the customer says they have it, False when lost or stolen.
        evidence: Literal fragment of the message.
    """

    value: bool


class ComprehensionContext(BaseModel):
    """What the comprehension step knows besides the message; never the customer id.

    Attributes:
        now: Simulated "now" of the case (TRZ-24), naive local time.
        country_code: Home country of the customer.
        local_currency: Currency of that country, used when the customer says "pesos" or "$".
    """

    model_config = ConfigDict(frozen=True)

    now: datetime
    country_code: str
    local_currency: str


class Comprehension(BaseModel):
    """Intent and clues of one customer message, as design 6.1 defines them.

    Attributes:
        intent: One of INTENTS.
        amount: Amount clue.
        date: Date clue.
        merchant_hint: Merchant clue.
        channel_hint: Channel clue.
        card_in_possession: Card possession clue.
        language: Language variant of the message.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: Intent
    amount: AmountClue | None = None
    date: DateClue | None = None
    merchant_hint: TextClue | None = None
    channel_hint: ChannelClue | None = None
    card_in_possession: PossessionClue | None = None
    language: LanguageVariant

    def clues(self) -> dict[str, _Clue]:
        """Returns the clues that are present, by field name.

        Returns:
            Field name to clue, without the empty ones.
        """
        found = {name: getattr(self, name) for name in CLUE_FIELDS}
        return {name: clue for name, clue in found.items() if clue is not None}

    def faithful(self, message: str) -> tuple["Comprehension", list[str]]:
        """Drops every clue whose evidence is not in the message.

        Args:
            message: The message the clues were extracted from.

        Returns:
            The comprehension without unfaithful clues, and the names of the dropped ones.
        """
        dropped = [
            name
            for name, clue in self.clues().items()
            if not evidence_is_faithful(clue.evidence, message)
        ]
        return self.model_copy(update=dict.fromkeys(dropped)), dropped


DateKind = Literal[
    "calendar",
    "today",
    "yesterday",
    "day_before_yesterday",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "last_monday",
    "last_tuesday",
    "last_wednesday",
    "last_thursday",
    "last_friday",
    "last_saturday",
    "last_sunday",
    "this_week",
    "last_week",
    "weekend",
    "last_month",
    "early_this_month",
    "few_days",
    "recently",
    "days_ago",
    "weeks_ago",
    "months_ago",
]


class DateReading(_Clue):
    """Date as the LLM reads it: the words and their meaning, never a computed date.

    Calendar arithmetic stays in code: the adapter turns `kind` into a window with the simulated
    clock (TRZ-24), the same one the rules baseline uses.

    Attributes:
        expression: The words the customer used.
        kind: "calendar" for a day and month, else a key of `SimulatedClock.relative_window`.
        count: Units back for days_ago, weeks_ago and months_ago; else None.
        day: Day of the month for "calendar"; else None.
        month: Month number for "calendar"; else None.
        year: Four-digit year for "calendar" when written; else None.
        evidence: Literal fragment of the message.
    """

    expression: str
    kind: DateKind
    count: int | None
    day: int | None
    month: int | None
    year: int | None


class ComprehensionReading(BaseModel):
    """What the LLM returns; `Comprehension` once the date is resolved by code.

    Every field is required, nullable where the clue can be absent, so the model states each
    one instead of leaving it out.

    Attributes:
        intent: One of INTENTS.
        amount: Amount clue or None.
        date: Date reading or None.
        merchant_hint: Merchant clue or None.
        channel_hint: Channel clue or None.
        card_in_possession: Card possession clue or None.
        language: Language variant of the message.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: Intent
    amount: AmountClue | None
    date: DateReading | None
    merchant_hint: TextClue | None
    channel_hint: ChannelClue | None
    card_in_possession: PossessionClue | None
    language: LanguageVariant


def _squash(text: str) -> str:
    return " ".join(text.split()).casefold()


def evidence_is_faithful(evidence: str, message: str) -> bool:
    """Checks that a fragment is a literal part of the message.

    Case and repeated whitespace are ignored; anything else, accents included, must match.

    Args:
        evidence: Fragment claimed as the source of a clue.
        message: Customer message.

    Returns:
        True when the fragment is a substring of the message.
    """
    fragment = _squash(evidence)
    return bool(fragment) and fragment in _squash(message)

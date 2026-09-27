"""Schema of an evaluation case (TRZ-42 CA3, design 6.3).

A base case is one real transaction (or, for claim status, one real complaint) of one cohort
customer, with the "now" of the case, the true value of every clue, how the customer states each
clue (the noise), the scenario and the expected action. A case is a base case rendered in one
language variant. The four variants of a base share everything but the message, so a difference
between them measures language and nothing else (design 6.6).

Every clue always carries its drawn form and value; `mentioned` only says whether it is in the
first message. The simulated client (TRZ-43) answers a question with the same form and value.
"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Variant = Literal["es-MX", "es-CO", "es-AR", "pt-BR"]
VARIANTS: tuple[Variant, ...] = ("es-MX", "es-CO", "es-AR", "pt-BR")
Split = Literal["dev", "calibration", "test"]
Provenance = Literal["generator_a", "generator_b", "handwritten"]
Category = Literal[
    "normal",
    "fraud_card_lost",
    "billing_amount",
    "duplicate_pending",
    "duplicate_approved",
    "ambiguous",
    "recognized",
    "high_amount",
    "approval_amount",
    "open_dispute",
    "no_match",
    "missing_data",
    "out_of_scope",
    "injection",
    "other_customer",
    "session_expired",
    "tool_failure",
    "multilingual",
    "claim_status",
]
Intent = Literal[
    "unrecognized_charge",
    "billing_error_amount",
    "billing_error_duplicate",
    "claim_status",
    "out_of_scope",
]
Action = Literal[
    "register_and_offer_block",
    "register_and_block",
    "register",
    "explain_and_watch",
    "recognized_closed",
    "analyst_approval",
    "escalate",
    "abstain_and_redirect",
    "report_claim_status",
    "security_blocked",
    "expired",
]
FirstStep = Literal["any", "ask_data", "show_options"]
AmountBand = Literal["up_to_500", "500_to_1000", "above_1000"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Truth(_Strict):
    """True values of the source record, as the bank holds them.

    Attributes:
        transaction_id: Source transaction; None for a claim status case.
        complaint_id: Source complaint of a claim status or open dispute case.
        timestamp: Local wall time of the transaction, naive like TRAZO_NOW.
        country_code: Home country of the customer.
        segment: Customer segment.
        product_type: Normalized product type.
        transaction_type: Purchase, Payment or Withdrawal.
        status: Approved or Pending.
        channel: Channel of the transaction.
        amount: Registered amount.
        currency: Registered currency.
        amount_usd: Amount in USD at the rate of the transaction date.
        local_amount: Amount in the local currency of the customer's country.
        local_currency: Local currency of the customer's country.
        merchant_name: Merchant; only purchases have one.
        merchant_category: Merchant category.
        city: City of the transaction.
        transaction_country: Country of the transaction.
        card_in_possession: Whether the customer still has the card (unrecognized charges).
        recognizes_after_detail: Whether the customer recognizes the charge once shown.
        claim_subcategory: Subcategory of the source complaint.
        claim_created: Local creation time of the source complaint.
        claim_state: State of the complaint at the case "now": open, in_process, resolved.
        open_dispute_claims: Dispute complaints of the customer open at the case "now".
    """

    transaction_id: str | None
    complaint_id: str | None = None
    timestamp: datetime | None
    country_code: str
    segment: str
    product_type: str | None
    transaction_type: str | None
    status: str | None
    channel: str | None
    amount: float | None
    currency: str | None
    amount_usd: float | None
    local_amount: float | None
    local_currency: str
    merchant_name: str | None
    merchant_category: str | None
    city: str | None
    transaction_country: str | None
    card_in_possession: bool | None = None
    recognizes_after_detail: bool = False
    claim_subcategory: str | None = None
    claim_created: datetime | None = None
    claim_state: str | None = None
    open_dispute_claims: int = 0


class AmountClue(_Strict):
    """How the customer states the amount.

    Attributes:
        mentioned: In the first message.
        form: exact, rounded or approximate.
        value: Stated value.
        currency: Stated currency.
        qualifier: None, "about" or "more_than".
        low: Lowest true amount, in the stated currency, compatible with the statement.
        high: Highest true amount, in the stated currency, compatible with the statement.
    """

    mentioned: bool
    form: Literal["exact", "rounded", "approximate"] | None
    value: float | None
    currency: str | None
    qualifier: Literal["about", "more_than"] | None = None
    low: float | None = None
    high: float | None = None


class DateClue(_Strict):
    """How the customer states the date.

    Attributes:
        mentioned: In the first message.
        form: exact or relative.
        expression: Language-neutral key of the expression, e.g. "yesterday", "last_week".
        window_start: First date the expression can mean, from the case "now".
        window_end: Last date the expression can mean.
    """

    mentioned: bool
    form: Literal["exact", "relative"] | None
    expression: str | None
    window_start: date | None
    window_end: date | None


class MerchantClue(_Strict):
    """How the customer names the merchant.

    Attributes:
        mentioned: In the first message.
        form: complete, partial, misspelled or category; None when there is no merchant.
        value: Stated text: the name, a part of it, a misspelling or the category.
    """

    mentioned: bool
    form: Literal["complete", "partial", "misspelled", "category"] | None
    value: str | None


class Mention(_Strict):
    """A clue whose value is the true one; only whether it is said is drawn.

    Attributes:
        mentioned: In the first message.
    """

    mentioned: bool


class Noise(_Strict):
    """The stated form of every clue.

    Attributes:
        amount: Amount clue.
        date: Date clue.
        merchant: Merchant clue.
        channel: Channel mention (value: truth.channel).
        product: Product type mention (value: truth.product_type).
        card_possession: Card possession mention (value: truth.card_in_possession).
    """

    amount: AmountClue
    date: DateClue
    merchant: MerchantClue
    channel: Mention
    product: Mention
    card_possession: Mention


class Scenario(_Strict):
    """What the harness has to stage around the conversation.

    Attributes:
        weak_clues: The first message fits several charges (ambiguous).
        nonexistent_charge: The description matches no charge of the customer (no match).
        agreed_amount: Amount the customer expected, in the registered currency (billing).
        topic: Out-of-scope topic: loan, branch, app, personal_data.
        injection: The message carries an instruction aimed at the system.
        other_customer_id: Cohort customer the message asks about (not the session's).
        session_expires_at_turn: Turn at which the session expires.
        tool_failure: Tool that fails when called.
        code_mixed: The message mixes languages.
        twin_transaction_id: Constructed duplicate of the source transaction.
        fixture_rows: Constructed rows the harness inserts before the case and removes after.
    """

    weak_clues: bool = False
    nonexistent_charge: bool = False
    agreed_amount: float | None = None
    topic: str | None = None
    injection: bool = False
    other_customer_id: str | None = None
    session_expires_at_turn: int | None = None
    tool_failure: str | None = None
    code_mixed: bool = False
    twin_transaction_id: str | None = None
    fixture_rows: list[dict[str, Any]] = []


class Expected(_Strict):
    """Expected outcome, computed by `pipeline.cases.labels` from design 8.

    Attributes:
        action: Final action the system must reach.
        rule: The rule of the design that decides it.
        first_step: What the system must do before acting; `any` when the case does not
            constrain it.
        amount_band: USD band of the charge, when there is one.
    """

    action: Action
    rule: str
    first_step: FirstStep
    amount_band: AmountBand | None


class BaseCase(_Strict):
    """Everything a case knows before it is written in a variant.

    Attributes:
        base_id: Opaque id, shared by the four variants.
        split: dev, calibration or test.
        provenance: generator_a, generator_b or handwritten.
        category: Case type of design 6.3.
        intent: Intent the customer has.
        customer_id: Customer of the session; never sent to an LLM.
        now: Local "now" of the case for the simulated clock.
        truth: True values.
        noise: Stated form of each clue.
        scenario: What the harness stages.
        expected: Expected outcome.
    """

    base_id: str
    split: Split
    provenance: Provenance
    category: Category
    intent: Intent
    customer_id: str
    now: datetime
    truth: Truth
    noise: Noise
    scenario: Scenario
    expected: Expected


class CaseRecord(BaseCase):
    """A base case written in one language variant.

    Attributes:
        case_id: base_id plus the variant.
        variant: es-MX, es-CO, es-AR or pt-BR.
        language: es or pt.
        message: First message of the customer.
        non_native_writer: Handwritten by someone who is not a native speaker of the variant.
        message_source: llm, template (LLM output rejected or unavailable) or handwritten.
        versions: Generator, templates, prompt and model that produced the message.
    """

    case_id: str
    variant: Variant
    language: Literal["es", "pt"]
    message: str
    non_native_writer: bool = False
    message_source: Literal["llm", "template", "handwritten"]
    versions: dict[str, str]

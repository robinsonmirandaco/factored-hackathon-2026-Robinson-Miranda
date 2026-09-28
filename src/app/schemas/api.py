"""Request and response contracts of the HTTP API."""

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator


class HealthOut(BaseModel):
    """Liveness and configuration summary.

    Attributes:
        llm_provider: Configured provider (anthropic or local).
        llm_available: True only when real LLM calls can be made; false means every turn
            uses the deterministic fallback (no key, no base URL, or LLM disabled).
    """

    status: str
    app_env: str
    db: str
    llm_provider: str
    llm_available: bool


class ChatIn(BaseModel):
    """One customer turn. The customer is the one of the session token.

    Attributes:
        customer_id: Ignored: the customer comes only from the session. One that differs from
            the session customer raises a security event (TRZ-09 CA6).
        message: Raw message; PII is redacted before any LLM call or audit write. For a button,
            its label.
        case_id: Existing case to continue, or None to open a new one.
        confirm_action_id: The `action_id` of the pending action the customer confirms. An
            action that was replaced, cancelled or belongs to another case runs nothing; one
            already executed returns the same result again.
        recognition: The button pressed at the recognition step of `case_id`.
        option: The `transaction_id` of the option chosen in `case_id` (the `claim_id` in a
            claim status case), or `none` when none of the options shown is the one. Any id that
            was not shown stops the case for security.
    """

    customer_id: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
    case_id: str | None = Field(default=None, max_length=64)
    confirm_action_id: str | None = Field(default=None, pattern=r"^ACT-[0-9A-F]{10}$")
    recognition: Literal["not_recognized", "recognized"] | None = None
    option: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _one_answer(self) -> Self:
        answers = [self.confirm_action_id, self.recognition, self.option]
        if sum(a is not None for a in answers) > 1:
            raise ValueError("send at most one of confirm_action_id, recognition and option")
        if any(a is not None for a in answers) and self.case_id is None:
            raise ValueError("confirm_action_id, recognition and option need a case_id")
        return self


class OtpRequestIn(BaseModel):
    """A request for a one-time code.

    Attributes:
        document_type: Identity document type, such as CC, DNI or Pasaporte.
        document_number: Identity document number.
    """

    document_type: str = Field(min_length=1, max_length=32)
    document_number: str = Field(min_length=1, max_length=32)


class OtpRequestOut(BaseModel):
    """The same answer for every document, whether a customer has it or not.

    Attributes:
        status: Always code_sent.
        expires_in_seconds: How long a code stays valid.
    """

    status: Literal["code_sent"] = "code_sent"
    expires_in_seconds: int


class OtpVerifyIn(OtpRequestIn):
    """A one-time code for a document.

    Attributes:
        code: The six-digit code.
    """

    code: str = Field(pattern=r"^\d{6}$")


class AnalystLoginIn(BaseModel):
    """Test credentials of an analyst."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenOut(BaseModel):
    """A new session.

    Attributes:
        access_token: JWT to send as `Authorization: Bearer <token>`.
        role: customer or analyst.
        expires_in_seconds: Absolute lifetime of the session.
        idle_timeout_seconds: Inactivity after which the session expires.
    """

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    role: Literal["customer", "analyst"]
    expires_in_seconds: int
    idle_timeout_seconds: int


class TwinOut(BaseModel):
    """Another charge of the same merchant, amount and currency."""

    at: datetime
    status: str


class ChargeOut(BaseModel):
    """The charge shown at the recognition step, read from the database (TRZ-16 CA1).

    Attributes:
        transaction_id: Id of the charge.
        transaction_type: Purchase, Payment or Withdrawal.
        merchant: Merchant; only purchases have one.
        amount: Registered amount, the primary figure.
        currency: Registered currency.
        converted_amount: Approximate amount in the local currency, when it differs.
        converted_currency: Local currency of the customer.
        converted_label: Label of the converted figure.
        at: Local date and time of the charge.
        channel: Channel of the charge.
        city: City of the charge; there is never an address.
        product_type: Type of the product charged.
        last4: Last four digits of that product.
        status: Approved or Pending.
        twin: An identical charge of the same merchant, when there is one.
        earlier_months: Earlier months with charges of the same merchant, as YYYY-MM.
    """

    transaction_id: str
    transaction_type: str
    merchant: str | None
    amount: float
    currency: str
    converted_amount: float | None
    converted_currency: str
    converted_label: str | None
    at: datetime
    channel: str
    city: str | None
    product_type: str
    last4: str | None
    status: str
    twin: TwinOut | None
    earlier_months: list[str]


class ChoiceOut(BaseModel):
    """A button of the recognition step; the first one is the primary."""

    id: Literal["not_recognized", "recognized"]
    label: str


class OptionOut(BaseModel):
    """A charge shown as an option; send its `transaction_id` as `option` to choose it."""

    transaction_id: str
    merchant: str | None
    amount: float
    currency: str
    date: str


class ClaimOut(BaseModel):
    """An open claim shown as an option; send its `claim_id` as `option` to choose it.

    Attributes:
        claim_id: Folio of a dispute (DSP-...) or id of a complaint (CMP-...).
        source: Table the claim was read from, disputes or complaints.
        opened_on: Day it was registered or created.
    """

    claim_id: str
    source: str
    opened_on: str


class PendingActionOut(BaseModel):
    """An action waiting for the customer's confirmation.

    Attributes:
        action_id: What `confirm_action_id` must name to run it.
        action: register, register_and_offer_block or register_and_block.
    """

    action_id: str
    action: str


class ChatOut(BaseModel):
    """What the system did with one customer turn.

    Attributes:
        charge: The charge to recognize, when `outcome` is recognizing.
        choices: The buttons of the recognition step, primary first.
        options: The charges to choose from, when several fit.
        claims: The open claims to choose from, when the customer has several.
        pending_action: The action to confirm, when `outcome` is awaiting_confirmation.
        dispute_folio: Folio DSP-AAAA-NNNNN of the dispute registered in this turn.
    """

    case_id: str
    trace_id: str
    intent: str
    reply: str
    outcome: str
    autonomy_level: str
    actions_taken: list[str]
    llm_fallback: bool
    tokens: int
    latency_ms: int
    charge: ChargeOut | None = None
    choices: list[ChoiceOut] = Field(default_factory=list)
    options: list[OptionOut] = Field(default_factory=list)
    claims: list[ClaimOut] = Field(default_factory=list)
    pending_action: PendingActionOut | None = None
    dispute_folio: str | None = None


class CaseOut(BaseModel):
    """A case as shown to the operator."""

    case_id: str
    customer_id: str
    transaction_id: str | None
    intent: str
    status: str
    autonomy_level: str
    recommended_action: str | None
    escalation_reason: str | None
    human_decision: str | None
    summary: str | None
    trace_id: str
    created_at: str | None


class TraceEventOut(BaseModel):
    """One audit log row of a case."""

    id: int
    actor: str
    action: str
    payload: dict[str, Any] | None
    result: dict[str, Any] | None
    latency_ms: int | None
    at: str


class HistoryEntryOut(BaseModel):
    """One step of a case told in plain language, written by code from its audit row.

    Attributes:
        id: Audit row id, to open the raw row in the trace.
        at: When the step happened.
        trace_id: Request that wrote the step.
        actor: Who acted.
        action: What was done.
        text: The step in the requested language.
    """

    id: int
    at: str
    trace_id: str
    actor: str
    action: str
    text: str


class HumanDecisionIn(BaseModel):
    """Operator decision on an escalated case.

    Attributes:
        decision: approve, reject or need_info.
        note: Optional free text; PII is redacted before it is stored.
        agent_id: Operator identifier.
    """

    decision: Literal["approve", "reject", "need_info"]
    note: str | None = Field(default=None, max_length=1000)
    agent_id: str = Field(default="human", min_length=1, max_length=64)


class MetricsOut(BaseModel):
    """Operational counters read from the cases table and the audit log."""

    cases_by_status: dict[str, int]
    cases_by_level: dict[str, int]
    turns: int
    avg_turn_latency_ms: float
    human_decisions: int

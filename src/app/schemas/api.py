"""Request and response contracts of the HTTP API."""

from datetime import date, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator


class HealthOut(BaseModel):
    """Liveness and configuration summary.

    Attributes:
        llm_provider: The LLM provider (anthropic).
        llm_available: True only when real LLM calls can be made; false means every turn
            uses the deterministic fallback (no key, or LLM disabled).
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
        transaction_id: The charge a "No lo reconozco" or "¿Qué es esto?" button of Movimientos
            was pressed on; it opens a new case, so it goes without `case_id`. `message` is the
            button's label.
        decline_action_id: The `action_id` of the pending action the customer declines ("No,
            gracias"); it is cancelled and nothing runs.
    """

    customer_id: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
    case_id: str | None = Field(default=None, max_length=64)
    confirm_action_id: str | None = Field(default=None, pattern=r"^ACT-[0-9A-F]{10}$")
    recognition: Literal["not_recognized", "recognized"] | None = None
    option: str | None = Field(default=None, min_length=1, max_length=64)
    transaction_id: str | None = Field(default=None, min_length=1, max_length=64)
    decline_action_id: str | None = Field(default=None, pattern=r"^ACT-[0-9A-F]{10}$")

    @model_validator(mode="after")
    def _one_answer(self) -> Self:
        answers = [self.confirm_action_id, self.recognition, self.option, self.decline_action_id]
        if sum(a is not None for a in answers) > 1:
            raise ValueError(
                "send at most one of confirm_action_id, decline_action_id, recognition and option"
            )
        if any(a is not None for a in answers) and self.case_id is None:
            raise ValueError(
                "confirm_action_id, decline_action_id, recognition and option need a case_id"
            )
        if self.transaction_id is not None and (
            self.case_id is not None or any(a is not None for a in answers)
        ):
            raise ValueError("transaction_id opens a new case: send it alone, without case_id")
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


class ClueOut(BaseModel):
    """A chip of what the system read in the customer's message (design 10.1).

    Attributes:
        field: amount, date, merchant_hint, channel_hint or card_in_possession.
        value: The value read; yes or no for card_in_possession.
        evidence: The literal fragment of the redacted message the value was read from.
        window_from: For a date, the first day it can mean, counted from the simulated now.
        window_to: For a date, the last day it can mean.
    """

    field: Literal["amount", "date", "merchant_hint", "channel_hint", "card_in_possession"]
    value: str
    evidence: str
    window_from: date | None = None
    window_to: date | None = None


class PendingActionOut(BaseModel):
    """An action waiting for the customer's confirmation.

    Attributes:
        action_id: What `confirm_action_id` must name to run it, or `decline_action_id` to
            decline it.
        action: register, register_and_offer_block, register_and_block, or block (the block
            offered after a registration).
        merchant: Merchant of the charge it acts on.
        amount: Registered amount of that charge.
        currency: Its currency.
        last4: Last four digits of the card of that charge.
    """

    action_id: str
    action: str
    merchant: str | None = None
    amount: float | None = None
    currency: str | None = None
    last4: str | None = None


class ChatOut(BaseModel):
    """What the system did with one customer turn.

    Attributes:
        charge: The charge to recognize, when `outcome` is recognizing.
        choices: The buttons of the recognition step, primary first.
        options: The charges to choose from, when several fit.
        claims: The open claims to choose from, when the customer has several.
        pending_action: The action to confirm, when `outcome` is awaiting_confirmation.
        dispute_folio: Folio DSP-AAAA-NNNNN of the dispute registered in this turn.
        clues: What was read in the customer's message, each with its literal fragment; empty
            for a button, a choice or a confirmation.
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
    clues: list[ClueOut] = Field(default_factory=list)


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


QueueFilter = Literal["high_priority", "over_1000_usd", "no_match", "verification_failed", "audit"]


class QueueItemOut(BaseModel):
    """One open row of the analyst queue (TRZ-27 CA1).

    A security event shows no customer data: no customer, intent or amount (CA8).

    Attributes:
        queue_id: Row of case_queue; a decision closes it.
        case_id: The case.
        kind: escalation, audit_sample or security_event.
        customer_id: Customer of the case; None for a security event.
        intent: What the case is about; None for a security event.
        amount_usd: USD amount of the identified charge, as the policy compared it; None when
            there is no charge, it was not convertible, or it is a security event.
        language: es or pt.
        reason: Rule that handed the case to a person.
        priority: normal, high or urgent.
        sla_due_at: End of the SLA of its priority, on the real clock.
        overdue: The SLA already ended.
        updated: The case came back with the customer's answer (TRZ-28).
        status: Status of the case.
        recommended_action: What the system suggests.
        can_approve: Approving has something to do: run the recommended registration, or
            close a security event.
        created_at: When the row entered the queue.
    """

    queue_id: int
    case_id: str
    kind: Literal["escalation", "audit_sample", "security_event"]
    customer_id: str | None
    intent: str | None
    amount_usd: float | None
    language: str | None
    reason: str | None
    priority: str
    sla_due_at: datetime | None
    overdue: bool
    updated: bool
    status: str
    recommended_action: str | None
    can_approve: bool
    created_at: datetime


class QueueOut(BaseModel):
    """The analyst queue with one counter per filter (TRZ-27 CA2).

    Attributes:
        items: Open rows of the filter asked for, most urgent first.
        counts: Rows of each filter, and "all"; each one is the length of that filter's items.
    """

    items: list[QueueItemOut]
    counts: dict[str, int]


class HumanDecisionIn(BaseModel):
    """Analyst decision on a case in the queue. The analyst comes from the session.

    Required fields depend on the decision and are checked by the service, which answers 400:
    a rejection needs a reason of the closed list, a request for information needs the question.

    Attributes:
        decision: approve, reject or need_info.
        reason: For a rejection, one of `autonomy.reversal_reasons` of the policy.
        question: For need_info, what the customer is asked; PII is redacted before it is
            stored and it is shown to the customer as written.
        note: Optional free text; PII is redacted before it is stored.
    """

    decision: Literal["approve", "reject", "need_info"]
    reason: str | None = Field(default=None, max_length=64)
    question: str | None = Field(default=None, max_length=1000)
    note: str | None = Field(default=None, max_length=1000)


class DecisionOut(BaseModel):
    """What an analyst decision did.

    Attributes:
        case_id: The case.
        decision: approve, reject or need_info.
        status: Status of the case after the decision.
        dispute_folio: Folio of the dispute an approval registered and verified.
        block_not_executed: The recommendation included a card block, which an approval does
            not run: it needs the customer's explicit confirmation (design 3.2).
        due_on: For need_info, the last business day for the customer's answer.
    """

    case_id: str
    decision: str
    status: str
    dispute_folio: str | None = None
    block_not_executed: bool = False
    due_on: date | None = None


class InfoRequestOut(BaseModel):
    """The analyst's question on a clarification, as the customer sees it (TRZ-28).

    Attributes:
        id: Row of info_requests.
        question: The question, PII-redacted, as the analyst wrote it.
        asked_on: Simulated day it was asked.
        due_on: Last business day to answer, with the country's holidays.
        overdue: The deadline is before the simulated today.
        status: open, answered or expired.
        answered_at: When the customer answered.
        answer: The customer's answer as the system stored it, PII-redacted.
    """

    id: int
    question: str
    asked_on: date
    due_on: date
    overdue: bool
    status: str
    answered_at: datetime | None
    answer: str | None = None


class InfoReplyIn(BaseModel):
    """The customer's answer to the analyst's question.

    Attributes:
        text: The answer; PII is redacted before it is stored.
    """

    text: str = Field(min_length=1, max_length=2000)


class InfoReplyOut(BaseModel):
    """The case after the customer answered.

    Attributes:
        case_id: The case.
        status: Status the case went back to, with a person again.
        answered_at: When the answer was stored.
    """

    case_id: str
    status: str
    answered_at: datetime


class MetricsOut(BaseModel):
    """Operational counters read from the cases table and the audit log."""

    cases_by_status: dict[str, int]
    cases_by_level: dict[str, int]
    turns: int
    avg_turn_latency_ms: float
    human_decisions: int


# ---- the customer's own screens (TRZ-34) ------------------------------------------------


class MeOut(BaseModel):
    """The session customer, for the header of the customer screens.

    Attributes:
        first_name: First name as registered.
        country_code: MX, CO or AR.
        local_currency: Currency of the customer's country.
        now: The simulated now every date of the screens is counted from (design 10.2, rule 7).
        demo: Demo mode is on: the screens show the audit view and its simulated controls.
    """

    first_name: str | None
    country_code: str
    local_currency: str
    now: datetime
    demo: bool


class ProductOut(BaseModel):
    """A product of the session customer; only the last four digits of its number exist."""

    product_id: str
    product_type: str
    last4: str | None
    currency: str
    current_balance: float | None
    credit_limit: float | None
    status: str


class MovementOut(BaseModel):
    """A transaction of the session customer.

    Attributes:
        amount: Registered amount, the primary figure.
        converted_amount: Approximate amount in the local currency at the rate of the
            transaction's day, when the currencies differ and a rate exists.
        converted_label: Label of the converted figure.
        disputable: A charge of the dispute window: the screen offers "No lo reconozco" on it,
            or "¿Qué es esto?" when it is pending.
    """

    transaction_id: str
    at: datetime
    transaction_type: str
    merchant: str | None
    amount: float
    currency: str
    converted_amount: float | None
    converted_currency: str
    converted_label: str | None
    channel: str
    city: str | None
    status: str
    disputable: bool


class MovementsOut(BaseModel):
    """A page of transactions, newest first.

    Attributes:
        items: The transactions of the page.
        next_before: Cursor of the next page (the last transaction's id), or None at the end.
    """

    items: list[MovementOut]
    next_before: str | None


class ClarificationOut(BaseModel):
    """One of the customer's clarifications: a dispute registered by TRAZO, a case with a
    person, or an open claim of the bank.

    Attributes:
        id: Folio of a dispute, case id of a case, or complaint id of the bank's records.
        source: disputes, cases or complaints.
        status: registered for a dispute; the case status for a case; received, in_review or
            answered for a claim.
        case_id: The TRAZO case of a dispute or case.
        intent: What it is about: the dispute type, or the case's intent.
        folio: Folio of the registered dispute.
        merchant: Merchant of the charge, when known.
        amount: Amount of the charge; for a dispute, the amount it was registered with.
        currency: Its currency.
        charge_at: Local date and time of that charge.
        opened_on: Business day it was registered; only disputes and claims have one.
        due_date: Response deadline, in business days with the country's holidays.
        overdue: The deadline is before the simulated today.
        passage_id: The demo policy passage that backs the deadline.
        review_hours: For a case with a person, the review time of its queue priority, the
            one the chat gave (demo policy).
        info_request: The analyst's latest question on the case, if any (TRZ-28).
    """

    id: str
    source: Literal["disputes", "cases", "complaints"]
    status: str
    case_id: str | None = None
    intent: str | None = None
    folio: str | None = None
    merchant: str | None = None
    amount: float | None = None
    currency: str | None = None
    charge_at: datetime | None = None
    opened_on: str | None = None
    due_date: str | None = None
    overdue: bool = False
    passage_id: str | None = None
    review_hours: float | None = None
    info_request: InfoRequestOut | None = None

"""Request and response contracts of the HTTP API."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class HealthOut(BaseModel):
    """Liveness and configuration summary.

    Attributes:
        llm_provider: Configured provider (anthropic or local).
        llm_available: True only when real LLM calls can be made; false means every turn
            uses the deterministic fallback (no key, no base URL, or LLM disabled).
    """

    status: str
    env: str
    db: str
    llm_provider: str
    llm_available: bool


class ChatIn(BaseModel):
    """One customer turn.

    Attributes:
        customer_id: Customer sending the message.
        message: Raw message; PII is redacted before any LLM call or audit write.
        case_id: Existing case to continue, or None to open a new one.
        confirm: True when the customer confirms a pending action.
    """

    customer_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
    case_id: str | None = Field(default=None, max_length=64)
    confirm: bool = False


class ChatOut(BaseModel):
    """What the system did with one customer turn."""

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

"""Append-only audit log writer, called by every component that decides or acts."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.db.models import AuditRecord
from app.adapters.llm import LLMCallStats
from app.core.logging import get_logger, trace_id_var

log = get_logger("audit")


# Keys under which a model would hand back its own reasoning. The statement rules that hidden
# reasoning is not an audit artifact, so a row carrying one is refused instead of trimmed.
REASONING_KEYS = frozenset(
    {"reasoning", "thinking", "rationale", "chain_of_thought", "explanation"}
)


class ReasoningInAuditError(ValueError):
    """A payload or result offered to the audit log carries model reasoning."""


def _reasoning_key(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, inner in value.items():
            if str(key).lower() in REASONING_KEYS:
                return str(key)
            found = _reasoning_key(inner)
            if found:
                return found
    elif isinstance(value, list | tuple):
        for inner in value:
            found = _reasoning_key(inner)
            if found:
                return found
    return None


def write_audit(
    session: Session,
    actor: str,
    action: str,
    case_id: str | None = None,
    payload: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    latency_ms: int | None = None,
    idempotency_key: str | None = None,
    customer_id: str | None = None,
    llm: LLMCallStats | None = None,
    verified: bool | None = None,
) -> AuditRecord:
    """Appends one row to the audit log under the current trace_id.

    Callers must pass PII-redacted payloads; this function stores what it receives. The row
    belongs to the customer of the session unless another one is given, so row level security
    lets that customer's later requests find it (idempotency lookups read the audit log). The
    policy version comes from the session, bound once per request.

    Args:
        session: Open database session.
        actor: Who acted: agent, tool, policy, human or system.
        action: What was done.
        case_id: Case the row belongs to, if any.
        payload: Inputs of the action.
        result: Outputs of the action.
        latency_ms: Duration of the action.
        idempotency_key: Unique key for state-changing actions.
        customer_id: Customer the row is about; defaults to the customer of the session.
        llm: Usage of the LLM calls made by the step; None when the step made none.
        verified: Outcome of reading an action back; None on rows that are not a read-back.

    Returns:
        The stored audit record.

    Raises:
        ReasoningInAuditError: If the payload or the result holds a reasoning key.
    """
    key = _reasoning_key(payload) or _reasoning_key(result)
    if key:
        raise ReasoningInAuditError(f"audit row {actor}/{action} carries model reasoning: {key}")
    rec = AuditRecord(
        trace_id=trace_id_var.get(),
        case_id=case_id,
        customer_id=customer_id or session.info.get("customer_id"),
        actor=actor,
        action=action,
        payload=payload,
        result=result,
        latency_ms=latency_ms,
        idempotency_key=idempotency_key,
        policy_version=session.info.get("policy_version"),
        verified=verified,
    )
    if llm is not None:
        # Cached prompt tokens are still prompt tokens; the cost already prices them apart.
        rec.input_tokens = llm.input_tokens + llm.cache_write_tokens + llm.cache_read_tokens
        rec.output_tokens = llm.output_tokens
        rec.cost_usd = round(llm.cost_usd, 6)
        rec.model = llm.model or None
        rec.prompt_version = llm.prompt_version
    session.add(rec)
    session.flush()
    log.info("audit", actor=actor, action=action, case_id=case_id, latency_ms=latency_ms)
    return rec


@contextmanager
def timed() -> Iterator[dict[str, int]]:
    """Measures the duration of a block.

    Yields:
        A dict whose "ms" key holds the elapsed milliseconds once the block exits.
    """
    box: dict[str, int] = {}
    t0 = time.perf_counter()
    try:
        yield box
    finally:
        box["ms"] = int((time.perf_counter() - t0) * 1000)

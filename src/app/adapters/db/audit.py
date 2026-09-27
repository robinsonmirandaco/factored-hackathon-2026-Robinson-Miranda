"""Append-only audit log writer, called by every component that decides or acts."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.db.models import AuditRecord
from app.core.logging import get_logger, trace_id_var

log = get_logger("audit")


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
) -> AuditRecord:
    """Appends one row to the audit log under the current trace_id.

    Callers must pass PII-redacted payloads; this function stores what it receives. The row
    belongs to the customer of the session unless another one is given, so row level security
    lets that customer's later requests find it (idempotency lookups read the audit log).

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

    Returns:
        The stored audit record.
    """
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
    )
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

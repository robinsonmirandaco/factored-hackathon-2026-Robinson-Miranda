"""Operator-side use cases: read cases, traces, histories and the queue, compute metrics."""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import func, select, text, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.db.models import AuditRecord, Case, QueueItem
from app.core.errors import AppError
from app.core.time import utcnow
from app.domain.history import Lang, describe
from app.schemas.api import (
    CaseOut,
    HistoryEntryOut,
    MetricsOut,
    QueueFilter,
    QueueItemOut,
    QueueOut,
    TraceEventOut,
)

_HISTORY = text(
    "SELECT id, trace_id, actor, action, payload, result, policy_version, created_at "
    "FROM case_history WHERE case_id = :case_id ORDER BY id"
)

# A person decides these cases: an escalation, an action prepared for analyst approval, a case
# stopped by a security rule, and an action whose read-back did not match.
HANDOFF_STATUSES = ("escalated", "pending_analyst_approval", "security_blocked", "failed")

# Intents that are about one charge.
DISPUTE_INTENTS = ("unrecognized_charge", "billing_error_amount", "billing_error_duplicate")
# Recommendations an analyst's approval can run, and whether they include a card block it
# leaves out (design 3.2: a block needs the customer's explicit confirmation).
REGISTERS = {"register": False, "register_and_offer_block": True, "register_and_block": True}
_PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2}
# Queue filters of TRZ-27 CA2 read the reason of the queue row: no charge fits the clues, or an
# action whose read-back did not match (the policy rule, or the reason written after acting).
NO_MATCH_REASON = "escalate.conformal_set_empty"
# Escalation reason of a case whose read-back after acting did not match (TRZ-19 CA3).
VERIFICATION_FAILED_REASON = "verification.registration_failed"
VERIFICATION_REASONS = ("escalate.verification_failed", VERIFICATION_FAILED_REASON)


def check_database(session: Session) -> None:
    """Runs a trivial query to prove the database answers.

    Args:
        session: Open database session.

    Raises:
        AppError: 503 db_unavailable if the query fails.
    """
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc


def get_case(session: Session, case_id: str) -> CaseOut:
    """Reads one case.

    Args:
        session: Open database session.
        case_id: Case to read.

    Returns:
        The case.

    Raises:
        AppError: 404 case_not_found.
    """
    return _to_out(_require_case(session, case_id))


def get_trace(session: Session, case_id: str) -> list[TraceEventOut]:
    """Lists the audit rows of a case in write order.

    Args:
        session: Open database session.
        case_id: Case whose trace to read.

    Returns:
        The audit rows; empty if the case has none.
    """
    rows = (
        session.execute(
            select(AuditRecord).where(AuditRecord.case_id == case_id).order_by(AuditRecord.id)
        )
        .scalars()
        .all()
    )
    return [
        TraceEventOut(
            id=r.id,
            actor=r.actor,
            action=r.action,
            payload=r.payload,
            result=r.result,
            latency_ms=r.latency_ms,
            at=r.created_at.isoformat(),
        )
        for r in rows
    ]


def get_history(session: Session, case_id: str, lang: Lang) -> list[HistoryEntryOut]:
    """Tells the steps of a case in plain language, read from the case_history view.

    Args:
        session: Open database session.
        case_id: Case whose history to read.
        lang: Language of the lines.

    Returns:
        One line per audit row of the case, in write order.

    Raises:
        AppError: 404 case_not_found, or 503 db_unavailable if the database fails.
    """
    try:
        _require_case(session, case_id)
        rows = session.execute(_HISTORY, {"case_id": case_id}).mappings().all()
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc
    return [
        HistoryEntryOut(
            id=r["id"],
            at=r["created_at"].isoformat(),
            trace_id=r["trace_id"],
            actor=r["actor"],
            action=r["action"],
            text=describe(
                r["actor"], r["action"], r["payload"], r["result"], r["policy_version"], lang
            ),
        )
        for r in rows
    ]


def list_queue(
    session: Session,
    human_review_above: float,
    only: QueueFilter | None = None,
    audit_sample_rate: float = 0.0,
) -> QueueOut:
    """Lists the open rows of the queue a person must decide, most urgent first (TRZ-27).

    The queue is case_queue, not the case status: a case is listed while its row is open, and
    a decision closes the row. Rows are ordered by priority, then by the end of their SLA.

    Args:
        session: Open session with the analyst role.
        human_review_above: USD amount above which a case is a large one, from the policy.
        only: Filter to apply; None lists every open row.
        audit_sample_rate: rho of the policy, returned so the console labels an audit sample.

    Returns:
        The rows of the filter and the counter of every filter over the same open rows.

    Raises:
        AppError: 503 db_unavailable if the database fails.
    """
    try:
        rows = session.execute(
            select(QueueItem, Case)
            .join(Case, Case.id == QueueItem.case_id)
            .where(QueueItem.resolved_at.is_(None))
        ).all()
        now = utcnow()
        items = [_queue_item(session, q, c, now) for q, c in rows]
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc
    items.sort(key=lambda i: (_PRIORITY_RANK[i.priority], i.sla_due_at or datetime.max, i.queue_id))
    tests: dict[str, Callable[[QueueItemOut], bool]] = {
        "high_priority": lambda i: i.priority in ("high", "urgent"),
        "over_1000_usd": lambda i: i.amount_usd is not None and i.amount_usd > human_review_above,
        "no_match": lambda i: i.reason == NO_MATCH_REASON,
        "verification_failed": lambda i: i.status == "failed" or i.reason in VERIFICATION_REASONS,
        "audit": lambda i: i.kind == "audit_sample",
    }
    counts = {"all": len(items)} | {k: sum(map(f, items)) for k, f in tests.items()}
    return QueueOut(
        items=[i for i in items if only is None or tests[only](i)],
        counts=counts,
        audit_sample_rate=audit_sample_rate,
    )


def approvable(case: Case) -> bool:
    """Tells whether approving the case has a registration to run.

    Args:
        case: A case with a person.

    Returns:
        True when the recommendation registers a dispute on an identified charge.
    """
    return (
        case.recommended_action in REGISTERS
        and case.transaction_id is not None
        and case.intent in DISPUTE_INTENTS
    )


def injection_stop(session: Session, case: Case) -> bool:
    """Tells whether the case was stopped for an instruction injected in its message.

    TRZ-27 CA8 hides a security event because it tried to reach another customer's data. In an
    injection the data are the customer's own: the analyst sees them and decides the case like
    any other (TRZ-46 follow-up).

    Args:
        session: Open session.
        case: The case.

    Returns:
        True when its latest security event was raised by an injected instruction.
    """
    event = session.execute(
        select(AuditRecord)
        .where(
            AuditRecord.case_id == case.id,
            AuditRecord.actor == "agent",
            AuditRecord.action == "security_event",
        )
        .order_by(AuditRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return event is not None and (event.payload or {}).get("reason") == "instruction_in_text"


def _queue_item(session: Session, q: QueueItem, c: Case, now: datetime) -> QueueItemOut:
    injection = c.status == "security_blocked" and injection_stop(session, c)
    # Another customer's data is never shown (CA8); an injection is the customer's own case.
    security = c.status == "security_blocked" and not injection
    return QueueItemOut(
        queue_id=q.id,
        case_id=c.id,
        kind="security_event" if security or injection else q.kind,  # type: ignore[arg-type]
        injection=injection,
        # A security event shows no customer data (CA8).
        customer_id=None if security else c.customer_id,
        intent=None if security else c.intent,
        amount_usd=None if security else _amount_usd(session, c.id),
        language=c.language,
        reason=q.reason,
        priority=q.priority,
        sla_due_at=q.sla_due_at,
        overdue=q.sla_due_at is not None and q.sla_due_at < now,
        updated=q.updated,
        status=c.status,
        recommended_action=None if security else c.recommended_action,
        # Approving a security event closes it; any other case needs a registration to run.
        can_approve=security or approvable(c),
        created_at=q.created_at,
        simulated=c.simulated,
    )


def _amount_usd(session: Session, case_id: str) -> float | None:
    """The USD amount the policy compared when it handed the case over."""
    decide = session.execute(
        select(AuditRecord)
        .where(
            AuditRecord.case_id == case_id,
            # The reading beside an injected instruction compares the amount after the stop.
            tuple_(AuditRecord.actor, AuditRecord.action).in_(
                [("policy", "decide"), ("agent", "read_beside_instruction")]
            ),
        )
        .order_by(AuditRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    value = ((decide.payload if decide else None) or {}).get("amount_usd")
    return None if value is None else float(value)


def get_metrics(session: Session) -> MetricsOut:
    """Computes operational counters from the cases table and the audit log.

    Args:
        session: Open database session.

    Returns:
        The metrics.
    """
    by_status = session.execute(select(Case.status, func.count()).group_by(Case.status)).all()
    by_level = session.execute(
        select(Case.autonomy_level, func.count()).group_by(Case.autonomy_level)
    ).all()
    turns = session.execute(
        select(func.count(), func.avg(AuditRecord.latency_ms)).where(
            AuditRecord.action == "turn_complete"
        )
    ).one()
    decisions = session.execute(
        select(func.count()).where(AuditRecord.actor == "human", AuditRecord.action == "decision")
    ).scalar_one()
    return MetricsOut(
        cases_by_status={k: int(v) for k, v in by_status},
        cases_by_level={k: int(v) for k, v in by_level},
        turns=int(turns[0] or 0),
        avg_turn_latency_ms=round(float(turns[1] or 0), 1),
        human_decisions=int(decisions),
    )


def _require_case(session: Session, case_id: str) -> Case:
    case = session.get(Case, case_id)
    if case is None:
        raise AppError("case_not_found", f"Case {case_id} not found.", 404)
    return case


def _to_out(c: Case) -> CaseOut:
    return CaseOut(
        case_id=c.id,
        customer_id=c.customer_id,
        transaction_id=c.transaction_id,
        intent=c.intent,
        status=c.status,
        autonomy_level=c.autonomy_level,
        recommended_action=c.recommended_action,
        escalation_reason=c.escalation_reason,
        human_decision=c.human_decision,
        summary=c.summary,
        trace_id=c.trace_id,
        created_at=c.created_at.isoformat() if c.created_at else None,
        simulated=c.simulated,
    )

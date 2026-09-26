"""Operator-side use cases: read cases and traces, record human decisions, compute metrics."""

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import AuditRecord, Case
from app.core.errors import AppError
from app.domain.pii import redact
from app.schemas.api import CaseOut, HumanDecisionIn, MetricsOut, TraceEventOut

_STATUS_AFTER_DECISION = {
    "approve": "approved",
    "reject": "rejected",
    "need_info": "awaiting_customer",
}


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


def list_queue(session: Session) -> list[CaseOut]:
    """Lists escalated cases, oldest first.

    Args:
        session: Open database session.

    Returns:
        The escalation queue.
    """
    rows = (
        session.execute(select(Case).where(Case.status == "escalated").order_by(Case.created_at))
        .scalars()
        .all()
    )
    return [_to_out(c) for c in rows]


def record_decision(session: Session, case_id: str, body: HumanDecisionIn) -> CaseOut:
    """Stores an operator decision on an escalated case next to the system recommendation.

    The free-text note is PII-redacted before it reaches the audit log.

    Args:
        session: Open database session.
        case_id: Escalated case.
        body: The decision.

    Returns:
        The updated case.

    Raises:
        AppError: 404 case_not_found, or 409 case_not_escalated.
    """
    case = _require_case(session, case_id)
    if case.status != "escalated":
        raise AppError("case_not_escalated", f"Case is {case.status}, not escalated.", 409)
    case.human_decision = body.decision
    case.status = _STATUS_AFTER_DECISION[body.decision]
    note = redact(body.note)[0] if body.note else None
    write_audit(
        session,
        "human",
        "decision",
        case_id,
        {"decision": body.decision, "note": note, "agent_id": body.agent_id},
        {"status": case.status, "system_recommended": case.recommended_action},
    )
    return _to_out(case)


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
        risk_score=c.risk_score,
        risk_explanation=c.risk_explanation,
        recommended_action=c.recommended_action,
        escalation_reason=c.escalation_reason,
        human_decision=c.human_decision,
        summary=c.summary,
        trace_id=c.trace_id,
        created_at=c.created_at.isoformat() if c.created_at else None,
    )

"""The customer's answer to an analyst's request for information (TRZ-28).

The answer is PII-redacted before it is stored or audited, goes to the dossier and puts the case
back in the queue, with a new row marked updated and a new SLA of its priority. The customer
comes from the session; row level security keeps every other customer's case out of reach.
"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import AuditRecord, Case, Customer, InfoRequest, QueueItem
from app.core.errors import AppError
from app.core.time import utcnow
from app.domain.clock import SimulatedClock
from app.domain.pii import redact
from app.schemas.api import InfoReplyOut, InfoRequestOut


def reply(
    session: Session,
    customer_id: str,
    case_id: str,
    text: str,
    sla_hours: dict[str, float],
) -> InfoReplyOut:
    """Stores the customer's answer and hands the case back to a person.

    Args:
        session: Session bound to the customer of the JWT.
        customer_id: Customer of the session.
        case_id: The case the analyst asked about.
        text: The answer.
        sla_hours: Review time of the queue by priority, `queue.sla_hours` of the policy.

    Returns:
        The case back with a person. An answer sent again returns the stored one and changes
        nothing.

    Raises:
        AppError: 404 case_not_found for an unknown case or another customer's; 409
            no_open_request when nothing waits for an answer; 503 db_unavailable.
    """
    try:
        return _reply(session, customer_id, case_id, text, sla_hours)
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc


def _reply(
    session: Session, customer_id: str, case_id: str, text: str, sla_hours: dict[str, float]
) -> InfoReplyOut:
    case = session.get(Case, case_id)
    if case is None or case.customer_id != customer_id:
        raise AppError("case_not_found", f"Case {case_id} not found.", 404)
    request = session.execute(
        select(InfoRequest)
        .where(InfoRequest.case_id == case_id, InfoRequest.status == "open")
        .with_for_update()
    ).scalar_one_or_none()
    if request is None:
        if (stored := _replay(session, case_id)) is not None:
            return stored
        raise AppError("no_open_request", "Nothing on this case waits for an answer.", 409)
    customer = session.get(Customer, customer_id)
    redacted, pii_counts = redact(text.strip(), name=customer.first_name if customer else None)
    now = utcnow()
    request.answer, request.answered_at, request.status = redacted, now, "answered"
    case.status = request.status_before
    priority = (
        session.execute(
            select(QueueItem.priority)
            .where(QueueItem.case_id == case_id)
            .order_by(QueueItem.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        or "normal"
    )
    session.add(
        QueueItem(
            case_id=case_id,
            customer_id=customer_id,
            kind="escalation",
            reason=case.escalation_reason,
            priority=priority,
            sla_due_at=now + timedelta(hours=sla_hours[priority]),
            updated=True,
        )
    )
    out = InfoReplyOut(case_id=case_id, status=case.status, answered_at=now)
    write_audit(
        session,
        "customer",
        "info_reply",
        case_id,
        {"info_request_id": request.id, "redacted_text": redacted, "pii_counts": pii_counts},
        out.model_dump(mode="json"),
        idempotency_key=f"info_reply:{request.id}",
    )
    return out


def _replay(session: Session, case_id: str) -> InfoReplyOut | None:
    """The stored result of the latest answer on the case, when the case still has it."""
    last = session.execute(
        select(AuditRecord)
        .where(
            AuditRecord.case_id == case_id,
            AuditRecord.actor == "customer",
            AuditRecord.action == "info_reply",
        )
        .order_by(AuditRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return InfoReplyOut.model_validate(last.result) if last and last.result else None


def latest_request(session: Session, clock: SimulatedClock, case_id: str) -> InfoRequestOut | None:
    """The latest question of an analyst on a case, as the customer sees it.

    Args:
        session: Open session.
        clock: Simulated clock: the deadline is overdue when it is before its today.
        case_id: The case.

    Returns:
        The question with its deadline, or None when the case has none.
    """
    r = session.execute(
        select(InfoRequest)
        .where(InfoRequest.case_id == case_id)
        .order_by(InfoRequest.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if r is None:
        return None
    return InfoRequestOut(
        id=r.id,
        question=r.question,
        asked_on=r.asked_on,
        due_on=r.due_on,
        overdue=r.status == "open" and r.due_on < clock.today(),
        status=r.status,
        answered_at=r.answered_at,
    )

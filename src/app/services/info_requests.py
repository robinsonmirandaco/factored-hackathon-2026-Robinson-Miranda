"""The customer's answer to an analyst's request for information (TRZ-28).

The answer is PII-redacted before it is stored or audited, goes to the dossier and puts the case
back in the queue, with a new row marked updated and a new SLA of its priority. The customer
comes from the session; row level security keeps every other customer's case out of reach. A
request left unanswered past its deadline closes the case for lack of information, and the
customer is told (CA3).
"""

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import AuditRecord, Case, Customer, InfoRequest, QueueItem
from app.core.errors import AppError
from app.core.time import utcnow
from app.domain.clock import SimulatedClock
from app.domain.email import EmailConfig
from app.domain.pii import redact
from app.schemas.api import InfoReplyOut, InfoRequestOut
from app.services.notifications import notify

# Status of a case closed because the customer did not answer the analyst in time (CA3).
CLOSED_NO_INFO = "closed_no_info"


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


def is_overdue(due_on: date, as_of: date) -> bool:
    """Tells whether a request is past its deadline: the customer has the whole due day.

    Args:
        due_on: Last business day to answer.
        as_of: Day of the simulated clock the process runs for.

    Returns:
        True from the day after the due day.
    """
    return as_of > due_on


def expire_overdue(session: Session, as_of: date, email: EmailConfig | None = None) -> list[str]:
    """Closes for lack of information every case whose request is past its deadline (CA3).

    Each request is locked and skipped if another run holds it, so two runs at once never close
    a case twice. The request becomes expired, the case closed_no_info, the customer gets the
    in-app notification and one audit row says which simulated day decided it. A second run
    finds nothing open and changes nothing. It is not a decision of an analyst, so it is not a
    review of the autonomy cell.

    Args:
        session: Session with the analyst role, which sees every customer's request.
        as_of: Day of the simulated clock to compare deadlines with.
        email: Email settings of the notification (TRZ-33); None while the flag is off.

    Returns:
        The cases closed, in the order of their requests.
    """
    overdue = session.execute(
        select(InfoRequest)
        .where(InfoRequest.status == "open", InfoRequest.due_on < as_of)
        .order_by(InfoRequest.id)
        .with_for_update(skip_locked=True)
    ).scalars()
    closed = []
    for request in overdue:
        case = session.get(Case, request.case_id)
        if case is None:
            continue
        request.status = "expired"
        before, case.status = case.status, CLOSED_NO_INFO
        key = f"info_expired:{request.id}"
        write_audit(
            session,
            "system",
            "info_expired",
            case.id,
            {"info_request_id": request.id, "as_of": as_of.isoformat()},
            {
                "status_before": before,
                "status": CLOSED_NO_INFO,
                "due_on": request.due_on.isoformat(),
            },
            idempotency_key=key,
            customer_id=case.customer_id,
        )
        notify(session, case, "info_expired", key, due=request.due_on, email=email)
        closed.append(case.id)
    session.flush()
    return closed


def all_requests(session: Session, clock: SimulatedClock, case_id: str) -> list[InfoRequestOut]:
    """Every question of an analyst on a case with its answer, in the order they were asked.

    Args:
        session: Open session.
        clock: Simulated clock: an open deadline is overdue when it is before its today.
        case_id: The case.

    Returns:
        The questions, oldest first; empty when the case has none.
    """
    rows = session.execute(
        select(InfoRequest).where(InfoRequest.case_id == case_id).order_by(InfoRequest.id)
    ).scalars()
    return [_out(r, clock) for r in rows]


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
    return None if r is None else _out(r, clock)


def _out(r: InfoRequest, clock: SimulatedClock) -> InfoRequestOut:
    return InfoRequestOut(
        id=r.id,
        question=r.question,
        asked_on=r.asked_on,
        due_on=r.due_on,
        overdue=r.status == "open" and r.due_on < clock.today(),
        status=r.status,
        answered_at=r.answered_at,
        answer=r.answer,
    )

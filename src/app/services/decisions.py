"""Analyst decisions on a case in the queue: approve, reject or ask for information (TRZ-27).

Each decision closes the open queue row of the case and writes one audit row, keyed by that
row, with the analyst, the time and the reason; the case history tells it from that row. The
analyst comes from the session, never from the body.

Approving runs the recommended registration with its read-back, the same tool and key the agent
uses. It does not block the card: a block needs the customer's explicit confirmation (design
3.2), so a recommendation that includes one leaves the block not executed.

An audit sample (TRZ-29) is a case the system already resolved: the analyst confirms it, which
changes nothing for the customer, or reverses it with a reason of the closed list. A reversal
puts the clarification in review by an analyst and notifies the customer; the registered dispute
is not annulled, since no tool annuls one (design 3.2). Its audit row says it is a review of an
automatic action, whether it is a reversal, the reason and the intent x language cell, which is
what the Wilson rule of TRZ-30 counts. An approval or a rejection of a case the system handed
over with a registration recommended on an identified charge carries the same block: approving
agrees with the system, rejecting reverses it (design 6.7). A case with no charge identified,
such as one no charge matched, is not a review whatever the analyst decides.

Every decision the customer must hear about writes a notification (TRZ-32).
"""

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import AuditRecord, Case, Customer, InfoRequest, QueueItem
from app.core.errors import AppError
from app.core.time import utcnow
from app.domain.pii import redact
from app.domain.policy_passages import PolicyDeadline, Unsupported, policy_deadline
from app.schemas.api import DecisionOut, HumanDecisionIn
from app.services import tools as T
from app.services.agent import AgentDeps
from app.services.cases import (
    HANDOFF_STATUSES,
    REGISTERS,
    VERIFICATION_FAILED_REASON,
    approvable,
)
from app.services.notifications import notify
from app.services.verification import verify_dispute


def record_decision(
    session: Session, deps: AgentDeps, analyst: str, case_id: str, body: HumanDecisionIn
) -> DecisionOut:
    """Records an analyst decision and does what it says.

    Args:
        session: Open session with the analyst role.
        deps: Agent collaborators: policy, simulated clock, passages and calendars.
        analyst: User name of the analyst session.
        case_id: The case.
        body: The decision.

    Returns:
        What the decision did. The same decision sent again on the same queue row returns the
        stored result and changes nothing.

    Raises:
        AppError: 404 case_not_found; 409 case_not_escalated when the case has no open queue
            row; 409 decision_not_allowed for need_info on a security event; 409
            nothing_to_approve when there is no registration to run; 409 charge_already_disputed
            when the charge already has an opened dispute; 400 reason_required,
            reason_not_allowed or question_required; 503 db_unavailable.
    """
    try:
        return _record(session, deps, analyst, case_id, body)
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc


def _record(
    session: Session, deps: AgentDeps, analyst: str, case_id: str, body: HumanDecisionIn
) -> DecisionOut:
    case = session.get(Case, case_id)
    if case is None:
        raise AppError("case_not_found", f"Case {case_id} not found.", 404)
    row = session.execute(
        select(QueueItem)
        .where(QueueItem.case_id == case_id, QueueItem.resolved_at.is_(None))
        .with_for_update()
    ).scalar_one_or_none()
    if row is not None and row.kind == "audit_sample":
        return _audit(session, deps, analyst, case, row, body)
    if row is None or case.status not in HANDOFF_STATUSES:
        if (stored := _replay(session, case_id, body.decision)) is not None:
            return stored
        raise AppError("case_not_escalated", f"Case is {case.status}, not with a person.", 409)
    _validate(deps, case, body)
    # A security event is not a clarification of the customer: it is never told (TRZ-27 CA8).
    security = case.status == "security_blocked"
    result: dict[str, Any] = {"analyst": analyst, "system_recommended": case.recommended_action}
    # Only a registration recommended on an identified charge is a review: without a charge the
    # system would never have acted, so a rejection says nothing of its autonomy.
    if approvable(case) and body.decision in ("approve", "reject"):
        result["review"] = _review("recommended_action", case, body)
    question: str | None = None
    if body.decision == "approve":
        result |= _approve(session, deps, case)
    elif body.decision == "reject":
        case.status = "rejected"
    else:
        question = redact(str(body.question).strip())[0]
        result["due_on"] = _ask(session, deps, analyst, case, question).isoformat()
    row.resolved_at = utcnow()
    case.human_decision = body.decision
    result["status"] = case.status
    if case.status == "failed":
        # The approval's read-back did not match: the case is back with a person.
        _requeue(session, deps, case, VERIFICATION_FAILED_REASON, "normal")
    write_audit(
        session,
        "human",
        "decision",
        case_id,
        {
            "decision": body.decision,
            "reason": body.reason,
            "note": redact(body.note)[0] if body.note else None,
            # Redacted like the note; the history tells it next to the customer's answer.
            "question": question if body.decision == "need_info" else None,
            "queue_id": row.id,
        },
        result,
        idempotency_key=f"decision:{row.id}",
        customer_id=case.customer_id,
    )
    if not security:
        _notify(session, case, body, result, f"decision:{row.id}")
    return _out(case_id, body.decision, result)


def _notify(
    session: Session, case: Case, body: HumanDecisionIn, result: dict[str, Any], key: str
) -> None:
    """Tells the customer what was decided. An approval whose read-back failed is back with a
    person and is not told yet."""
    if case.status == "approved" and result.get("folio"):
        notify(session, case, "approved", key, folio=str(result["folio"]))
    elif case.status == "rejected":
        notify(session, case, "rejected", key, reason=body.reason)
    elif case.status == "awaiting_customer":
        notify(session, case, "info_requested", key, due=date.fromisoformat(result["due_on"]))


def _review(of: str, case: Case, body: HumanDecisionIn) -> dict[str, Any]:
    """What TRZ-30 counts: one review of what the system did or recommended, in its intent x
    language cell. A rejection is a reversal with its reason of the closed list; an approval
    agrees with the system."""
    reversal = body.decision == "reject"
    return {
        "of": of,
        "system_action": case.recommended_action,
        "reversal": reversal,
        "reason": body.reason if reversal else None,
        "cell": {"intent": case.intent, "language": case.language},
    }


def _audit(
    session: Session,
    deps: AgentDeps,
    analyst: str,
    case: Case,
    row: QueueItem,
    body: HumanDecisionIn,
) -> DecisionOut:
    """Confirms or reverses an audit sample (TRZ-29 CA5)."""
    if body.decision == "need_info":
        raise AppError(
            "decision_not_allowed", "An audit sample can only be confirmed or reversed.", 409
        )
    _validate(deps, case, body)
    reversal = body.decision == "reject"
    result: dict[str, Any] = {
        "analyst": analyst,
        "system_recommended": case.recommended_action,
        "review": _review("audit_sample", case, body),
        # The dispute stays registered: no tool annuls one.
        "dispute_annulled": False,
    }
    if reversal:
        case.status = "in_review"
        case.human_decision = body.decision
    row.resolved_at = utcnow()
    result["status"] = case.status
    write_audit(
        session,
        "human",
        "decision",
        case.id,
        {
            "decision": body.decision,
            "reason": body.reason,
            "note": redact(body.note)[0] if body.note else None,
            "queue_id": row.id,
            "kind": "audit_sample",
        },
        result,
        idempotency_key=f"decision:{row.id}",
        customer_id=case.customer_id,
    )
    if reversal:
        notify(session, case, "audit_reversed", f"decision:{row.id}")
    return _out(case.id, body.decision, result)


def _validate(deps: AgentDeps, case: Case, body: HumanDecisionIn) -> None:
    security = case.status == "security_blocked"
    # Asking the customer for more would hand a case stopped by security back to the chat.
    if security and body.decision == "need_info":
        raise AppError(
            "decision_not_allowed", "A case stopped by security can only be closed.", 409
        )
    if body.decision == "reject":
        allowed = deps.policy.config.autonomy.reversal_reasons
        if not body.reason:
            raise AppError("reason_required", "A rejection needs a reason of the list.", 400)
        if body.reason not in allowed:
            raise AppError(
                "reason_not_allowed", f"Reason must be one of {', '.join(allowed)}.", 400
            )
    if body.decision == "need_info" and not (body.question or "").strip():
        raise AppError("question_required", "A request for information needs a question.", 400)
    if body.decision == "approve" and not security and not approvable(case):
        raise AppError(
            "nothing_to_approve",
            "There is no registration to run: ask for information or reject.",
            409,
        )


def _approve(session: Session, deps: AgentDeps, case: Case) -> dict[str, Any]:
    """Runs the approved registration and reads it back; a security event is only closed."""
    if case.status == "security_blocked":
        case.status = "approved"
        return {}
    customer = _customer(session, case)
    transaction_id = str(case.transaction_id)
    language = case.language or "es"

    def deadline(start: Any) -> PolicyDeadline | Unsupported:
        return policy_deadline(
            dict(deps.passages),
            dict(deps.calendars),
            "response_deadline",
            start,
            customer.country_code,
            language,
        )

    dispute = T.register_dispute(
        session,
        deps.clock,
        deadline,
        case.customer_id,
        case.id,
        transaction_id,
        case.intent,
        reason="analyst approved",
    )
    if not dispute.ok:
        raise AppError(
            "charge_already_disputed",
            f"The charge already has the opened dispute {dispute.data.get('existing_folio')}.",
            409,
        )
    backed = deadline(deps.clock.now.date())
    check = verify_dispute(
        session,
        case.id,
        case.customer_id,
        transaction_id,
        case.intent,
        deps.clock.now,
        backed.due if isinstance(backed, PolicyDeadline) else None,
        str(dispute.data.get("folio", "")),
    )
    blocks = REGISTERS[str(case.recommended_action)]
    if not check.verified:
        case.status, case.escalation_reason = "failed", VERIFICATION_FAILED_REASON
        return {"verified": False, "block_not_executed": blocks}
    case.status = "approved"
    return {"verified": True, "folio": dispute.data["folio"], "block_not_executed": blocks}


def _ask(session: Session, deps: AgentDeps, analyst: str, case: Case, question: str) -> Any:
    """Stores the redacted question with its deadline in business days; the case waits for the
    customer."""
    customer = _customer(session, case)
    asked_on = deps.clock.today()
    due_on = deps.calendars[customer.country_code].add_business_days(
        asked_on, deps.policy.config.queue.info_request_business_days
    )
    session.add(
        InfoRequest(
            case_id=case.id,
            customer_id=case.customer_id,
            question=question,
            asked_by=analyst,
            asked_on=asked_on,
            due_on=due_on,
            status_before=case.status,
        )
    )
    case.status = "awaiting_customer"
    return due_on


def _customer(session: Session, case: Case) -> Customer:
    customer = session.get(Customer, case.customer_id)
    if customer is None:
        raise AppError("customer_not_found", f"Customer {case.customer_id} not found.", 404)
    return customer


def _requeue(session: Session, deps: AgentDeps, case: Case, reason: str, priority: str) -> None:
    session.add(
        QueueItem(
            case_id=case.id,
            customer_id=case.customer_id,
            kind="escalation",
            reason=reason,
            priority=priority,
            sla_due_at=utcnow() + timedelta(hours=deps.policy.config.queue.sla_hours[priority]),
        )
    )


def _replay(session: Session, case_id: str, decision: str) -> DecisionOut | None:
    """The stored result of the latest decision on the case, when it is the same decision."""
    last = session.execute(
        select(AuditRecord)
        .where(
            AuditRecord.case_id == case_id,
            AuditRecord.actor == "human",
            AuditRecord.action == "decision",
            AuditRecord.idempotency_key.is_not(None),
        )
        .order_by(AuditRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last is None or (last.payload or {}).get("decision") != decision:
        return None
    return _out(case_id, decision, last.result or {})


def _out(case_id: str, decision: str, result: dict[str, Any]) -> DecisionOut:
    return DecisionOut(
        case_id=case_id,
        decision=decision,
        status=result["status"],
        dispute_folio=result.get("folio"),
        block_not_executed=bool(result.get("block_not_executed")),
        due_on=result.get("due_on"),
    )

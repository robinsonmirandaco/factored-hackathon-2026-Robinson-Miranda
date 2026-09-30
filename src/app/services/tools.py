"""Tools the agent can call. Each one:
  - takes an open Session and typed arguments,
  - writes one audit row,
  - is idempotent when it changes state (keyed by action + case_id + target),
  - checks that every transaction and product id is the session customer's,
  - never exposes PII: no name, document or product number leaves these tools.

Each state-changing tool documents its contract (inputs, writes, output, replay, errors and
limits) in its docstring (TRZ-18 CA7).

Which tools may run is decided by the policy engine before the call, never here.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.adapters.db.audit import timed, write_audit
from app.adapters.db.models import (
    AuditRecord,
    CardBlock,
    Case,
    CaseAction,
    Customer,
    Dispute,
    Product,
    QueueItem,
    Transaction,
)
from app.core.time import utcnow
from app.domain.clock import SimulatedClock
from app.domain.policy_passages import PolicyDeadline, Unsupported

CARD_TYPES = ("credit_card", "debit_card")
HandoffStatus = Literal["escalated", "pending_analyst_approval", "security_blocked", "failed"]
# Complaint subcategories that are transaction disputes (design 2.1).
DISPUTE_SUBCATEGORIES = ("Cargo no reconocido", "Cobro indebido")

# Complaints are loaded by the seed, not mapped by the ORM. Resolution or closing, whichever
# comes first, ends a complaint, as the case generator reads it (TRZ-42).
_OPEN_COMPLAINTS = text(
    "SELECT complaint_id FROM complaints WHERE customer_id = :customer_id "
    "AND subcategory = ANY(:subcategories) AND creation_date BETWEEN :since AND :now "
    "AND status <> 'Rejected' "
    "AND (least(resolution_date, closing_date) IS NULL "
    "OR least(resolution_date, closing_date) > :now) "
    "ORDER BY complaint_id"
)
# Every complaint of the customer still open at the simulated now, whatever its category.
_OPEN_CLAIMS = text(
    "SELECT complaint_id, subcategory, creation_date, assignment_date, first_response_date "
    "FROM complaints WHERE customer_id = :customer_id AND creation_date <= :now "
    "AND status <> 'Rejected' "
    "AND (least(resolution_date, closing_date) IS NULL "
    "OR least(resolution_date, closing_date) > :now) "
    "ORDER BY creation_date DESC, complaint_id"
)


@dataclass
class ToolResult:
    """Outcome of a tool call.

    Attributes:
        ok: Whether the tool succeeded.
        data: Tool output, also stored in the audit log.
        message: Short explanation when not ok or when the call was a replay.
    """

    ok: bool
    data: dict[str, Any]
    message: str = ""


def _existing(session: Session, key: str) -> AuditRecord | None:
    return session.execute(
        select(AuditRecord).where(AuditRecord.idempotency_key == key)
    ).scalar_one_or_none()


# ---- read tools (class 0) ---------------------------------------------------------------


def get_customer_profile(
    session: Session,
    clock: SimulatedClock,
    customer_id: str,
    lookback_days: int,
    case_id: str | None = None,
) -> ToolResult:
    """Reads the segment, country and status of a customer, and whether a dispute is open.

    A dispute is open when a dispute complaint of the dataset was created in the `lookback_days`
    before the simulated now, was not rejected and was not resolved or closed by then, or when
    a dispute registered by TRAZO in that look-back is still opened (design 8,
    customer_has_open_dispute_last_90d).

    Args:
        session: Open database session.
        clock: Simulated clock the look-back ends at.
        customer_id: Customer to read.
        lookback_days: `open_dispute_lookback_days` of the policy.
        case_id: Case for the audit row.

    Returns:
        The profile; ok is False when the customer does not exist.
    """
    with timed() as t:
        c = session.get(Customer, customer_id)
        if not c:
            res = ToolResult(False, {}, f"customer {customer_id} not found")
        else:
            complaints = list(
                session.execute(
                    _OPEN_COMPLAINTS,
                    {
                        "customer_id": customer_id,
                        "subcategories": list(DISPUTE_SUBCATEGORIES),
                        "since": clock.days_ago(lookback_days),
                        "now": clock.now,
                    },
                ).scalars()
            )
            # Own disputes count on their business date, the same clock and window as the
            # complaints; one registered before business dates existed has none and is left out.
            disputes = list(
                session.execute(
                    select(Dispute.folio)
                    .where(
                        Dispute.customer_id == customer_id,
                        Dispute.status == "opened",
                        Dispute.business_at.between(clock.days_ago(lookback_days), clock.now),
                    )
                    .order_by(Dispute.folio)
                ).scalars()
            )
            res = ToolResult(
                True,
                {
                    "customer_id": c.customer_id,
                    "segment": c.segment,
                    "country_code": c.country_code,
                    "customer_status": c.customer_status,
                    "open_dispute_complaints": len(complaints),
                    "open_disputes": len(disputes),
                    "open_dispute_last_90d": bool(complaints or disputes),
                    # The records behind the rule, the sources the analyst's dossier cites.
                    "open_dispute_ids": {"complaints": complaints, "disputes": disputes},
                },
            )
    write_audit(
        session,
        "tool",
        "get_customer_profile",
        case_id,
        {"customer_id": customer_id, "lookback_days": lookback_days},
        res.data,
        t["ms"],
    )
    return res


def list_recent_transactions(
    session: Session, customer_id: str, limit: int = 10, case_id: str | None = None
) -> ToolResult:
    """Lists the latest transactions of a customer.

    Args:
        session: Open database session.
        customer_id: Customer to read.
        limit: Maximum rows.
        case_id: Case for the audit row.

    Returns:
        The transactions, newest first.
    """
    with timed() as t:
        rows = (
            session.execute(
                select(Transaction)
                .where(Transaction.customer_id == customer_id)
                .order_by(Transaction.transaction_date.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        data = {"transactions": [_tx_dict(r) for r in rows]}
    write_audit(
        session,
        "tool",
        "list_recent_transactions",
        case_id,
        {"customer_id": customer_id, "limit": limit},
        {"count": len(rows)},
        t["ms"],
    )
    return ToolResult(True, data)


def get_open_claims(
    session: Session,
    clock: SimulatedClock,
    deadline: Callable[[date], PolicyDeadline | Unsupported],
    customer_id: str,
    case_id: str | None = None,
) -> ToolResult:
    """Reads the open claims of the session customer, with status, last step and deadline.

    A claim is a dispute registered by TRAZO that is still opened, or a complaint of the dataset
    created before the simulated now, not rejected, and not resolved or closed by then (design
    3.1). Status and last step come from the dates known at the simulated now, not from the
    dataset's final status, which may lie after it.

    The deadline of a dispute is the one registered with it. The deadline of a dispute complaint
    is counted from its creation date by the response deadline passage. Any other complaint has
    no passage that backs a deadline, and none is given (TRZ-22 CA6).

    Args:
        session: Open session bound to the customer of the JWT.
        clock: Simulated clock of the case.
        deadline: Response deadline counted from a business date, with its passage, or
            Unsupported when no passage backs it.
        customer_id: Customer of the session.
        case_id: Case for the audit row.

    Returns:
        The claims of `read_open_claims`.
    """
    with timed() as t:
        claims = read_open_claims(session, clock, deadline, customer_id)
    write_audit(
        session,
        "tool",
        "get_open_claims",
        case_id,
        {"customer_id": customer_id},
        {"count": len(claims), "claims": claims},
        t["ms"],
    )
    return ToolResult(True, {"claims": claims})


def read_open_claims(
    session: Session,
    clock: SimulatedClock,
    deadline: Callable[[date], PolicyDeadline | Unsupported],
    customer_id: str,
) -> list[dict[str, Any]]:
    """The open claims of `get_open_claims`, read without an audit row.

    Mis aclaraciones reads them on every visit; a page view is not a step of a case.

    Args:
        session: Open session bound to the customer of the JWT.
        clock: Simulated clock.
        deadline: Response deadline counted from a business date, with its passage, or
            Unsupported when no passage backs it.
        customer_id: Customer of the session.

    Returns:
        The claims, newest first, each with claim_id, source (table), opened_on, status,
        last_step, and due_date with passage_id, overdue, or due_unsupported.
    """
    today = clock.now.date()
    claims: list[dict[str, Any]] = []
    disputes = session.execute(
        select(Dispute)
        .where(Dispute.customer_id == customer_id, Dispute.status == "opened")
        .order_by(Dispute.business_at.desc(), Dispute.id)
    ).scalars()
    for d in disputes:
        if d.business_at is None or d.folio is None or d.business_at > clock.now:
            continue
        opened = d.business_at.date()
        # register_dispute sets due_date only from the response deadline passage.
        backed = deadline(opened)
        due = d.due_date if isinstance(backed, PolicyDeadline) else None
        claims.append(
            _claim(d.folio, "disputes", opened, "received", ("registered", opened))
            | _due(due, backed)
        )
    rows = session.execute(_OPEN_CLAIMS, {"customer_id": customer_id, "now": clock.now}).mappings()
    for r in rows:
        opened = r["creation_date"].date()
        steps = [("created", r["creation_date"])]
        steps += [
            (name, r[column])
            for name, column in (
                ("assigned", "assignment_date"),
                ("first_response", "first_response_date"),
            )
            if r[column] is not None and r[column] <= clock.now
        ]
        step, at = max(steps, key=lambda s: s[1])
        status = {"created": "received", "assigned": "in_review"}.get(step, "answered")
        backed = (
            deadline(opened)
            if r["subcategory"] in DISPUTE_SUBCATEGORIES
            else Unsupported("no passage backs a deadline for this kind of complaint")
        )
        due = backed.due if isinstance(backed, PolicyDeadline) else None
        claims.append(
            _claim(r["complaint_id"], "complaints", opened, status, (step, at.date()))
            | _due(due, backed)
        )
    claims.sort(key=lambda c: (c["opened_on"], c["claim_id"]), reverse=True)
    for c in claims:
        c["overdue"] = c["due_date"] is not None and c["due_date"] < today.isoformat()
    return claims


def _claim(
    claim_id: str,
    source: str,
    opened: date,
    status: str,
    last_step: tuple[str, date],
) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "source": source,
        "opened_on": opened.isoformat(),
        "status": status,
        "last_step": {"step": last_step[0], "on": last_step[1].isoformat()},
    }


def _due(due: date | None, backed: PolicyDeadline | Unsupported) -> dict[str, Any]:
    if due is None or not isinstance(backed, PolicyDeadline):
        reason = backed.reason if isinstance(backed, Unsupported) else "no deadline registered"
        return {"due_date": None, "passage_id": None, "due_unsupported": reason}
    return {"due_date": due.isoformat(), "passage_id": backed.passage_id, "due_unsupported": None}


# ---- state-changing tools (class 1) -----------------------------------------------------


class OwnershipError(Exception):
    """A transaction or product id that is not the session customer's.

    Under row level security another customer's row is invisible, so "not yours" and "does not
    exist" look the same, and both raise this. The caller stops the case for security and says
    nothing about whether the id exists (design 11.4).
    """


def register_dispute(
    session: Session,
    clock: SimulatedClock,
    deadline: Callable[[date], PolicyDeadline | Unsupported],
    customer_id: str,
    case_id: str,
    transaction_id: str,
    dispute_type: str,
) -> ToolResult:
    """Registers a dispute on a transaction of the session customer (TRZ-18 CA1).

    Contract:
        Inputs: the session customer, the case, the disputed transaction and the dispute type
            (the case intent). The business date is the simulated now of `clock`.
        Writes: one row of disputes with folio DSP-AAAA-NNNNN (AAAA the year of the business
            date, NNNNN from dispute_folio_seq), transaction, type, amount, currency,
            business_at and due_date; one audit row keyed `dispute:{case_id}:{transaction_id}`.
            The transaction, its product and every balance are left untouched (CA6).
        Output: folio, transaction_id, dispute_type, amount, currency, business_at, due_date
            and dispute_status "opened".
        Replay: the same key returns the stored output, the same folio, and writes nothing
            (CA4).
        Errors: OwnershipError when the transaction is not the customer's (CA5); the database
            error of the sequence when folios pass 99999.
        Limits: due_date is null when no policy passage backs a deadline, with the reason in
            the output; it is never guessed.

    Args:
        session: Open session bound to the customer of the JWT.
        clock: Simulated clock of the case: the business date of the dispute.
        deadline: Response deadline counted from a business date, with its passage, or
            Unsupported when no passage backs it.
        customer_id: Customer of the session.
        case_id: Case that owns the action.
        transaction_id: Disputed transaction.
        dispute_type: Kind of dispute, the case intent.

    Returns:
        The dispute, or the stored result on a replay.

    Raises:
        OwnershipError: If the transaction is not a transaction of the customer.
    """
    key = f"dispute:{case_id}:{transaction_id}"
    if prev := _existing(session, key):
        return ToolResult(True, prev.result or {}, "already applied (idempotent)")
    with timed() as t:
        tx = _owned_transaction(session, customer_id, transaction_id)
        business_at = clock.now
        number = session.execute(text("SELECT nextval('dispute_folio_seq')")).scalar_one()
        folio = f"DSP-{business_at.year:04d}-{number:05d}"
        backed = deadline(business_at.date())
        due = backed.due if isinstance(backed, PolicyDeadline) else None
        session.add(
            Dispute(
                folio=folio,
                customer_id=customer_id,
                case_id=case_id,
                transaction_id=transaction_id,
                dispute_type=dispute_type,
                reason="customer confirmed",
                amount=tx.amount,
                currency=tx.currency,
                business_at=business_at,
                due_date=due,
            )
        )
        res = ToolResult(
            True,
            {
                "folio": folio,
                "transaction_id": transaction_id,
                "dispute_type": dispute_type,
                "amount": tx.amount,
                "currency": tx.currency,
                "business_at": business_at.isoformat(),
                "due_date": due.isoformat() if due else None,
                "due_date_passage": backed.passage_id
                if isinstance(backed, PolicyDeadline)
                else None,
                "due_date_unsupported": backed.reason if isinstance(backed, Unsupported) else None,
                "dispute_status": "opened",
            },
        )
    write_audit(
        session,
        "tool",
        "register_dispute",
        case_id,
        {"transaction_id": transaction_id, "dispute_type": dispute_type},
        res.data,
        t["ms"],
        idempotency_key=key,
    )
    return res


def block_card(
    session: Session, customer_id: str, case_id: str, transaction_id: str, reason: str
) -> ToolResult:
    """Blocks the card the disputed charge was made with, and only that one (TRZ-18 CA2).

    Contract:
        Inputs: the session customer, the case, the disputed transaction and a short reason.
        Writes: product_status of the charge's product to Blocked and one row of card_blocks
            with the status before; one audit row keyed `card_block:{case_id}:{product_id}`.
            No other product and no balance changes (CA6).
        Output: product_id, status_before and status_after "Blocked".
        Replay: the same key returns the stored output and writes nothing (CA4).
        Not ok, nothing written: the product is not a credit or debit card (reason
            not_a_card), or the card is not Active (reason card_not_active).
        Errors: OwnershipError when the transaction or its product is not the customer's (CA5).

    Args:
        session: Open session bound to the customer of the JWT.
        customer_id: Customer of the session.
        case_id: Case that owns the action.
        transaction_id: Disputed transaction; its product is the card blocked.
        reason: Why the card is blocked, such as the case intent.

    Returns:
        The block, the stored result on a replay, or ok False with the reason.

    Raises:
        OwnershipError: If the transaction or its product is not the customer's.
    """
    tx = _owned_transaction(session, customer_id, transaction_id)
    key = f"card_block:{case_id}:{tx.product_id}"
    if prev := _existing(session, key):
        return ToolResult(True, prev.result or {}, "already applied (idempotent)")
    with timed() as t:
        product = session.get(Product, tx.product_id)
        if product is None or product.customer_id != customer_id:
            raise OwnershipError("product not found for the session customer")
        if product.product_type not in CARD_TYPES:
            res = ToolResult(False, {"product_id": product.product_id}, "not_a_card")
        elif product.product_status != "Active":
            res = ToolResult(False, {"product_id": product.product_id}, "card_not_active")
        else:
            session.add(
                CardBlock(
                    customer_id=customer_id,
                    product_id=product.product_id,
                    case_id=case_id,
                    reason=reason[:200],
                    status_before=product.product_status,
                )
            )
            res = ToolResult(
                True,
                {
                    "product_id": product.product_id,
                    "status_before": product.product_status,
                    "status_after": "Blocked",
                },
            )
            product.product_status = "Blocked"
    write_audit(
        session,
        "tool",
        "block_card",
        case_id,
        {"transaction_id": transaction_id, "reason": reason[:200]},
        res.data if res.ok else {**res.data, "message": res.message},
        t["ms"],
        idempotency_key=key if res.ok else None,
    )
    return res


def _owned_transaction(session: Session, customer_id: str, transaction_id: str) -> Transaction:
    tx = session.get(Transaction, transaction_id)
    if tx is None or tx.customer_id != customer_id:
        raise OwnershipError("transaction not found for the session customer")
    return tx


def escalate_to_human(
    session: Session,
    case_id: str,
    reason: str,
    sla_hours: float,
    recommended_action: str | None = None,
    status: HandoffStatus = "escalated",
    priority: str = "normal",
) -> ToolResult:
    """Puts a case in the human queue with the reason, the recommended action, its priority and
    the end of its SLA (TRZ-25 CA5).

    Contract:
        Writes: the case status and reason; one row of case_queue (kind escalation) with the
            priority and sla_due_at, the real clock now plus `sla_hours`; one audit row keyed
            `{case_id}:escalate_to_human`.
        Replay: the same key returns the stored output and writes nothing, so a case is queued
            once however often it is handed over.

    Args:
        session: Open database session.
        case_id: Case to escalate; also the idempotency key.
        reason: Why it was escalated, truncated to 256 characters.
        sla_hours: Hours until the SLA of the priority ends (policy `queue.sla_hours`).
        recommended_action: What the system suggests the operator do.
        status: escalated, pending_analyst_approval (the analyst approves a prepared action),
            security_blocked, or failed (an action whose read-back did not match).
        priority: normal, high or urgent, as the policy decided.

    Returns:
        The escalation, or the stored result on a replay.
    """
    key = f"{case_id}:escalate_to_human"
    if prev := _existing(session, key):
        return ToolResult(True, prev.result or {}, "already escalated (idempotent)")
    with timed() as t:
        case = session.get(Case, case_id)
        if not case:
            return ToolResult(False, {}, "case not found")
        case.status = status
        case.escalation_reason = reason[:256]
        if recommended_action:
            case.recommended_action = recommended_action
        due = utcnow() + timedelta(hours=sla_hours)
        session.add(
            QueueItem(
                case_id=case_id,
                customer_id=case.customer_id,
                kind="escalation",
                reason=reason[:256],
                priority=priority,
                sla_due_at=due,
            )
        )
        res = ToolResult(
            True,
            {
                "case_id": case_id,
                "status": status,
                "reason": reason[:256],
                "priority": priority,
                "sla_due_at": due.isoformat(),
            },
        )
    write_audit(
        session,
        "tool",
        "escalate_to_human",
        case_id,
        {"reason": reason[:256], "priority": priority},
        res.data,
        t["ms"],
        idempotency_key=key,
    )
    return res


# ---- pending actions ----------------------------------------------------------------------


def offer_action(session: Session, case: Case, action: str) -> CaseAction:
    """Stores an action that waits for the customer's confirmation, replacing any earlier one.

    Args:
        session: Open session bound to the case customer.
        case: The case, with its identified transaction.
        action: register, register_and_offer_block or register_and_block.

    Returns:
        The pending action; its id is what a confirmation must name.
    """
    settle_pending_action(session, case.id, "replaced")
    row = CaseAction(
        id=f"ACT-{uuid.uuid4().hex[:10].upper()}",
        case_id=case.id,
        customer_id=case.customer_id,
        action=action,
        transaction_id=case.transaction_id,
        status="pending",
    )
    session.add(row)
    session.flush()
    return row


def settle_pending_action(
    session: Session, case_id: str, status: Literal["replaced", "executed", "canceled"]
) -> str | None:
    """Ends the pending action of a case, if there is one, so no later "sí" can run it.

    Args:
        session: Open session bound to the case customer.
        case_id: The case.
        status: replaced by a newer action, or canceled (security stop, expired session, a new
            message that offered nothing).

    Returns:
        The id of the action ended, or None when nothing was pending.
    """
    row = session.execute(
        select(CaseAction).where(CaseAction.case_id == case_id, CaseAction.status == "pending")
    ).scalar_one_or_none()
    if row is None:
        return None
    row.status, row.resolved_at = status, utcnow()
    # The one-pending-per-case index needs this update in before a new pending row goes in.
    session.flush()
    return row.id


# ---- helpers ----------------------------------------------------------------------------


def _tx_dict(r: Transaction) -> dict[str, Any]:
    return {
        "tx_id": r.transaction_id,
        "amount": r.amount,
        "currency": r.currency,
        "merchant": r.merchant_name,
        "merchant_category": r.merchant_category,
        "country": r.transaction_country,
        "channel": r.channel,
        "timestamp": r.transaction_date.isoformat(),
        "status": r.transaction_status,
    }

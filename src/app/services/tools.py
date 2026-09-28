"""Tools the agent can call. Each one:
  - takes an open Session and typed arguments,
  - writes one audit row,
  - is idempotent when it changes state (keyed by case_id + action + target),
  - never exposes PII: no name, document or product number leaves these tools.

Which tools may run is decided by the policy engine before the call, never here.
"""

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.adapters.db.audit import timed, write_audit
from app.adapters.db.models import (
    AuditRecord,
    CardBlock,
    Case,
    Customer,
    Dispute,
    Product,
    Transaction,
)
from app.domain.clock import SimulatedClock

CARD_TYPES = ("credit_card", "debit_card")
HandoffStatus = Literal["escalated", "pending_analyst_approval", "security_blocked"]
# Complaint subcategories that are transaction disputes (design 2.1).
DISPUTE_SUBCATEGORIES = ("Cargo no reconocido", "Cobro indebido")

# Complaints are loaded by the seed, not mapped by the ORM. Resolution or closing, whichever
# comes first, ends a complaint, as the case generator reads it (TRZ-42).
_OPEN_COMPLAINTS = text(
    "SELECT count(*) FROM complaints WHERE customer_id = :customer_id "
    "AND subcategory = ANY(:subcategories) AND creation_date BETWEEN :since AND :now "
    "AND status <> 'Rejected' "
    "AND (least(resolution_date, closing_date) IS NULL "
    "OR least(resolution_date, closing_date) > :now)"
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
    a dispute registered by TRAZO is still opened (design 8, customer_has_open_dispute_last_90d).

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
            complaints = session.execute(
                _OPEN_COMPLAINTS,
                {
                    "customer_id": customer_id,
                    "subcategories": list(DISPUTE_SUBCATEGORIES),
                    "since": clock.days_ago(lookback_days),
                    "now": clock.now,
                },
            ).scalar_one()
            # Own disputes carry the real clock of the database, not the simulated one, so the
            # look-back cannot be applied to them: every one still opened counts.
            disputes = session.execute(
                select(func.count(Dispute.id)).where(
                    Dispute.customer_id == customer_id, Dispute.status == "opened"
                )
            ).scalar_one()
            res = ToolResult(
                True,
                {
                    "customer_id": c.customer_id,
                    "segment": c.segment,
                    "country_code": c.country_code,
                    "customer_status": c.customer_status,
                    "open_dispute_complaints": int(complaints),
                    "open_disputes": int(disputes),
                    "open_dispute_last_90d": complaints + disputes > 0,
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


# ---- state-changing tools (class 1) -----------------------------------------------------


def freeze_card(session: Session, customer_id: str, case_id: str, reason: str) -> ToolResult:
    """Blocks every active card of the customer and records each block in card_blocks.

    Every active card is blocked because the customer has not said which card yet; choosing
    the card and the folio come with TRZ-18.

    Args:
        session: Open database session.
        customer_id: Card owner.
        case_id: Case that owns the action; part of the idempotency key.
        reason: Why the cards are blocked, such as the case intent.

    Returns:
        The blocked products and their status before, or the stored result on a replay; ok
        is False when the customer has no active card.
    """
    key = f"{case_id}:freeze_card:{customer_id}"
    if prev := _existing(session, key):
        return ToolResult(True, prev.result or {}, "already applied (idempotent)")
    with timed() as t:
        cards = (
            session.execute(
                select(Product)
                .where(
                    Product.customer_id == customer_id,
                    Product.product_type.in_(CARD_TYPES),
                    Product.product_status == "Active",
                )
                .order_by(Product.product_id)
            )
            .scalars()
            .all()
        )
        if not cards:
            return ToolResult(False, {}, "no active card")
        for p in cards:
            session.add(
                CardBlock(
                    customer_id=customer_id,
                    product_id=p.product_id,
                    case_id=case_id,
                    reason=reason[:200],
                    status_before=p.product_status,
                )
            )
            p.product_status = "Blocked"
        res = ToolResult(
            True,
            {
                "customer_id": customer_id,
                "blocked_products": [p.product_id for p in cards],
                "status_after": "Blocked",
            },
        )
    write_audit(
        session,
        "tool",
        "freeze_card",
        case_id,
        {"customer_id": customer_id, "reason": reason[:200]},
        res.data,
        t["ms"],
        idempotency_key=key,
    )
    return res


def open_dispute(
    session: Session, tx_id: str, case_id: str, dispute_type: str, reason: str
) -> ToolResult:
    """Registers a dispute on a transaction as a row of disputes; the transaction is untouched.

    The folio and the deadline come with TRZ-18.

    Args:
        session: Open database session.
        tx_id: Disputed transaction.
        case_id: Case that owns the action; part of the idempotency key.
        dispute_type: Kind of dispute, such as the case intent.
        reason: Why the dispute was opened, truncated to 200 characters.

    Returns:
        The dispute, or the stored result on a replay.
    """
    key = f"{case_id}:open_dispute:{tx_id}"
    if prev := _existing(session, key):
        return ToolResult(True, prev.result or {}, "already applied (idempotent)")
    with timed() as t:
        tx = session.get(Transaction, tx_id)
        if not tx:
            return ToolResult(False, {}, "transaction not found")
        session.add(
            Dispute(
                customer_id=tx.customer_id,
                case_id=case_id,
                transaction_id=tx_id,
                dispute_type=dispute_type,
                reason=reason[:200],
                amount=tx.amount,
                currency=tx.currency,
            )
        )
        res = ToolResult(True, {"tx_id": tx_id, "dispute_status": "opened", "reason": reason[:200]})
    write_audit(
        session,
        "tool",
        "open_dispute",
        case_id,
        {"tx_id": tx_id, "reason": reason[:200]},
        res.data,
        t["ms"],
        idempotency_key=key,
    )
    return res


def escalate_to_human(
    session: Session,
    case_id: str,
    reason: str,
    recommended_action: str | None = None,
    status: HandoffStatus = "escalated",
) -> ToolResult:
    """Puts a case in the human queue with the reason and the recommended action.

    Args:
        session: Open database session.
        case_id: Case to escalate; also the idempotency key.
        reason: Why it was escalated, truncated to 256 characters.
        recommended_action: What the system suggests the operator do.
        status: escalated, pending_analyst_approval (the analyst approves a prepared action)
            or security_blocked.

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
        res = ToolResult(True, {"case_id": case_id, "status": status, "reason": reason[:256]})
    write_audit(
        session,
        "tool",
        "escalate_to_human",
        case_id,
        {"reason": reason[:256]},
        res.data,
        t["ms"],
        idempotency_key=key,
    )
    return res


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

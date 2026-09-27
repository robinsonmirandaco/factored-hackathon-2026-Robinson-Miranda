"""Tools the agent can call. Each one:
  - takes an open Session and typed arguments,
  - writes one audit row,
  - is idempotent when it changes state (keyed by case_id + action + target),
  - never exposes PII: no name, document or product number leaves these tools.

Which tools may run is decided by the policy engine before the call, never here.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
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
from app.core.time import utcnow
from app.domain.clock import SimulatedClock

CARD_TYPES = ("credit_card", "debit_card")


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
    session: Session, customer_id: str, case_id: str | None = None
) -> ToolResult:
    """Reads the segment, status and recent dispute count of a customer.

    Args:
        session: Open database session.
        customer_id: Customer to read.
        case_id: Case for the audit row.

    Returns:
        The profile; ok is False when the customer does not exist.
    """
    with timed() as t:
        c = session.get(Customer, customer_id)
        if not c:
            res = ToolResult(False, {}, f"customer {customer_id} not found")
        else:
            since = utcnow() - timedelta(days=30)
            disputes = session.execute(
                select(func.count(Case.id)).where(
                    Case.customer_id == customer_id,
                    Case.intent.in_(["unrecognized_charge", "duplicate_charge"]),
                    Case.created_at >= since,
                )
            ).scalar_one()
            res = ToolResult(
                True,
                {
                    "customer_id": c.customer_id,
                    "segment": c.segment,
                    "country_code": c.country_code,
                    "customer_status": c.customer_status,
                    "disputes_last_30d": int(disputes),
                },
            )
    write_audit(
        session,
        "tool",
        "get_customer_profile",
        case_id,
        {"customer_id": customer_id},
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


def lookup_transaction(
    session: Session,
    clock: SimulatedClock,
    customer_id: str,
    amount: float | None = None,
    merchant: str | None = None,
    days: int = 14,
    case_id: str | None = None,
) -> ToolResult:
    """Finds the transaction the customer is talking about. Fuzzy on amount (+-2%) and merchant.

    The search covers the `days` before the simulated `now`, never after it.

    Args:
        session: Open database session.
        clock: Simulated clock the search window ends at.
        customer_id: Owner of the transaction.
        amount: Amount mentioned by the customer, if any.
        merchant: Merchant mentioned by the customer, if any.
        days: How far back to search.
        case_id: Case for the audit row.

    Returns:
        Up to five matches, newest first; ok is False when there are none.
    """
    with timed() as t:
        # The upper bound matters when an evaluation case sets its own "now": later
        # transactions had not happened yet when the customer wrote.
        q = select(Transaction).where(
            Transaction.customer_id == customer_id,
            Transaction.transaction_date.between(clock.days_ago(days), clock.now),
        )
        if amount is not None:
            q = q.where(Transaction.amount.between(amount * 0.98, amount * 1.02))
        if merchant:
            q = q.where(func.lower(Transaction.merchant_name).like(f"%{merchant.lower()}%"))
        rows = (
            session.execute(q.order_by(Transaction.transaction_date.desc()).limit(5))
            .scalars()
            .all()
        )
        data = {"matches": [_tx_dict(r) for r in rows], "count": len(rows)}
        res = ToolResult(len(rows) > 0, data, "" if rows else "no matching transaction")
    write_audit(
        session,
        "tool",
        "lookup_transaction",
        case_id,
        {"customer_id": customer_id, "amount": amount, "merchant": merchant},
        {"count": len(rows)},
        t["ms"],
    )
    return res


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
    session: Session, case_id: str, reason: str, recommended_action: str | None = None
) -> ToolResult:
    """Puts a case in the human queue with the reason and the recommended action.

    Args:
        session: Open database session.
        case_id: Case to escalate; also the idempotency key.
        reason: Why it was escalated, truncated to 256 characters.
        recommended_action: What the system suggests the operator do.

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
        case.status = "escalated"
        case.escalation_reason = reason[:256]
        if recommended_action:
            case.recommended_action = recommended_action
        res = ToolResult(True, {"case_id": case_id, "status": "escalated", "reason": reason[:256]})
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

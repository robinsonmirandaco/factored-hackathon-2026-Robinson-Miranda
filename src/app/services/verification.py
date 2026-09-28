"""Read-back after acting (TRZ-19, design 5 and 11.5).

A tool's own answer is not proof that it wrote. After each action the database is read again,
bypassing the objects the session already holds, and compared with the state the action should
have left. What is expected comes from the inputs of the action, never from the tool's output.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import CardBlock, Dispute, Product, Transaction

# A dispute whose read-back failed keeps this status: it is not a valid dispute and the
# open-dispute rule, which counts only opened ones, leaves it out.
VERIFICATION_FAILED = "verification_failed"


@dataclass(frozen=True)
class Verification:
    """Result of reading one action back.

    Attributes:
        action: register_dispute or block_card.
        verified: True when the database holds exactly what the action should have written.
        mismatches: Fields that differ, or "missing" when the row is not there.
    """

    action: str
    verified: bool
    mismatches: tuple[str, ...]


def mismatches(expected: dict[str, Any], found: dict[str, Any] | None) -> tuple[str, ...]:
    """Names the expected fields whose value was not found.

    Args:
        expected: Field values the action should have left.
        found: Field values read back, or None when there is no row.

    Returns:
        The differing field names in the order of `expected`, or ("missing",).
    """
    if found is None:
        return ("missing",)
    return tuple(k for k, v in expected.items() if found.get(k) != v)


def verify_dispute(
    session: Session,
    case_id: str,
    customer_id: str,
    transaction_id: str,
    dispute_type: str,
    business_at: datetime,
    due_date: date | None,
    folio: str,
) -> Verification:
    """Reads a registered dispute back and compares it with what was asked for.

    A dispute that is there but differs is marked `verification_failed`; one that is missing
    leaves nothing to mark.

    Args:
        session: Open session bound to the customer of the JWT.
        case_id: Case that owns the action.
        customer_id: Customer of the session.
        transaction_id: Disputed transaction.
        dispute_type: The case intent.
        business_at: Simulated now of the case.
        due_date: Deadline the backing passage sets from that date, or None without backing.
        folio: Folio the tool reported.

    Returns:
        The verification, also written to the audit log.
    """
    tx = session.get(Transaction, transaction_id)
    expected = {
        "customer_id": customer_id,
        "case_id": case_id,
        "transaction_id": transaction_id,
        "dispute_type": dispute_type,
        "amount": _money(tx.amount) if tx else None,
        "currency": tx.currency if tx else None,
        "status": "opened",
        "business_at": business_at,
        "due_date": due_date,
    }
    row = _fresh(session, select(Dispute).where(Dispute.folio == folio))
    found = None
    if row is not None:
        found = {k: getattr(row, k) for k in expected}
        found["amount"] = _money(row.amount)
    differ = mismatches(expected, found)
    result = Verification("register_dispute", not differ, differ)
    if row is not None and not result.verified:
        session.execute(
            update(Dispute).where(Dispute.id == row.id).values(status=VERIFICATION_FAILED)
        )
    _audit(session, case_id, {"action": result.action, "folio": folio}, result)
    return result


def verify_block(
    session: Session, case_id: str, product_id: str, status_before: str
) -> Verification:
    """Reads a card block back: the card is blocked and the block row keeps its prior status.

    Args:
        session: Open session bound to the customer of the JWT.
        case_id: Case that owns the action.
        product_id: Card the tool reported as blocked.
        status_before: Status the tool reported the card had.

    Returns:
        The verification, also written to the audit log.
    """
    product = _fresh(session, select(Product).where(Product.product_id == product_id))
    block = _fresh(
        session,
        select(CardBlock).where(CardBlock.case_id == case_id, CardBlock.product_id == product_id),
    )
    expected = {"product_status": "Blocked", "status_before": status_before}
    found = None
    if product is not None and block is not None:
        found = {"product_status": product.product_status, "status_before": block.status_before}
    differ = mismatches(expected, found)
    result = Verification("block_card", not differ, differ)
    _audit(session, case_id, {"action": result.action, "product_id": product_id}, result)
    return result


def _fresh(session: Session, query: Any) -> Any:
    # populate_existing makes the ORM take the row from the database, not the object it holds.
    return session.execute(query.execution_options(populate_existing=True)).scalar_one_or_none()


def _money(value: Any) -> float | None:
    return None if value is None else round(float(value), 2)


def _audit(session: Session, case_id: str, payload: dict[str, Any], result: Verification) -> None:
    write_audit(
        session,
        "agent",
        "verify_action",
        case_id,
        payload,
        {"verified": result.verified, "mismatches": list(result.mismatches)},
        verified=result.verified,
    )

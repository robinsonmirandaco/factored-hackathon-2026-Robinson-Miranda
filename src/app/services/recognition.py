"""Reads the detail of an identified charge for the recognition step (TRZ-16, design 6.4).

Everything shown comes from the database in this call, under the row level security of the
session customer: never from the customer's message nor from the LLM.
"""

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit import timed, write_audit
from app.adapters.db.models import Product, Transaction
from app.adapters.db.rates import rates_near
from app.domain.clock import SimulatedClock
from app.domain.fx import display_amount
from app.domain.identification import DISPUTABLE_STATUSES, find_twin
from app.domain.recognition import ChargeDetail, Twin
from app.services.identification import candidate_of, load_candidates


def charge_detail(
    session: Session,
    clock: SimulatedClock,
    customer_id: str,
    transaction_id: str,
    local_currency: str,
    window_days: int,
    case_id: str,
) -> ChargeDetail | None:
    """Reads what the customer sees before disputing a charge.

    Args:
        session: Open session bound to the customer of the JWT.
        clock: Simulated clock; the twin and the earlier months are looked for in the window.
        customer_id: Customer of the session.
        transaction_id: The identified charge.
        local_currency: Local currency of the customer's country.
        window_days: Dispute window, `dispute_window_days` of config/policy.yaml.
        case_id: Case for the audit row.

    Returns:
        The detail, or None when the charge is not a transaction of this customer.
    """
    with timed() as t:
        tx = session.get(Transaction, transaction_id)
        if tx is None or tx.customer_id != customer_id:
            return None
        product = session.get(Product, tx.product_id)
        charge = candidate_of(tx)
        twin = find_twin(charge, load_candidates(session, customer_id, clock, window_days))
        on = tx.transaction_date.date()
        rates = rates_near(session, on, {(tx.currency, local_currency)})
        detail = ChargeDetail(
            transaction_id=tx.transaction_id,
            transaction_type=tx.transaction_type,
            merchant=tx.merchant_name,
            amount=display_amount(tx.amount, tx.currency, local_currency, on, rates),
            at=tx.transaction_date,
            channel=tx.channel,
            city=tx.transaction_city,
            product_type=product.product_type if product else "",
            last4=product.product_number_last4 if product else None,
            status=tx.transaction_status,
            twin=Twin(twin.transaction_id, twin.timestamp, twin.status) if twin else None,
            earlier_months=_earlier_months(session, clock, tx, window_days),
        )
    # The last four digits are shown to the customer only; the trail keeps the product id.
    write_audit(
        session,
        "tool",
        "show_charge_detail",
        case_id,
        {"transaction_id": transaction_id},
        {
            "product_id": tx.product_id,
            "status": detail.status,
            "twin": (
                {"transaction_id": detail.twin.transaction_id, "status": detail.twin.status}
                if detail.twin
                else None
            ),
            "earlier_months": [m.strftime("%Y-%m") for m in detail.earlier_months],
        },
        t["ms"],
    )
    return detail


def _earlier_months(
    session: Session, clock: SimulatedClock, tx: Transaction, window_days: int
) -> tuple[date, ...]:
    """Months before the charge's month, in the window, with charges of the same merchant."""
    if tx.merchant_name is None:
        return ()
    month_start = datetime(tx.transaction_date.year, tx.transaction_date.month, 1)
    dates = session.execute(
        select(Transaction.transaction_date).where(
            Transaction.customer_id == tx.customer_id,
            Transaction.merchant_name == tx.merchant_name,
            Transaction.transaction_status.in_(DISPUTABLE_STATUSES),
            Transaction.transaction_date >= clock.days_ago(window_days),
            Transaction.transaction_date < month_start,
        )
    ).scalars()
    return tuple(sorted({d.date().replace(day=1) for d in dates}))

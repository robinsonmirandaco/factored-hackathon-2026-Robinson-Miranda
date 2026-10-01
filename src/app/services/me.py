"""Reads behind the customer's own screens (TRZ-34): header, products, movements and clarifications.

Every function runs on a session bound to the customer of the JWT, so row level security already
limits each read to that customer; the explicit customer filter repeats the rule in code (design
11.4). Nothing here writes: viewing a screen is not a step of a case and leaves no audit row.
"""

from collections.abc import Mapping
from datetime import date
from typing import Any

from sqlalchemy import select, text, tuple_
from sqlalchemy.orm import Session

from app.adapters.db.models import AuditRecord, Case, Customer, Dispute, Product, Transaction
from app.adapters.db.rates import rates_between
from app.core.errors import AppError
from app.domain.business_days import HolidayCalendar
from app.domain.clock import SimulatedClock
from app.domain.fx import display_amount, local_currency
from app.domain.identification import DISPUTABLE_STATUSES, DISPUTABLE_TYPES
from app.domain.policy_passages import Passage, PolicyDeadline, Unsupported, policy_deadline
from app.schemas.api import (
    ClarificationOut,
    MeOut,
    MovementOut,
    MovementsOut,
    ProductOut,
)
from app.services.info_requests import all_requests, latest_request
from app.services.tools import read_open_claims

# Cases the customer follows in Mis aclaraciones besides its disputes: with a person, or decided
# by one. A case still in conversation lives in the chat; one that only informed or redirected is
# over; a registered one is shown through its dispute. One waiting for the customer's answer to an
# analyst carries the question (TRZ-28). A case stopped for security is not a
# clarification of the customer: it is reviewed by the bank and never listed.
# Statuses of a case a person is reviewing: they carry the review time of their queue priority.
WITH_PERSON_STATUSES = ("failed", "pending_analyst_approval", "escalated")
FOLLOWED_STATUSES = (
    "failed",
    "pending_analyst_approval",
    "escalated",
    "awaiting_customer",
    "approved",
    "rejected",
)


def get_me(session: Session, clock: SimulatedClock, customer_id: str, demo: bool) -> MeOut:
    """The header of the customer screens.

    Args:
        session: Session bound to the customer of the JWT.
        clock: Simulated clock of the service.
        customer_id: Customer of the session.
        demo: Whether demo mode is on.

    Returns:
        First name, country, local currency, the simulated now and the demo flag.

    Raises:
        AppError: 404 if the session's customer is not in the database.
    """
    customer = _customer(session, customer_id)
    return MeOut(
        first_name=customer.first_name,
        country_code=customer.country_code,
        local_currency=local_currency(customer.country_code),
        now=clock.now,
        demo=demo,
    )


def list_products(session: Session, customer_id: str) -> list[ProductOut]:
    """The customer's products.

    Args:
        session: Session bound to the customer of the JWT.
        customer_id: Customer of the session.

    Returns:
        Each product with its type, last four digits, currency, balance, limit and status.
    """
    rows = session.execute(
        select(Product)
        .where(Product.customer_id == customer_id)
        .order_by(Product.product_type, Product.product_id)
    ).scalars()
    return [
        ProductOut(
            product_id=p.product_id,
            product_type=p.product_type,
            last4=p.product_number_last4,
            currency=p.currency,
            current_balance=p.current_balance,
            credit_limit=p.credit_limit,
            status=p.product_status,
        )
        for p in rows
    ]


def list_movements(
    session: Session,
    clock: SimulatedClock,
    customer_id: str,
    window_days: int,
    limit: int,
    before: str | None = None,
) -> MovementsOut:
    """One page of the customer's transactions up to the simulated now, newest first.

    Args:
        session: Session bound to the customer of the JWT.
        clock: Simulated clock of the service.
        customer_id: Customer of the session.
        window_days: Dispute window, `dispute_window_days` of config/policy.yaml.
        limit: Transactions per page.
        before: Id of the last transaction of the previous page, or None for the first page.

    Returns:
        The page, with the approximate local amount of each transaction at its day's rate.

    Raises:
        AppError: 404 if the session's customer is not in the database; 422 if `before` is
            not one of the customer's transactions.
    """
    customer = _customer(session, customer_id)
    local = local_currency(customer.country_code)
    query = select(Transaction).where(
        Transaction.customer_id == customer_id, Transaction.transaction_date <= clock.now
    )
    if before is not None:
        cursor = session.get(Transaction, before)
        # Another customer's id is invisible under RLS, so it reads as a bad cursor.
        if cursor is None:
            raise AppError("invalid_cursor", "The page cursor is not valid.", 422)
        query = query.where(
            tuple_(Transaction.transaction_date, Transaction.transaction_id)
            < tuple_(cursor.transaction_date, cursor.transaction_id)
        )
    rows = list(
        session.execute(
            query.order_by(
                Transaction.transaction_date.desc(), Transaction.transaction_id.desc()
            ).limit(limit + 1)
        ).scalars()
    )
    page, more = rows[:limit], len(rows) > limit
    foreign = {(t.currency, local) for t in page if t.currency != local}
    days = [t.transaction_date.date() for t in page]
    rates = rates_between(session, min(days), max(days), foreign) if foreign and days else {}
    opens = clock.days_ago(window_days)
    items = []
    for t in page:
        shown = display_amount(t.amount, t.currency, local, t.transaction_date.date(), rates)
        items.append(
            MovementOut(
                transaction_id=t.transaction_id,
                at=t.transaction_date,
                transaction_type=t.transaction_type,
                merchant=t.merchant_name,
                amount=t.amount,
                currency=t.currency,
                converted_amount=shown.converted_amount,
                converted_currency=local,
                converted_label=shown.label,
                channel=t.channel,
                city=t.transaction_city,
                status=t.transaction_status,
                disputable=(
                    t.transaction_type in DISPUTABLE_TYPES
                    and t.transaction_status in DISPUTABLE_STATUSES
                    and opens <= t.transaction_date <= clock.now
                ),
            )
        )
    return MovementsOut(items=items, next_before=page[-1].transaction_id if more else None)


def list_clarifications(
    session: Session,
    clock: SimulatedClock,
    passages: Mapping[str, Passage],
    calendars: Mapping[str, HolidayCalendar],
    customer_id: str,
    sla_hours: Mapping[str, float],
) -> list[ClarificationOut]:
    """The customer's clarifications: disputes registered by TRAZO, cases with a person, then
    the bank's open claims.

    Each dispute is read from its own row: its charge, amount and folio are the ones it was
    registered with, whatever later happens to the case. A case with a person has no deadline
    yet: none is made up for it.

    Args:
        session: Session bound to the customer of the JWT.
        clock: Simulated clock of the service.
        passages: Demo policy passages, which back the response deadline.
        calendars: Holiday calendars by country code.
        customer_id: Customer of the session.
        sla_hours: Review time of the queue by priority, `queue.sla_hours` of the policy.

    Returns:
        Disputes newest first, then cases with a person or decided by one, then the open
        complaints of the bank's records.

    Raises:
        AppError: 404 if the session's customer is not in the database.
    """
    customer = _customer(session, customer_id)

    def deadline(start: date) -> PolicyDeadline | Unsupported:
        return policy_deadline(
            dict(passages), dict(calendars), "response_deadline", start, customer.country_code, "es"
        )

    claims = read_open_claims(session, clock, deadline, customer_id)
    by_folio = {c["claim_id"]: c for c in claims if c["source"] == "disputes"}
    disputes = session.execute(
        select(Dispute)
        .where(
            Dispute.customer_id == customer_id,
            Dispute.status == "opened",
            Dispute.folio.is_not(None),
        )
        .order_by(Dispute.business_at.desc(), Dispute.id.desc())
    ).scalars()
    out = []
    with_dispute = set()
    for d in disputes:
        with_dispute.add(d.case_id)
        tx = session.get(Transaction, d.transaction_id)
        case = session.get(Case, d.case_id)
        # An audit an analyst reversed puts the registered dispute in review (TRZ-29 CA5).
        reviewed = case is not None and case.status == "in_review"
        out.append(
            ClarificationOut(
                id=str(d.folio),
                source="disputes",
                status="in_review" if reviewed else "registered",
                case_id=d.case_id,
                intent=d.dispute_type,
                folio=d.folio,
                merchant=tx.merchant_name if tx else None,
                amount=d.amount,
                currency=d.currency,
                charge_at=tx.transaction_date if tx else None,
                **_deadline_fields(by_folio.get(str(d.folio))),
            )
        )
    cases = session.execute(
        select(Case)
        .where(Case.customer_id == customer_id, Case.status.in_(FOLLOWED_STATUSES))
        .order_by(Case.created_at.desc(), Case.id)
    ).scalars()
    for c in cases:
        if c.id in with_dispute:
            continue
        tx = session.get(Transaction, c.transaction_id) if c.transaction_id else None
        priority = session.execute(
            text("SELECT priority FROM case_queue WHERE case_id = :c ORDER BY id DESC LIMIT 1"),
            {"c": c.id},
        ).scalar_one_or_none()
        with_person = c.status in WITH_PERSON_STATUSES and priority is not None
        out.append(
            ClarificationOut(
                id=c.id,
                source="cases",
                review_hours=sla_hours.get(priority) if with_person and priority else None,
                status=c.status,
                case_id=c.id,
                intent=c.intent,
                merchant=tx.merchant_name if tx else None,
                amount=tx.amount if tx else None,
                currency=tx.currency if tx else None,
                charge_at=tx.transaction_date if tx else None,
                info_request=latest_request(session, clock, c.id),
                info_requests=all_requests(session, clock, c.id),
                reason=_rejection_reason(session, c) if c.status == "rejected" else None,
            )
        )
    out += [
        ClarificationOut(id=c["claim_id"], source="complaints", status=c["status"]).model_copy(
            update=_deadline_fields(c)
        )
        for c in claims
        if c["source"] == "complaints"
    ]
    return out


def _rejection_reason(session: Session, case: Case) -> str | None:
    """The reason of the closed list of the analyst's rejection, from its audit row."""
    row = session.execute(
        select(AuditRecord)
        .where(
            AuditRecord.case_id == case.id,
            AuditRecord.actor == "human",
            AuditRecord.action == "decision",
        )
        .order_by(AuditRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    reason = (row.payload or {}).get("reason") if row is not None else None
    return str(reason) if reason else None


def _deadline_fields(claim: dict[str, Any] | None) -> dict[str, Any]:
    if claim is None:
        return {}
    return {
        "opened_on": claim["opened_on"],
        "due_date": claim["due_date"],
        "overdue": claim["overdue"],
        "passage_id": claim["passage_id"],
    }


def _customer(session: Session, customer_id: str) -> Customer:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise AppError("customer_not_found", f"Customer {customer_id} not found.", 404)
    return customer

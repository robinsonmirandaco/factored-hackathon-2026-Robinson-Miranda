"""Identification on the serving database: the session customer's candidates, scored.

The scoring, the conformal set and the decision are `app.domain.identification`, the same code
the evaluation measures; this module only reads the candidates and the rates from Postgres, under
the row level security context of the session customer, and writes the audit row.
"""

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit import timed, write_audit
from app.adapters.db.models import Transaction
from app.adapters.db.rates import rates_between
from app.domain.clock import SimulatedClock
from app.domain.fx import Rates
from app.domain.identification import (
    DISPUTABLE_STATUSES,
    DISPUTABLE_TYPES,
    WINDOW_DAYS,
    Candidate,
    Identification,
    Params,
    dates_of,
    identify,
    identify_from_button,
)
from app.schemas.comprehension import Comprehension


def load_candidates(session: Session, customer_id: str, clock: SimulatedClock) -> list[Candidate]:
    """Disputable transactions of a customer in the window before the simulated now (CA1).

    Args:
        session: Open session bound to the customer of the JWT.
        customer_id: Customer of the session.
        clock: Simulated clock of the service or of the case.

    Returns:
        Purchases, payments and withdrawals, approved or pending, of the last 120 days.
    """
    rows = (
        session.execute(
            select(Transaction)
            .where(
                Transaction.customer_id == customer_id,
                Transaction.transaction_type.in_(DISPUTABLE_TYPES),
                Transaction.transaction_status.in_(DISPUTABLE_STATUSES),
                Transaction.transaction_date.between(clock.days_ago(WINDOW_DAYS), clock.now),
            )
            .order_by(Transaction.transaction_date, Transaction.transaction_id)
        )
        .scalars()
        .all()
    )
    return [
        Candidate(
            transaction_id=r.transaction_id,
            timestamp=r.transaction_date,
            amount=r.amount,
            currency=r.currency,
            channel=r.channel,
            merchant_name=r.merchant_name,
            transaction_type=r.transaction_type,
            status=r.transaction_status,
        )
        for r in rows
    ]


def identify_charge(
    session: Session,
    clock: SimulatedClock,
    customer_id: str,
    clues: Comprehension,
    params: Params,
    local_currency: str,
    case_id: str | None = None,
) -> Identification:
    """Identifies the charge a customer describes in the conversation.

    Args:
        session: Open session bound to the customer of the JWT.
        clock: Simulated clock.
        customer_id: Customer of the session.
        clues: Faithful clues of the redacted message.
        params: Fitted parameters of the comprehension that read the clues.
        local_currency: Local currency of the customer's country.
        case_id: Case for the audit row.

    Returns:
        The identification.
    """
    with timed() as t:
        candidates = load_candidates(session, customer_id, clock)
        span = dates_of(candidates)
        rates: Rates = {}
        if span is not None and clues.amount is not None:
            stated = {clues.amount.currency} if clues.amount.currency else set()
            sources = stated or {c.currency for c in candidates} | {local_currency}
            pairs = {(s, c.currency) for s in sources for c in candidates}
            rates = rates_between(session, span[0], span[1], pairs)
        result = identify(clues, candidates, params, local_currency, rates)
    _audit(session, result, params, len(candidates), case_id, t["ms"])
    return result


def identify_by_button(
    session: Session,
    clock: SimulatedClock,
    customer_id: str,
    transaction_id: str,
    case_id: str | None = None,
) -> Identification:
    """Checks the charge a customer chose with the "No lo reconozco" button.

    Args:
        session: Open session bound to the customer of the JWT.
        clock: Simulated clock.
        customer_id: Customer of the session.
        transaction_id: Transaction the button was pressed on.
        case_id: Case for the audit row.

    Returns:
        Identified when it is one of the customer's candidates, else not found.
    """
    with timed() as t:
        candidates = load_candidates(session, customer_id, clock)
        result = identify_from_button(candidates, transaction_id)
    _audit(session, result, None, len(candidates), case_id, t["ms"])
    return result


def _audit(
    session: Session,
    result: Identification,
    params: Params | None,
    candidates: int,
    case_id: str | None,
    ms: int,
) -> None:
    write_audit(
        session,
        "tool",
        "identify_transaction",
        case_id,
        {
            "door": result.door,
            "params_version": params.version if params else None,
            # JSON has no infinity; "no threshold" is stored as null.
            "reject_below": (
                params.reject_below if params and math.isfinite(params.reject_below) else None
            ),
            "comprehension": params.comprehension if params else None,
        },
        {
            "candidates": candidates,
            "conformal_set": list(result.conformal_set),
            "decision": result.decision,
            "amount_not_convertible": result.amount_not_convertible,
            "rejected": result.rejected,
            "top": [
                {
                    "transaction_id": s.candidate.transaction_id,
                    "probability": round(s.probability, 4),
                    "components": {k: round(v, 3) for k, v in s.components.items()},
                }
                for s in result.scored[:5]
            ],
        },
        ms,
    )

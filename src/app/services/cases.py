"""Operator-side use cases: read cases, traces, histories and the queue, compute metrics."""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select, text, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.db.models import AuditRecord, Case, QueueItem
from app.core.errors import AppError
from app.core.time import utcnow
from app.domain.autonomy import cells_from_audit
from app.domain.history import Lang, describe, group_turns
from app.domain.policy import AutonomyLevel
from app.schemas.api import (
    CaseOut,
    CellMetricsOut,
    HistoryEntryOut,
    LatencyOut,
    MetricsOut,
    QueueFilter,
    QueueItemOut,
    QueueOut,
    SimulatedMetricsOut,
    TraceEventOut,
)

_HISTORY = text(
    "SELECT id, trace_id, actor, action, payload, result, policy_version, latency_ms, created_at "
    "FROM case_history WHERE case_id = :case_id ORDER BY id"
)

# A person decides these cases: an escalation, an action prepared for analyst approval, a case
# stopped by a security rule, and an action whose read-back did not match.
HANDOFF_STATUSES = ("escalated", "pending_analyst_approval", "security_blocked", "failed")

# Intents that are about one charge.
DISPUTE_INTENTS = ("unrecognized_charge", "billing_error_amount", "billing_error_duplicate")
# Recommendations an analyst's approval can run, and whether they include a card block it
# leaves out (design 3.2: a block needs the customer's explicit confirmation).
REGISTERS = {"register": False, "register_and_offer_block": True, "register_and_block": True}
_PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2}
# Queue filters of TRZ-27 CA2 read the reason of the queue row: no charge fits the clues, or an
# action whose read-back did not match (the policy rule, or the reason written after acting).
NO_MATCH_REASON = "escalate.conformal_set_empty"
# Escalation reason of a case whose read-back after acting did not match (TRZ-19 CA3).
VERIFICATION_FAILED_REASON = "verification.registration_failed"
VERIFICATION_REASONS = ("escalate.verification_failed", VERIFICATION_FAILED_REASON)


def check_database(session: Session) -> None:
    """Runs a trivial query to prove the database answers.

    Args:
        session: Open database session.

    Raises:
        AppError: 503 db_unavailable if the query fails.
    """
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc


def get_case(session: Session, case_id: str) -> CaseOut:
    """Reads one case.

    Args:
        session: Open database session.
        case_id: Case to read.

    Returns:
        The case.

    Raises:
        AppError: 404 case_not_found.
    """
    case = _require_case(session, case_id)
    return _to_out(case, recommendation_hidden(session, case))


def get_trace(session: Session, case_id: str) -> list[TraceEventOut]:
    """Lists the audit rows of a case in write order.

    Args:
        session: Open database session.
        case_id: Case whose trace to read.

    Returns:
        The audit rows of the case.

    Raises:
        AppError: 404 case_not_found, or 503 db_unavailable if the database fails.
    """
    try:
        _require_case(session, case_id)
        rows = (
            session.execute(
                select(AuditRecord).where(AuditRecord.case_id == case_id).order_by(AuditRecord.id)
            )
            .scalars()
            .all()
        )
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc
    return [
        TraceEventOut(
            id=r.id,
            actor=r.actor,
            action=r.action,
            payload=r.payload,
            result=r.result,
            latency_ms=r.latency_ms,
            at=r.created_at.isoformat(),
        )
        for r in rows
    ]


def get_history(session: Session, case_id: str, lang: Lang) -> list[HistoryEntryOut]:
    """Tells the steps of a case in plain language, read from the case_history view.

    Args:
        session: Open database session.
        case_id: Case whose history to read.
        lang: Language of the lines.

    Returns:
        One line per audit row of the case, in write order.

    Raises:
        AppError: 404 case_not_found, or 503 db_unavailable if the database fails.
    """
    try:
        _require_case(session, case_id)
        rows = session.execute(_HISTORY, {"case_id": case_id}).mappings().all()
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc
    return [
        HistoryEntryOut(
            id=step.row["id"],
            at=step.row["created_at"].isoformat(),
            trace_id=step.row["trace_id"],
            actor=step.row["actor"],
            action=step.row["action"],
            text=describe(
                step.row["actor"],
                step.row["action"],
                step.row["payload"],
                step.row["result"],
                step.row["policy_version"],
                lang,
            ),
            turn=turn.number,
            turn_kind=turn.kind,
            turn_header=turn.header,
            step=step.number,
            offset_ms=step.offset_ms,
            duration_ms=step.duration_ms,
        )
        for turn in group_turns([dict(r) for r in rows], "analyst", lang)
        for step in turn.steps
    ]


def list_queue(
    session: Session,
    human_review_above: float,
    only: QueueFilter | None = None,
    audit_sample_rate: float = 0.0,
) -> QueueOut:
    """Lists the open rows of the queue a person must decide, most urgent first (TRZ-27).

    The queue is case_queue, not the case status: a case is listed while its row is open, and
    a decision closes the row. Rows are ordered by priority, then by the end of their SLA.

    Args:
        session: Open session with the analyst role.
        human_review_above: USD amount above which a case is a large one, from the policy.
        only: Filter to apply; None lists every open row.
        audit_sample_rate: rho of the policy, returned so the console labels an audit sample.

    Returns:
        The rows of the filter and the counter of every filter over the same open rows.

    Raises:
        AppError: 503 db_unavailable if the database fails.
    """
    try:
        rows = session.execute(
            select(QueueItem, Case)
            .join(Case, Case.id == QueueItem.case_id)
            .where(QueueItem.resolved_at.is_(None))
        ).all()
        now = utcnow()
        items = [_queue_item(session, q, c, now) for q, c in rows]
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc
    items.sort(key=lambda i: (_PRIORITY_RANK[i.priority], i.sla_due_at or datetime.max, i.queue_id))
    tests: dict[str, Callable[[QueueItemOut], bool]] = {
        "high_priority": lambda i: i.priority in ("high", "urgent"),
        "over_1000_usd": lambda i: i.amount_usd is not None and i.amount_usd > human_review_above,
        "no_match": lambda i: i.reason == NO_MATCH_REASON,
        "verification_failed": lambda i: i.status == "failed" or i.reason in VERIFICATION_REASONS,
        "audit": lambda i: i.kind == "audit_sample",
    }
    counts = {"all": len(items)} | {k: sum(map(f, items)) for k, f in tests.items()}
    return QueueOut(
        items=[i for i in items if only is None or tests[only](i)],
        counts=counts,
        audit_sample_rate=audit_sample_rate,
    )


def approvable(case: Case) -> bool:
    """Tells whether approving the case has a registration to run.

    Args:
        case: A case with a person.

    Returns:
        True when the recommendation registers a dispute on an identified charge.
    """
    return (
        case.recommended_action in REGISTERS
        and case.transaction_id is not None
        and case.intent in DISPUTE_INTENTS
    )


def recommendation_hidden(session: Session, case: Case) -> bool:
    """Tells whether the system handed the case over without proposing an action.

    At autonomy level A2 the system still computes and records what it would have recommended,
    but does not show it; the analyst decides alone and the decision is compared against it
    (design 6.7, TRZ-30 CA7).

    Args:
        session: Open session.
        case: The case.

    Returns:
        True when the latest policy decision on the case consulted a cell at A2.
    """
    decide = session.execute(
        select(AuditRecord.result)
        .where(
            AuditRecord.case_id == case.id,
            AuditRecord.actor == "policy",
            AuditRecord.action == "decide",
        )
        .order_by(AuditRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return (decide or {}).get("autonomy_level") == "A2"


def injection_stop(session: Session, case: Case) -> bool:
    """Tells whether the case was stopped for an instruction injected in its message.

    TRZ-27 CA8 hides a security event because it tried to reach another customer's data. In an
    injection the data are the customer's own: the analyst sees them and decides the case like
    any other (TRZ-46 follow-up).

    Args:
        session: Open session.
        case: The case.

    Returns:
        True when its latest security event was raised by an injected instruction.
    """
    event = session.execute(
        select(AuditRecord)
        .where(
            AuditRecord.case_id == case.id,
            AuditRecord.actor == "agent",
            AuditRecord.action == "security_event",
        )
        .order_by(AuditRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return event is not None and (event.payload or {}).get("reason") == "instruction_in_text"


def _queue_item(session: Session, q: QueueItem, c: Case, now: datetime) -> QueueItemOut:
    injection = c.status == "security_blocked" and injection_stop(session, c)
    # Another customer's data is never shown (CA8); an injection is the customer's own case.
    security = c.status == "security_blocked" and not injection
    return QueueItemOut(
        queue_id=q.id,
        case_id=c.id,
        kind="security_event" if security or injection else q.kind,  # type: ignore[arg-type]
        injection=injection,
        # A security event shows no customer data (CA8).
        customer_id=None if security else c.customer_id,
        intent=None if security else c.intent,
        amount_usd=None if security else _amount_usd(session, c.id),
        language=c.language,
        reason=q.reason,
        priority=q.priority,
        sla_due_at=q.sla_due_at,
        overdue=q.sla_due_at is not None and q.sla_due_at < now,
        updated=q.updated,
        status=c.status,
        recommended_action=(
            None if security or recommendation_hidden(session, c) else c.recommended_action
        ),
        # Approving a security event closes it; any other case needs a registration to run.
        can_approve=security or approvable(c),
        created_at=q.created_at,
        simulated=c.simulated,
    )


def _amount_usd(session: Session, case_id: str) -> float | None:
    """The USD amount the policy compared when it handed the case over."""
    decide = session.execute(
        select(AuditRecord)
        .where(
            AuditRecord.case_id == case_id,
            # The reading beside an injected instruction compares the amount after the stop.
            tuple_(AuditRecord.actor, AuditRecord.action).in_(
                [("policy", "decide"), ("agent", "read_beside_instruction")]
            ),
        )
        .order_by(AuditRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    value = ((decide.payload if decide else None) or {}).get("amount_usd")
    return None if value is None else float(value)


_TURNS = text(
    "SELECT case_id, result->>'outcome' AS outcome FROM audit_log "
    "WHERE actor = 'agent' AND action = 'turn_complete' ORDER BY id"
)
_LATENCY = text(
    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms), "
    "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) FROM audit_log "
    "WHERE actor = 'agent' AND action = 'turn_complete' AND latency_ms IS NOT NULL"
)
_USAGE = text(
    "SELECT coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0), "
    "coalesce(sum(cost_usd), 0) FROM audit_log"
)
_SIMULATED = text(
    "SELECT DISTINCT case_id FROM audit_log WHERE actor = 'system' AND action = 'mark_simulated'"
)
# The rows the autonomy of a cell is made of: the reviews and the blocks they closed.
_REVIEWS = text(
    "SELECT action, payload, result FROM audit_log "
    "WHERE (actor = 'human' AND action = 'decision' AND result ? 'review') "
    "OR (actor = 'system' AND action = 'autonomy_block') ORDER BY id"
)


def get_metrics(session: Session, initial_level: AutonomyLevel) -> MetricsOut:
    """Computes the operational metrics of design 11.6 from the audit log alone (TRZ-37).

    A case counts once it has a customer turn (turn_complete). It is handed to a person when one
    of its turns ended in a handoff status; the other cases are contained. A case the demo state
    created is the one with a mark_simulated row: it counts in the totals and again in
    `simulated`. Tokens and cost add up every step that called the LLM; a reply written by code
    and the receipt of a registration made no call and add nothing, and a handoff is not an LLM
    fallback. The cells are rebuilt from the reviews and closed blocks, never read from
    autonomy_cells.

    Args:
        session: Open session with the analyst role, which reads every row.
        initial_level: `autonomy.initial_level` of the policy.

    Returns:
        The metrics.

    Raises:
        AppError: 503 db_unavailable if the database fails.
    """
    try:
        turns = session.execute(_TURNS).all()
        p50, p95 = session.execute(_LATENCY).one()
        input_tokens, output_tokens, cost = session.execute(_USAGE).one()
        simulated = set(session.execute(_SIMULATED).scalars())
        reviews = session.execute(_REVIEWS).all()
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc
    seen: dict[str, None] = {}
    handoff: dict[str, str] = {}
    for case_id, outcome in turns:
        seen.setdefault(case_id)
        if outcome in HANDOFF_STATUSES:
            handoff.setdefault(case_id, outcome)
    cases = list(seen)
    contained = [c for c in cases if c not in handoff]
    by_outcome = {s: 0 for s in HANDOFF_STATUSES}
    for outcome in handoff.values():
        by_outcome[outcome] += 1
    cells = cells_from_audit(((a, p, r) for a, p, r in reviews), initial_level)
    return MetricsOut(
        cases=len(cases),
        contained=len(contained),
        containment=round(len(contained) / len(cases), 4) if cases else None,
        handed_to_person=by_outcome,
        turns=len(turns),
        turn_latency_ms=(
            None if p50 is None else LatencyOut(p50=round(float(p50), 1), p95=round(float(p95), 1))
        ),
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cost_usd=round(float(cost), 6),
        simulated=SimulatedMetricsOut(
            cases=sum(c in simulated for c in cases),
            contained=sum(c in simulated for c in contained),
            handed_to_person=sum(c in simulated for c in handoff),
        ),
        autonomy=[
            CellMetricsOut(
                intent=intent,
                language=language,
                level=state.level,
                block_reviews=state.reviews,
                block_reversals=state.reversals,
                good_blocks=state.good_blocks,
            )
            for (intent, language), state in sorted(cells.items())
        ],
    )


def _require_case(session: Session, case_id: str) -> Case:
    case = session.get(Case, case_id)
    if case is None:
        raise AppError("case_not_found", f"Case {case_id} not found.", 404)
    return case


def _to_out(c: Case, hidden: bool) -> CaseOut:
    return CaseOut(
        case_id=c.id,
        customer_id=c.customer_id,
        transaction_id=c.transaction_id,
        intent=c.intent,
        status=c.status,
        autonomy_level=c.autonomy_level,
        recommended_action=None if hidden else c.recommended_action,
        escalation_reason=c.escalation_reason,
        human_decision=c.human_decision,
        summary=c.summary,
        trace_id=c.trace_id,
        created_at=c.created_at.isoformat() if c.created_at else None,
        simulated=c.simulated,
    )

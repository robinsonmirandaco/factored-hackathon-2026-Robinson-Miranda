"""Autonomy cells in the database (TRZ-30): the level the policy consults, and the reviews that
move it.

Every analyst decision that carries a `review` block (decisions.py) is added to the block of its
intent x language cell in the same transaction, under a row lock, so reviews count in the order
they were decided and a block closes exactly once. Closing a block writes one audit row with the
cell, r, W, N, the threshold crossed and the reversed cases with their reasons (CA8). Every
session reads a cell, so its row keeps counts and no case: the reversed cases come from the
audit log when the block closes.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import AuditRecord, AutonomyCell
from app.core.time import utcnow
from app.domain.autonomy import BlockClosed, CellState, apply_review
from app.domain.policy import Autonomy, AutonomyLevel, Language
from app.schemas.comprehension import Intent


@dataclass(frozen=True)
class CellStatus:
    """What the policy and the trace need of a cell.

    Attributes:
        level: Autonomy level in force.
        last_change: The block that set this level (r, w, n, threshold, audit_id, ...); None
            while the cell has never changed.
    """

    level: AutonomyLevel
    last_change: dict[str, Any] | None = None


# (session, intent, language) -> status of that cell, read on every policy decision.
CellReader = Callable[[Session, Intent, Language], CellStatus]


def cell_reader(initial: AutonomyLevel) -> CellReader:
    """The reader of the service: a cell with no row yet is at the initial level.

    Args:
        initial: `autonomy.initial_level` of the policy.

    Returns:
        A reader that queries autonomy_cells in the caller's session.
    """

    def read(session: Session, intent: Intent, language: Language) -> CellStatus:
        row = session.get(AutonomyCell, (intent, language))
        if row is None:
            return CellStatus(initial)
        return CellStatus(row.level, row.last_change)  # type: ignore[arg-type]

    return read


def record_review(
    session: Session, params: Autonomy, review: dict[str, Any], case_id: str
) -> BlockClosed | None:
    """Adds one review to its cell; when it closes the block, evaluates it and audits it.

    Args:
        session: Open session with the analyst role, the one of the decision.
        params: Autonomy settings of the policy.
        review: The `review` block of the decision: cell, reversal and reason.
        case_id: The reviewed case.

    Returns:
        What the closed block decided, or None while the block is open.
    """
    intent, language = review["cell"]["intent"], review["cell"]["language"]
    session.execute(
        insert(AutonomyCell)
        .values(intent=intent, language=language, level=params.initial_level)
        .on_conflict_do_nothing()
    )
    row = session.execute(
        select(AutonomyCell)
        .where(AutonomyCell.intent == intent, AutonomyCell.language == language)
        .with_for_update()
    ).scalar_one()
    state = CellState(
        level=row.level,  # type: ignore[arg-type]
        reviews=row.block_reviews,
        reversals=row.block_reversals,
        good_blocks=row.good_blocks,
    )
    state, closed = apply_review(state, bool(review["reversal"]), params)
    row.level = state.level
    row.block_reviews, row.block_reversals = state.reviews, state.reversals
    row.good_blocks = state.good_blocks
    row.updated_at = utcnow()
    if closed is not None:
        evidence = block_result(closed, params)
        result = {**evidence, "reversed": _reversed(session, row)}
        audit = write_audit(
            session,
            "system",
            "autonomy_block",
            case_id,
            {"cell": {"intent": intent, "language": language}},
            result,
            # The row names cases of other customers: it belongs to none of them.
            customer_id=None,
        )
        row.block_after = audit.id
        if closed.changed:
            row.last_change = {**evidence, "audit_id": audit.id}
    session.flush()
    return closed


def _reversed(session: Session, cell: AutonomyCell) -> list[dict[str, Any]]:
    """The reversed cases of the block that just closed, with their reasons, in decision order.

    They are the decisions with a reversal review of the cell written after the previous closed
    block; the decision that closes the block is already written.
    """
    rows = session.execute(
        select(AuditRecord.case_id, AuditRecord.result)
        .where(
            AuditRecord.actor == "human",
            AuditRecord.action == "decision",
            AuditRecord.id > (cell.block_after or 0),
        )
        .order_by(AuditRecord.id)
    ).all()
    out = []
    for case_id, result in rows:
        review = (result or {}).get("review")
        if (
            review
            and review["reversal"]
            and review["cell"]
            == {
                "intent": cell.intent,
                "language": cell.language,
            }
        ):
            out.append({"case_id": case_id, "reason": review["reason"]})
    return out


def block_result(closed: BlockClosed, params: Autonomy) -> dict[str, Any]:
    """The audit view of a closed block (design 6.7, CA8), with the value of its threshold.

    Args:
        closed: What the block decided.
        params: Autonomy settings of the policy.

    Returns:
        Level before and after, r, W, N, z, the threshold crossed and its value.
    """
    return {
        "level_before": closed.level_before,
        "level_after": closed.level_after,
        "changed": closed.changed,
        "n": closed.n,
        "reversals": closed.reversals,
        "r": round(closed.r, 4),
        "w": round(closed.w, 4),
        "z": closed.z,
        "threshold": closed.threshold,
        "threshold_value": getattr(params, closed.threshold) if closed.threshold else None,
        "good_blocks": closed.good_blocks,
    }

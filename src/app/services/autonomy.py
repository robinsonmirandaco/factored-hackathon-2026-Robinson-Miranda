"""Autonomy cells in the database (TRZ-30): the level the policy consults, and the reviews that
move it.

Every analyst decision that carries a `review` block (decisions.py) is added to the block of its
intent x language cell in the same transaction, under a row lock, so reviews count in the order
they were decided and a block closes exactly once. Closing a block writes one audit row with the
cell, r, W, N, the threshold crossed and the reversed cases with their reasons (CA8). Every
session reads a cell, so its row keeps counts and no case: the reversed cases come from the
audit log when the block closes.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, get_args

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import AuditRecord, AutonomyCell, Case
from app.core.errors import AppError
from app.core.time import utcnow
from app.domain.autonomy import BlockClosed, CellState, apply_review
from app.domain.policy import Autonomy, AutonomyLevel, Language
from app.schemas.api import (
    AutonomyCellOut,
    AutonomyOut,
    ClosedBlockOut,
    ReversedCaseOut,
    ThresholdsOut,
)
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


_DECISIONS = text(
    "SELECT id, case_id, result->'review' FROM audit_log "
    "WHERE actor = 'human' AND action = 'decision' AND result ? 'review' ORDER BY id"
)
_BLOCKS = text(
    "SELECT id, case_id, payload->'cell', result FROM audit_log "
    "WHERE actor = 'system' AND action = 'autonomy_block' ORDER BY id"
)


def autonomy_status(
    session: Session, params: Autonomy, intents: Sequence[Intent], demo_mode: bool
) -> AutonomyOut:
    """The Estado de autonomía tab (TRZ-31): every cell, its blocks and its reversed cases.

    The level and the open block are read from autonomy_cells, the row the policy consults; a
    cell with no row yet is at the initial level with an empty block. The closed blocks and the
    reversed cases come from the audit log. No real-clock date is returned: the screens show
    only dates of the simulated clock (design 10.2, rule 7).

    Args:
        session: Open session with the analyst role.
        params: Autonomy settings of the policy.
        intents: `dispute_intents` of the policy, the intents that act and so have a cell.
        demo_mode: Whether the app runs in demo mode.

    Returns:
        The thresholds, one row per intent x language and whether the data are simulated.

    Raises:
        AppError: 503 db_unavailable if the database fails.
    """
    try:
        stored = {(c.intent, c.language): c for c in session.scalars(select(AutonomyCell))}
        decisions = session.execute(_DECISIONS).all()
        blocks = session.execute(_BLOCKS).all()
        reviewed = {case_id for _, case_id, _ in decisions}
        simulated = set(
            session.scalars(select(Case.id).where(Case.id.in_(reviewed), Case.simulated))
        )
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc
    by_id = {block_id: _block_out(block_id, case_id, r) for block_id, case_id, _, r in blocks}
    last_block = {(c["intent"], c["language"]): by_id[i] for i, _, c, _ in blocks}
    closed_reversed = {(c["intent"], c["language"]): r.get("reversed", []) for _, _, c, r in blocks}

    def reversed_out(items: list[dict[str, Any]]) -> list[ReversedCaseOut]:
        return [
            ReversedCaseOut(
                case_id=i["case_id"], reason=i["reason"], simulated=i["case_id"] in simulated
            )
            for i in items
        ]

    cells = []
    for intent in intents:
        for language in get_args(Language):
            key = (intent, language)
            row = stored.get(key)
            reviews, reversals = (row.block_reviews, row.block_reversals) if row else (0, 0)
            after = (row.block_after if row else None) or 0
            open_reversed = [
                {"case_id": case_id, "reason": review["reason"]}
                for decision_id, case_id, review in decisions
                if decision_id > after
                and review["reversal"]
                and (review["cell"]["intent"], review["cell"]["language"]) == key
            ]
            change = (row.last_change or {}) if row else {}
            cells.append(
                AutonomyCellOut(
                    intent=intent,
                    language=language,
                    level=row.level if row else params.initial_level,
                    block_reviews=reviews,
                    block_reversals=reversals,
                    rate=round(reversals / reviews, 4) if reviews else None,
                    last_block=last_block.get(key),
                    last_change=by_id.get(change.get("audit_id")),
                    reversed_last_block=reversed_out(closed_reversed.get(key, [])),
                    reversed_open_block=reversed_out(open_reversed),
                )
            )
    return AutonomyOut(
        thresholds=ThresholdsOut(
            window_n=params.window_n,
            z=params.z,
            demote_if_wilson_lower_gte=params.demote_if_wilson_lower_gte,
            promote_if_rate_lt=params.promote_if_rate_lt,
            promote_after_consecutive_windows=params.promote_after_consecutive_windows,
            audit_sample_rate=params.audit_sample_rate,
        ),
        cells=cells,
        simulated=demo_mode or bool(simulated),
    )


def _block_out(audit_id: int, case_id: str | None, r: dict[str, Any]) -> ClosedBlockOut:
    return ClosedBlockOut(
        audit_id=audit_id,
        closed_by=case_id,
        n=r["n"],
        reversals=r["reversals"],
        r=r["r"],
        w=r["w"],
        threshold=r.get("threshold"),
        threshold_value=r.get("threshold_value"),
        level_before=r["level_before"],
        level_after=r["level_after"],
        changed=r["changed"],
    )

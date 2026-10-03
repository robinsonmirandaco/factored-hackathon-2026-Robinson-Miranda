"""Autonomy of an intent x language cell governed by the Wilson lower bound (design 6.7).

Reviews of a cell are grouped in consecutive, non-overlapping blocks of N, in the order the
analyst decided them. Only a closed block is evaluated: with fewer than N reviews no bound is
computed. A block whose Wilson lower bound reaches the demotion threshold takes the cell down one
level; consecutive blocks with a reversal rate under the promotion threshold take it up one. Losing
autonomy takes one block and earning it back takes several, on purpose. Pure functions, no I/O:
the service stores the state and the evaluation simulates with the same code.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from math import sqrt
from typing import Any

from app.domain.policy import Autonomy, AutonomyLevel

LEVELS: tuple[AutonomyLevel, ...] = ("A0", "A1", "A2")


def wilson_lower(reversals: int, n: int, z: float) -> float:
    """Lower bound of the Wilson score interval of a proportion.

    Args:
        reversals: Reversals in the block.
        n: Reviews in the block; at least 1.
        z: Normal quantile; 1.645 is one-sided 95% confidence.

    Returns:
        W: with that confidence the true error rate of the cell is at least W. Never negative,
        so 0 reversals give exactly 0 instead of a rounding residue.
    """
    r = reversals / n
    centre = r + z * z / (2 * n)
    spread = z * sqrt(r * (1 - r) / n + z * z / (4 * n * n))
    return max(0.0, (centre - spread) / (1 + z * z / n))


@dataclass(frozen=True)
class CellState:
    """Level of a cell and its open block.

    Attributes:
        level: Autonomy level in force.
        reviews: Reviews in the open block.
        reversals: Reversals among them.
        good_blocks: Consecutive closed blocks with a rate under the promotion threshold since
            the last change of level.
    """

    level: AutonomyLevel
    reviews: int = 0
    reversals: int = 0
    good_blocks: int = 0


@dataclass(frozen=True)
class BlockClosed:
    """What closing a block decided; the audit row of design 6.7 is written from it.

    Attributes:
        level_before: Level while the block was open.
        level_after: Level from now on.
        n: Reviews in the block.
        reversals: Reversals in the block.
        r: Reversal rate.
        w: Wilson lower bound of the rate.
        z: Quantile used.
        threshold: Policy key of the threshold the block crossed (`demote_if_wilson_lower_gte`
            or `promote_if_rate_lt`), or None.
        good_blocks: Consecutive good blocks after this one.
    """

    level_before: AutonomyLevel
    level_after: AutonomyLevel
    n: int
    reversals: int
    r: float
    w: float
    z: float
    threshold: str | None
    good_blocks: int

    @property
    def changed(self) -> bool:
        """True when the block moved the cell to another level."""
        return self.level_after != self.level_before


def apply_review(
    state: CellState, reversal: bool, params: Autonomy
) -> tuple[CellState, BlockClosed | None]:
    """Adds one review to the open block and evaluates the block when it closes.

    Args:
        state: The cell before the review.
        reversal: The analyst decided differently from what the system did or recommended.
        params: Autonomy settings of the policy.

    Returns:
        The cell after the review, with a new empty block when this review closed one, and what
        the closed block decided (None while the block is open).
    """
    reviews = state.reviews + 1
    reversals = state.reversals + int(reversal)
    if reviews < params.window_n:
        return replace(state, reviews=reviews, reversals=reversals), None
    r = reversals / reviews
    w = wilson_lower(reversals, reviews, params.z)
    at = LEVELS.index(state.level)
    after, threshold, good = state.level, None, 0
    if w >= params.demote_if_wilson_lower_gte:
        threshold = "demote_if_wilson_lower_gte"
        after = LEVELS[min(at + 1, len(LEVELS) - 1)]
    elif r < params.promote_if_rate_lt:
        good = state.good_blocks + 1
        if good >= params.promote_after_consecutive_windows and at > 0:
            threshold, after = "promote_if_rate_lt", LEVELS[at - 1]
    if after != state.level:
        good = 0
    closed = BlockClosed(
        level_before=state.level,
        level_after=after,
        n=reviews,
        reversals=reversals,
        r=r,
        w=w,
        z=params.z,
        threshold=threshold,
        good_blocks=good,
    )
    return CellState(level=after, good_blocks=good), closed


def cells_from_audit(
    rows: Iterable[tuple[str, dict[str, Any] | None, dict[str, Any] | None]],
    initial: AutonomyLevel,
) -> dict[tuple[str, str], CellState]:
    """Rebuilds the state of every cell from the audit log alone (TRZ-37 CA2).

    The level and the good blocks are the ones the last closed block of the cell wrote; the open
    block is the reviews of the cell decided after it. The service keeps the same state in
    autonomy_cells, so both must agree.

    Args:
        rows: In write order, the analyst decisions that carry a review, as ("decision", payload,
            result), and the closed blocks, as ("autonomy_block", payload, result).
        initial: `autonomy.initial_level` of the policy, the level of a cell with no closed block.

    Returns:
        The state of each (intent, language) cell that has at least one review.
    """
    cells: dict[tuple[str, str], CellState] = {}
    for action, payload, result in rows:
        if action == "autonomy_block":
            cell = (payload or {})["cell"]
            key = (cell["intent"], cell["language"])
            r = result or {}
            cells[key] = CellState(level=r["level_after"], good_blocks=r.get("good_blocks", 0))
            continue
        review = (result or {}).get("review")
        if not review:
            continue
        key = (review["cell"]["intent"], review["cell"]["language"])
        state = cells.get(key, CellState(initial))
        cells[key] = replace(
            state,
            reviews=state.reviews + 1,
            reversals=state.reversals + int(bool(review["reversal"])),
        )
    return cells

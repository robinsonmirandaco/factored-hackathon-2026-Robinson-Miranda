"""Wilson lower bound and the autonomy level of a cell (TRZ-30, design 6.7)."""

from typing import Any

import pytest

from app.domain.autonomy import CellState, apply_review, wilson_lower
from app.domain.policy import load_policy

PARAMS = load_policy("config/policy.yaml").autonomy
Z = PARAMS.z


def _block(state: CellState, reversals: int) -> tuple[CellState, Any]:
    """Feeds one block of N reviews, the reversals first; returns the state and the last result."""
    closed = None
    for i in range(PARAMS.window_n):
        state, closed = apply_review(state, i < reversals, PARAMS)
    return state, closed


def test_the_policy_declares_the_parameters_of_the_design() -> None:
    assert (PARAMS.window_n, PARAMS.z) == (20, 1.645)
    assert PARAMS.demote_if_wilson_lower_gte == 0.30
    assert PARAMS.promote_if_rate_lt == 0.15
    assert PARAMS.promote_after_consecutive_windows == 2


@pytest.mark.parametrize(("k", "expected"), [(10, 0.327), (9, 0.284)])
def test_wilson_reproduces_the_reference_values(k: int, expected: float) -> None:
    # CA3: with z = 1.645, 10 of 20 gives W ~ 0.327 and 9 of 20 gives W ~ 0.284.
    assert wilson_lower(k, 20, Z) == pytest.approx(expected, abs=5e-4)


def test_no_reversal_gives_exactly_zero() -> None:
    assert wilson_lower(0, 20, Z) == 0.0


def test_ten_reversals_is_the_least_that_demotes_a_block_of_20() -> None:
    assert wilson_lower(10, 20, Z) >= PARAMS.demote_if_wilson_lower_gte
    assert wilson_lower(9, 20, Z) < PARAMS.demote_if_wilson_lower_gte


def test_an_open_block_computes_nothing() -> None:
    # CA2: with fewer than N reviews no bound is computed, however many reversals there are.
    state = CellState(level="A0")
    for _ in range(PARAMS.window_n - 1):
        state, closed = apply_review(state, True, PARAMS)
        assert closed is None
    assert (state.level, state.reviews, state.reversals) == ("A0", 19, 19)


def test_ten_of_20_takes_the_cell_down_one_level_and_starts_a_new_block() -> None:
    # CA4.
    state, closed = _block(CellState(level="A0"), 10)
    assert closed.changed and (closed.level_before, closed.level_after) == ("A0", "A1")
    assert closed.threshold == "demote_if_wilson_lower_gte"
    assert (closed.n, closed.reversals, closed.r) == (20, 10, 0.5)
    assert state == CellState(level="A1")


def test_nine_of_20_keeps_the_level_and_starts_a_new_block() -> None:
    state, closed = _block(CellState(level="A0"), 9)
    assert not closed.changed and closed.threshold is None
    assert state == CellState(level="A0")


def test_a1_goes_down_to_a2_and_a2_is_the_floor() -> None:
    state, _ = _block(CellState(level="A1"), 12)
    assert state.level == "A2"
    state, closed = _block(state, 20)
    assert state.level == "A2" and not closed.changed


def test_the_cell_goes_up_only_after_two_blocks_in_a_row_under_015() -> None:
    # CA5: one good block is not enough; the second one in a row promotes.
    state, closed = _block(CellState(level="A2"), 2)
    assert state.level == "A2" and state.good_blocks == 1 and not closed.changed
    state, closed = _block(state, 2)
    assert (closed.level_before, closed.level_after) == ("A2", "A1")
    assert closed.threshold == "promote_if_rate_lt"
    assert state == CellState(level="A1")


def test_a_block_at_or_over_015_breaks_the_streak() -> None:
    state, _ = _block(CellState(level="A1"), 0)
    state, _ = _block(state, 3)  # r = 0.15 is not under 0.15
    assert state.good_blocks == 0
    state, _ = _block(state, 0)
    assert state.level == "A1" and state.good_blocks == 1


def test_a0_is_the_ceiling() -> None:
    state, _ = _block(CellState(level="A0"), 0)
    state, closed = _block(state, 0)
    assert state.level == "A0" and not closed.changed


def test_a_change_of_level_resets_the_streak() -> None:
    state, _ = _block(CellState(level="A1"), 0)
    assert state.good_blocks == 1
    state, _ = _block(state, 10)
    assert state == CellState(level="A2")

"""Audit sample draws (TRZ-29): rate, reproducibility and the seed of the policy."""

import random

from app.domain.audit_sample import draw
from app.domain.policy import load_policy

POLICY = load_policy("config/policy.yaml")
SEED = POLICY.autonomy.audit_sample_seed
RHO = POLICY.autonomy.audit_sample_rate


def test_the_policy_declares_rho_and_a_seed() -> None:
    assert RHO == 0.10
    assert SEED == 20260934


def test_a_simulation_of_1000_cases_selects_between_8_and_12_percent() -> None:
    # CA4: the selection rate of 1,000 resolved cases with the seed of the policy.
    selected = sum(draw(SEED, n, RHO).selected for n in range(1, 1001))
    assert 80 <= selected <= 120, selected


def test_the_same_seed_gives_the_same_draws() -> None:
    first = [draw(SEED, n, RHO) for n in range(1, 1001)]
    again = [draw(SEED, n, RHO) for n in range(1, 1001)]
    assert first == again


def test_a_draw_depends_only_on_the_seed_and_its_number() -> None:
    # Drawn out of order, each one is the same: anyone recomputes draw n from its audit row.
    forward = {n: draw(SEED, n, RHO).u for n in range(1, 51)}
    backward = {n: draw(SEED, n, RHO).u for n in reversed(range(1, 51))}
    assert forward == backward
    assert draw(SEED, 7, RHO).u == random.Random(f"{SEED}:7").random()


def test_another_seed_gives_other_draws() -> None:
    assert [draw(SEED, n, RHO).u for n in range(1, 21)] != [
        draw(SEED + 1, n, RHO).u for n in range(1, 21)
    ]


def test_with_the_seed_of_the_policy_the_first_resolved_case_is_selected() -> None:
    # Declared in the trace and the README: the first case of a fresh demo is an audit sample.
    first = draw(SEED, 1, RHO)
    assert first.selected and first.u < RHO


def test_a_case_is_selected_exactly_when_u_is_below_rho() -> None:
    for n in range(1, 201):
        d = draw(SEED, n, RHO)
        assert d.selected == (d.u < RHO)
        assert 0 <= d.u < 1

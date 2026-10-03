"""Autonomy watch of the evaluation (TRZ-47): the seeded simulation, the replay of a degraded
cell and the degraded prompt."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.domain.policy import load_policy
from pipeline import autonomy_watch as aw

PARAMS = load_policy("config/policy.yaml").autonomy
PROMPT = Path("config/prompts/comprehension.yaml")


def test_the_exact_probability_of_a_demotion_in_one_block() -> None:
    assert aw.block_demotion_probability(0.10, PARAMS) == pytest.approx(7.15e-6, rel=1e-2)
    assert aw.block_demotion_probability(0.40, PARAMS) == pytest.approx(0.2447, abs=1e-4)


def test_the_same_seed_gives_the_same_simulation() -> None:
    assert aw.simulate_rate(0.4, PARAMS, streams=200) == aw.simulate_rate(0.4, PARAMS, streams=200)
    assert aw.simulate_rate(0.4, PARAMS, streams=200) != aw.simulate_rate(
        0.4, PARAMS, seed=1, streams=200
    )


def test_the_simulation_agrees_with_the_exact_probability() -> None:
    r = aw.simulate_rate(0.4, PARAMS, streams=4000)
    se = (r.exact_block * (1 - r.exact_block) / r.streams) ** 0.5
    assert abs(r.demoted_first_block / r.streams - r.exact_block) < 4 * se
    assert all(n % PARAMS.window_n == 0 for n in r.reviews_to_detect)


def _o(kind: str, wrong: bool, unsafe: bool = False) -> aw.Outcome:
    return aw.Outcome(("unrecognized_charge", "pt"), kind, wrong, unsafe)  # type: ignore[arg-type]


def test_a_cell_whose_reviews_are_all_reversed_goes_down_and_stops_unsafe_outcomes() -> None:
    pool = [_o("review", True), _o("auto", True, True)]
    r = aw.replay(pool, PARAMS, streams=20, cases=200)
    assert len(r.detected_at_case) == 20
    # The first block closes at review 20, so nothing is detected before it.
    assert min(r.detected_at_review) == PARAMS.window_n
    assert sum(r.unsafe_with) < sum(r.unsafe_without)
    assert r.final_levels == {"A2": 20}


def test_a_cell_that_is_never_wrong_keeps_a0() -> None:
    pool = [_o("review", False), _o("auto", False), _o("other", False)]
    r = aw.replay(pool, PARAMS, streams=20, cases=200)
    assert r.detected_at_case == [] and r.final_levels == {"A0": 20}
    assert sum(r.unsafe_without) == sum(r.unsafe_with) == 0


def test_the_replay_is_reproducible() -> None:
    pool = [_o("review", True), _o("review", False), _o("auto", True, True), _o("other", False)]
    first = aw.replay(pool, PARAMS, streams=50, cases=300)
    again = aw.replay(pool, PARAMS, streams=50, cases=300)
    assert (first.detected_at_case, first.unsafe_with) == (
        again.detected_at_case,
        again.unsafe_with,
    )


def _scored(**kw: Any) -> SimpleNamespace:
    case = SimpleNamespace(
        intent="unrecognized_charge",
        variant="pt-BR",
        truth=SimpleNamespace(transaction_id="T1"),
        scenario=SimpleNamespace(twin_transaction_id=None),
        expected=SimpleNamespace(action=kw.pop("expected", "register_and_offer_block")),
    )
    score = SimpleNamespace(
        acted=kw.pop("acted", False),
        handed_off=kw.pop("handed_off", False),
        correct=kw.pop("correct", True),
    )
    final = SimpleNamespace(
        recommended=kw.pop("recommended", None), handoffs=kw.pop("handoffs", [])
    )
    return SimpleNamespace(case=case, score=score, run=SimpleNamespace(final=final), **kw)


def test_a_case_the_system_resolved_alone_is_an_audit_candidate() -> None:
    o = aw.outcome(_scored(acted=True, unsafe=("wrong_charge",), correct=False))
    assert (o.cell, o.kind, o.wrong, o.unsafe) == (
        ("unrecognized_charge", "pt"),
        "auto",
        True,
        True,
    )
    assert aw.outcome(_scored(acted=True, unsafe=())).wrong is False


def test_a_handover_is_reversed_when_its_recommended_charge_is_not_the_label() -> None:
    right = _scored(handed_off=True, unsafe=(), recommended=[("T1", "register_and_offer_block")])
    wrong = _scored(handed_off=True, unsafe=(), recommended=[("T9", "register_and_offer_block")])
    assert (aw.outcome(right).kind, aw.outcome(right).wrong) == ("review", False)
    assert (aw.outcome(wrong).kind, aw.outcome(wrong).wrong) == ("review", True)
    # No registration recommended, as when no charge matched: nothing to review.
    none = _scored(handed_off=True, unsafe=(), recommended=[(None, "register_and_offer_block")])
    assert aw.outcome(none).kind == "other"


def test_runs_without_the_recommended_charge_fall_back_to_the_rule() -> None:
    approval = [("escalation", "approval.amount_above_auto_register")]
    empty = [("escalation", "escalate.conformal_set_empty")]
    on_label = _scored(handed_off=True, unsafe=(), handoffs=approval)
    off_label = _scored(handed_off=True, unsafe=(), handoffs=approval, expected="escalate")
    assert (aw.outcome(on_label).kind, aw.outcome(on_label).wrong) == ("review", False)
    assert aw.outcome(off_label).wrong is True
    assert aw.outcome(_scored(handed_off=True, unsafe=(), handoffs=empty)).kind == "other"


def test_the_degraded_prompt_drops_only_the_portuguese_example(tmp_path: Path) -> None:
    path = aw.degraded_prompt(PROMPT, tmp_path)
    base = yaml.safe_load(PROMPT.read_text(encoding="utf-8"))
    degraded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert degraded["version"] == f"{base['version']}-no-pt"
    assert degraded["system"] == base["system"]
    assert [e for e in base["examples"] if not e["derived_from"].endswith("-pt-br")] == degraded[
        "examples"
    ]
    assert len(degraded["examples"]) == len(base["examples"]) - 1


def test_the_section_says_simulado_and_the_criterion_of_ca4() -> None:
    md = "\n".join(aw.section(PARAMS, {}, {}, {}))
    assert "## Autonomy watch [simulado]" in md
    assert "fixed before this simulation ran" in md
    assert "The degraded run is not recorded yet." in md

"""The identification harness: fitting, base-case scores, cross-fit and the report (TRZ-15)."""

import json
import math
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.domain.identification import COMPONENTS, Candidate, Params, conformal_quantile
from app.schemas.comprehension import Comprehension
from pipeline.identification_eval import (
    Outcome,
    Prepared,
    base_scores,
    candidates_of,
    choose_threshold,
    cross_fit,
    ece,
    fit,
    golden_minimum,
    in_population,
    mean_nll,
    nonconformity,
    outcome,
    per_variant,
    prepare,
    probabilities,
    reliability,
    reliability_chart,
    threshold_curve,
    top_total,
    weight_grid,
)
from tests.unit.test_comprehension_eval import NOW, _case

TS = datetime(2026, 6, 16, 12, 0)


def _cand(tid: str, **extra: Any) -> Candidate:
    data: dict[str, Any] = {
        "transaction_id": tid,
        "timestamp": TS,
        "amount": 1790.0,
        "currency": "MXN",
        "channel": "POS",
        "merchant_name": "MercaYa",
        "transaction_type": "Purchase",
        "status": "Approved",
    }
    data.update(extra)
    return Candidate(**data)


def _clues(merchant: str | None = "MercaYa") -> Comprehension:
    data: dict[str, Any] = {"intent": "unrecognized_charge", "language": "es-MX"}
    if merchant:
        data["merchant_hint"] = {"value": merchant, "evidence": "x"}
    return Comprehension.model_validate(data)


def _world(bases: int = 12) -> tuple[list, dict[str, list[Candidate]]]:
    """Base cases whose true charge differs from a decoy only by the merchant."""
    cases, by_customer = [], {}
    for b in range(bases):
        customer = f"C{b}"
        by_customer[customer] = [
            _cand(f"T{b}"),
            _cand(f"D{b}", merchant_name="Farmacia Sol", timestamp=TS - timedelta(days=2)),
        ]
        for variant in ("es-MX", "pt-BR"):
            cases.append(
                _case(
                    case_id=f"b{b}-{variant}",
                    base_id=f"b{b}",
                    customer_id=customer,
                    variant=variant,
                    language="pt" if variant == "pt-BR" else "es",
                    truth={**_case().truth.model_dump(), "transaction_id": f"T{b}"},
                )
            )
    return cases, by_customer


def _prepared(bases: int = 12, drop_merchant: set[str] | None = None) -> list[Prepared]:
    cases, by_customer = _world(bases)
    drop = drop_merchant or set()
    readings = {c.case_id: _clues(None if c.case_id in drop else "MercaYa") for c in cases}
    return prepare(cases, readings, by_customer, {})


def test_population_is_the_customers_own_charge_disputes() -> None:
    assert in_population(_case())
    assert not in_population(_case(category="no_match"))
    assert not in_population(_case(category="other_customer"))
    assert not in_population(_case(intent="out_of_scope"))


def test_candidates_apply_the_window_and_add_the_staged_twin() -> None:
    twin = {
        "transaction_id": "T1-DUP",
        "transaction_date": (TS + timedelta(minutes=5)).isoformat(),
        "transaction_type": "Purchase",
        "transaction_status": "Pending",
        "channel": "POS",
        "amount": 1790.0,
        "currency": "MXN",
        "merchant_name": "MercaYa",
    }
    case = _case(scenario={"fixture_rows": [twin]})
    by_customer = {"C1": [_cand("T1"), _cand("OLD", timestamp=NOW - timedelta(days=121))]}
    assert [c.transaction_id for c in candidates_of(case, by_customer)] == ["T1", "T1-DUP"]


def test_weight_grid_sums_to_one() -> None:
    grid = weight_grid(0.1)
    assert len(grid) == 1001
    assert all(sum(w.values()) == pytest.approx(1.0) for w in grid)
    assert set(grid[0]) == set(COMPONENTS)


def test_golden_search_finds_the_minimum() -> None:
    found = golden_minimum(lambda t: (math.log(t) - math.log(0.5)) ** 2, 0.01, 10.0)
    assert found == pytest.approx(0.5, rel=1e-3)


def test_fit_puts_weight_on_the_component_that_separates_and_repeats_exactly() -> None:
    preps = _prepared()
    weights, temperature, nll = fit(preps, step=0.5)
    assert weights["merchant"] == max(weights.values())
    assert nll == pytest.approx(mean_nll(preps, weights, temperature))
    assert fit(preps, step=0.5) == (weights, temperature, nll)


def test_a_base_case_scores_the_largest_nonconformity_of_its_variants() -> None:
    # The Portuguese variant of b0 lost its merchant clue, so it cannot tell the charges apart.
    preps = _prepared(drop_merchant={"b0-pt-BR"})
    weights = {**dict.fromkeys(COMPONENTS, 0.0), "merchant": 1.0}
    scores = base_scores(preps, weights, 0.1)
    by_case = {p.case.case_id: nonconformity(p, weights, 0.1) for p in preps}
    assert scores["b0"] == by_case["b0-pt-BR"] == pytest.approx(0.5)
    assert by_case["b0-es-MX"] < 0.01
    assert scores["b1"] == max(by_case["b1-es-MX"], by_case["b1-pt-BR"])


def test_a_true_charge_outside_the_candidates_is_a_miss() -> None:
    prep = _prepared(1)[0]
    lost = replace(prep, true_index=None)
    params = Params("v", "rules", dict.fromkeys(COMPONENTS, 0.2), 1.0, 0.9)
    assert nonconformity(lost, dict(params.weights), 1.0) == math.inf
    missing = replace(
        prep,
        case=_case(truth={**prep.case.truth.model_dump(), "transaction_id": "GONE"}),
    )
    out = outcome(missing, params)
    assert out.true_missing and not out.covered and out.brier > 1.0


def test_outcomes_use_the_service_code_with_the_fitted_probabilities() -> None:
    prep = _prepared(1)[0]
    weights = {**dict.fromkeys(COMPONENTS, 0.0), "merchant": 1.0}
    params = Params("v", "rules", weights, 0.2, 0.5)
    out = outcome(prep, params)
    probs = probabilities(prep, weights, 0.2)
    assert out.confidence == pytest.approx(max(probs))
    assert out.covered and out.size == sum(1 - p <= 0.5 for p in probs)


def test_cross_fit_is_reproducible_and_holds_out_half_the_bases() -> None:
    preps = _prepared(bases=20, drop_merchant={"b3-es-MX", "b7-pt-BR"})
    weights = {**dict.fromkeys(COMPONENTS, 0.0), "merchant": 1.0}
    scores = base_scores(preps, weights, 0.2)
    params = Params("v", "rules", weights, 0.2, conformal_quantile(scores.values(), 0.05))
    first = cross_fit(preps, params, seed=42, splits=5)
    assert first == cross_fit(preps, params, seed=42, splits=5)
    assert all(r["bases"] == 10 for r in first)


def test_per_variant_quantiles_are_reported_next_to_the_largest_of_four() -> None:
    preps = _prepared(bases=20, drop_merchant={"b3-pt-BR"})
    weights = {**dict.fromkeys(COMPONENTS, 0.0), "merchant": 1.0}
    qhat = conformal_quantile(base_scores(preps, weights, 0.2).values(), 0.05)
    result = per_variant(preps, Params("v", "rules", weights, 0.2, qhat))
    assert set(result) == {"es-MX", "pt-BR"}
    assert result["pt-BR"]["own_qhat"] <= qhat and result["es-MX"]["own_qhat"] <= qhat


def _out(confidence: float, correct: bool) -> Outcome:
    return Outcome(_case(), 1, correct, "identified", 0.0, confidence, correct, False, False)


def test_reliability_bins_and_ece_by_hand() -> None:
    outs = [_out(0.95, True), _out(0.95, True), _out(0.45, True), _out(0.45, False)]
    rows = reliability(outs)
    assert [(r["low"], r["n"], r["accuracy"]) for r in rows] == [(0.4, 2, 0.5), (0.9, 2, 1.0)]
    # Bin 0.4: |0.45 - 0.5| = 0.05; bin 0.9: |0.95 - 1.0| = 0.05; weighted mean 0.05.
    assert ece(outs) == pytest.approx(0.05)


def test_reliability_chart_draws_accuracy_bars_against_the_confidence_line() -> None:
    outs = [_out(0.95, True), _out(0.95, True), _out(0.45, True), _out(0.45, False)]
    chart = reliability_chart("rules, all languages", reliability(outs))
    assert chart[0] == "```mermaid" and chart[-1] == "```"
    assert '    x-axis ["0.4-0.5 (n=2)", "0.9-1.0 (n=2)"]' in chart
    assert "    bar [0.500, 1.000]" in chart
    assert "    line [0.450, 0.950]" in chart


def test_the_committed_config_names_its_splits() -> None:
    config = yaml.safe_load(Path("config/identification.yaml").read_text())
    manifest = json.loads(Path("eval/splits/manifest.json").read_text())
    assert config["fitted_on"]["dev_sha256"] == manifest["splits"]["dev"]["sha256"]
    assert config["fitted_on"]["calibration_sha256"] == manifest["splits"]["calibration"]["sha256"]


def test_threshold_accepts_the_fewest_fake_charges_within_the_real_rejection_cap() -> None:
    real = [0.2, 0.1, 0.0, -0.1, -0.9] + [0.2] * 15
    fake = [-1.0, -0.8, -0.5]
    # Rejecting one real charge of 20 (5%) is allowed: tau = 0.0 accepts no fake charge but
    # rejects two real ones; tau = -0.1 rejects only -0.9 and accepts no fake charge either.
    assert choose_threshold(real, fake, 0.05) == pytest.approx(-0.1)
    # Without room to reject a real charge, the best is to accept only the fakes above -0.9.
    assert choose_threshold(real, fake, 0.0) == pytest.approx(-0.9)
    # When nothing separates them, rejecting nothing is the lowest best threshold.
    assert choose_threshold([0.0], [], 0.05) == -math.inf


def test_threshold_curve_counts_both_errors() -> None:
    rows = threshold_curve([-1.0, 0.0], [0.5, -0.5], [-0.7, 0.1], [0.3])
    assert rows == [
        {"tau": -1.0, "dev_real_rejected": 0, "fake_accepted": 2, "cal_real_rejected": 0},
        {"tau": 0.0, "dev_real_rejected": 1, "fake_accepted": 1, "cal_real_rejected": 0},
    ]


def test_rejected_variants_do_not_enter_the_quantile() -> None:
    preps = _prepared(drop_merchant={"b0-pt-BR"})
    weights = {**dict.fromkeys(COMPONENTS, 0.0), "merchant": 1.0}
    # b0-pt-BR has no merchant clue, so its best total is 0 while the others reach 1.
    assert top_total(preps[1], weights) == 0.0
    screened = base_scores(preps, weights, 0.1, reject_below=0.5)
    assert screened["b0"] < 0.01

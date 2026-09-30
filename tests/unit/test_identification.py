"""Score, probabilities, conformal set and decision of identification (TRZ-15)."""

import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.domain.identification import (
    COMPONENTS,
    Candidate,
    Params,
    components,
    conformal_quantile,
    decide,
    identify,
    identify_from_button,
    is_disputable,
    load_params,
    merchant_similarity,
    softmax,
)
from app.schemas.comprehension import Comprehension

NOW = datetime(2026, 6, 17, 23, 59)
DAY = date(2026, 6, 10)
RATES = {(DAY, "MXN", "USD"): 0.05}
EVEN = dict.fromkeys(COMPONENTS, 0.2)


def _cand(tid: str = "T1", **extra: Any) -> Candidate:
    data: dict[str, Any] = {
        "transaction_id": tid,
        "timestamp": datetime(2026, 6, 10, 12, 0),
        "amount": 100.0,
        "currency": "USD",
        "channel": "POS",
        "merchant_name": "MercaYa Centro",
        "transaction_type": "Purchase",
        "status": "Approved",
    }
    data.update(extra)
    return Candidate(**data)


def _clues(**fields: Any) -> Comprehension:
    return Comprehension.model_validate(
        {"intent": "unrecognized_charge", "language": "es-MX", **fields}
    )


def _amount(value: float, currency: str | None = None, approximate: bool = False) -> dict:
    return {"value": value, "currency": currency, "approximate": approximate, "evidence": "x"}


def _date(low: int, high: int) -> dict:
    return {
        "expression": "x",
        "resolved_from": NOW.date(),
        "window_days": (low, high),
        "evidence": "x",
    }


# ---- CA5: the quantile, worked by hand ---------------------------------------------------


def test_quantile_level_worked_by_hand() -> None:
    # n = 9, alpha = 0.2: ceil((9 + 1) * 0.8) = 8, so q-hat is the 8th smallest score.
    scores = [0.9, 0.1, 0.5, 0.3, 0.7, 0.2, 0.8, 0.4, 0.6]
    assert conformal_quantile(scores, 0.2) == 0.8
    # n = 100, alpha = 0.05: ceil(101 * 0.95) = ceil(95.95) = 96, the 96th smallest.
    assert conformal_quantile([i / 100 for i in range(100)], 0.05) == 0.95
    # n = 19, alpha = 0.05: ceil(20 * 0.95) = 19 exactly, so float error must not make it 20.
    assert conformal_quantile([i / 19 for i in range(19)], 0.05) == 18 / 19


def test_too_few_scores_keep_every_candidate() -> None:
    # n = 10, alpha = 0.05: ceil(11 * 0.95) = 11 > 10.
    assert conformal_quantile([0.1] * 10, 0.05) == math.inf


def test_a_missing_true_transaction_is_an_infinite_score() -> None:
    # Five units, alpha 0.2: ceil(6 * 0.8) = 5, the largest; a miss there makes q-hat infinite.
    assert conformal_quantile([0.1, 0.2, 0.3, 0.4, math.inf], 0.2) == math.inf


# ---- CA1: candidates ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({}, True),
        ({"transaction_type": "Withdrawal", "merchant_name": None}, True),
        ({"status": "Pending"}, True),
        ({"status": "Declined"}, False),
        ({"transaction_type": "Transfer"}, False),
        ({"timestamp": NOW - timedelta(days=120)}, True),
        ({"timestamp": NOW - timedelta(days=120, seconds=1)}, False),
        ({"timestamp": NOW + timedelta(minutes=1)}, False),
    ],
)
def test_disputable_candidates(extra: dict, expected: bool) -> None:
    assert is_disputable(_cand(**extra), NOW, 120) is expected


# ---- CA2: components ---------------------------------------------------------------------


def test_every_component_is_returned_and_absent_clues_add_nothing() -> None:
    values, not_convertible = components(_clues(), _cand(), "MXN", RATES)
    assert values == dict.fromkeys(COMPONENTS, 0.0) and not not_convertible


def test_amount_closeness_is_on_a_log_scale_with_a_tolerance_per_form() -> None:
    exact, _ = components(_clues(amount=_amount(100, "USD")), _cand(), "MXN", RATES)
    near, _ = components(_clues(amount=_amount(105, "USD")), _cand(), "MXN", RATES)
    hedged, _ = components(_clues(amount=_amount(105, "USD", True)), _cand(), "MXN", RATES)
    far, _ = components(_clues(amount=_amount(10_000, "USD")), _cand(), "MXN", RATES)

    assert exact["amount"] == 0.0
    assert near["amount"] == pytest.approx(-math.log(1.05) / 0.05)
    assert hedged["amount"] == pytest.approx(-math.log(1.05) / math.log(2))
    assert far["amount"] == -4.0
    assert exact["currency"] == 1.0


def test_an_amount_without_currency_may_be_in_the_local_currency() -> None:
    # 2,000 MXN at 0.05 is exactly the 100 USD registered.
    values, not_convertible = components(_clues(amount=_amount(2000)), _cand(), "MXN", RATES)
    assert values["amount"] == 0.0 and values["currency"] == 0.0 and not not_convertible


def test_an_amount_without_rate_is_flagged_not_convertible() -> None:
    values, not_convertible = components(
        _clues(amount=_amount(2000, "MXN")), _cand(timestamp=datetime(2026, 1, 1)), "MXN", {}
    )
    assert values["amount"] == 0.0 and not_convertible


def test_date_distance_to_the_window() -> None:
    # The candidate is 7 days back; the window 1 to 1 day back misses it by 6 days.
    inside, _ = components(_clues(date=_date(5, 9)), _cand(), "MXN", RATES)
    outside, _ = components(_clues(date=_date(1, 1)), _cand(), "MXN", RATES)
    assert inside["date"] == 0.0
    assert outside["date"] == pytest.approx(-2.0)


def test_merchant_part_misspelling_and_other_names() -> None:
    assert merchant_similarity("mercaya", "MercaYa Centro") == 1.0
    assert merchant_similarity("Centro", "MercaYa Centro") == 1.0
    assert 0.8 < merchant_similarity("MecraYa Centro", "MercaYa Centro") < 1.0
    assert merchant_similarity("Farmacia Sol", "MercaYa Centro") < 0.5
    values, _ = components(
        _clues(merchant_hint={"value": "MercaYa", "evidence": "x"}),
        _cand(merchant_name=None, transaction_type="Withdrawal"),
        "MXN",
        RATES,
    )
    assert values["merchant"] == 0.0


def test_channel_matches_or_contradicts() -> None:
    clue = _clues(channel_hint={"value": "ATM", "evidence": "x"})
    assert components(clue, _cand(channel="ATM"), "MXN", RATES)[0]["channel"] == 1.0
    assert components(clue, _cand(), "MXN", RATES)[0]["channel"] == -1.0


# ---- CA6: probabilities, set and decision -----------------------------------------------


def test_softmax_sums_to_one_and_temperature_spreads_it() -> None:
    sharp = softmax([1.0, 0.0], 0.1)
    flat = softmax([1.0, 0.0], 10.0)
    assert sum(sharp) == pytest.approx(1.0) and sum(flat) == pytest.approx(1.0)
    assert sharp[0] > flat[0] > 0.5
    assert softmax([], 1.0) == []


@pytest.mark.parametrize(
    ("size", "decision"),
    [
        (0, "not_found"),
        (1, "identified"),
        (2, "show_options"),
        (3, "show_options"),
        (4, "ask_for_detail"),
    ],
)
def test_decision_table(size: int, decision: str) -> None:
    assert decide(size) == decision


def test_the_set_holds_every_candidate_with_one_minus_p_at_most_qhat() -> None:
    clues = _clues(amount=_amount(100, "USD"))
    cands = [_cand("A"), _cand("B", amount=101.0), _cand("C", amount=300.0)]
    params = Params("v", "rules", {**dict.fromkeys(COMPONENTS, 0.0), "amount": 1.0}, 1.0, 0.8)

    result = identify(clues, cands, params, "MXN", RATES)

    probs = {s.candidate.transaction_id: s.probability for s in result.scored}
    assert [s.candidate.transaction_id for s in result.scored] == ["A", "B", "C"]
    assert set(result.conformal_set) == {t for t, p in probs.items() if 1 - p <= 0.8}
    assert result.decision == decide(len(result.conformal_set))
    assert result.scored[0].components["amount"] == 0.0
    assert result.scored[0].total == pytest.approx(0.0)


def test_one_clear_candidate_is_identified_and_no_candidates_are_not_found() -> None:
    params = Params("v", "rules", EVEN, 0.05, 0.8)
    clues = _clues(merchant_hint={"value": "MercaYa", "evidence": "x"})
    result = identify(
        clues, [_cand("A"), _cand("B", merchant_name="Farmacia Sol")], params, "MXN", RATES
    )
    assert result.conformal_set == ("A",) and result.decision == "identified"
    assert identify(clues, [], params, "MXN", RATES).decision == "not_found"


# ---- CA9: the button door ----------------------------------------------------------------


def test_the_button_checks_the_chosen_charge_is_a_candidate() -> None:
    cands = [_cand("A"), _cand("B")]
    assert identify_from_button(cands, "B").conformal_set == ("B",)
    assert identify_from_button(cands, "B").door == "button"
    assert identify_from_button(cands, "Z").decision == "not_found"


# ---- CA3, CA4: versioned parameters ------------------------------------------------------


def test_the_fitted_parameters_are_versioned_per_comprehension() -> None:
    for name in ("rules", "llm"):
        params = load_params(Path("config/identification.yaml"), name)
        assert params.version == "identification-3" and params.comprehension == name
        assert sum(params.weights.values()) == pytest.approx(1.0)
        assert params.temperature > 0 and 0 < params.qhat <= 1
        assert math.isfinite(params.reject_below)


@pytest.mark.parametrize("tolerance", [None, {"exact": 0.05, "approximate": 0.35}])
def test_parameters_fitted_with_another_amount_tolerance_are_refused(
    tmp_path: Path, tolerance: dict[str, float] | None
) -> None:
    config = yaml.safe_load(Path("config/identification.yaml").read_text(encoding="utf-8"))
    config.pop("amount_tolerance")
    if tolerance is not None:
        config["amount_tolerance"] = tolerance
    path = tmp_path / "identification.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match="amount tolerance"):
        load_params(path, "rules")


def test_a_best_candidate_below_the_threshold_empties_the_set() -> None:
    clues = _clues(merchant_hint={"value": "Farmacia Sol", "evidence": "x"}, date=_date(40, 45))
    cands = [_cand("A"), _cand("B", channel="ATM")]
    lenient = Params("v", "rules", EVEN, 0.05, 0.8, reject_below=-5.0)
    strict = Params("v", "rules", EVEN, 0.05, 0.8, reject_below=0.0)

    kept = identify(clues, cands, lenient, "MXN", RATES)
    rejected = identify(clues, cands, strict, "MXN", RATES)

    # The softmax alone still spreads confidence over two charges that do not fit.
    assert kept.conformal_set and not kept.rejected
    assert rejected.conformal_set == () and rejected.decision == "not_found" and rejected.rejected
    assert rejected.scored == kept.scored


# ---- an empty set has two causes (design 6.2, option B of the QA of TRZ-34) ---------------


def _many(n: int = 10) -> list[Candidate]:
    # Charges that the clues cannot tell apart: every one scores the same.
    return [_cand(f"T{i:02d}", merchant_name=f"Comercio {i}") for i in range(n)]


def test_an_empty_set_from_spread_confidence_asks_for_a_detail() -> None:
    # Only a date that fits them all: p = 1/10 each, 1 - p = 0.9 above q-hat, so the set is
    # empty although nothing was rejected. Too few clues is not "no such charge".
    params = Params("v", "rules", EVEN, 1.0, 0.5, reject_below=-5.0)
    result = identify(_clues(date=_date(0, 30)), _many(), params, "MXN", RATES)

    assert result.conformal_set == () and not result.rejected
    assert result.decision == "ask_for_detail"


def test_an_empty_set_from_the_rejection_threshold_is_still_not_found() -> None:
    params = Params("v", "rules", EVEN, 1.0, 0.5, reject_below=5.0)
    result = identify(_clues(date=_date(0, 30)), _many(), params, "MXN", RATES)

    assert result.rejected and result.decision == "not_found"


def test_no_candidates_or_one_are_decided_as_before() -> None:
    params = Params("v", "rules", EVEN, 1.0, 0.5, reject_below=-5.0)
    clues = _clues(date=_date(0, 30))
    assert identify(clues, [], params, "MXN", RATES).decision == "not_found"
    assert identify(clues, _many(1), params, "MXN", RATES).decision == "identified"

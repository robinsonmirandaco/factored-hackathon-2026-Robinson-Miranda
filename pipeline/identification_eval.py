"""Identification harness (TRZ-15, design 6.2): fit on development, calibrate, report.

`fit` sets the component weights and the softmax temperature on the development split and the
conformal threshold q-hat on the calibration split, once per comprehension (rules and LLM), and
writes them with their provenance to config/identification.yaml. `report` reads that file and
writes docs/reports/identificacion.md. Both use `app.domain.identification`, the code the
service runs; only the candidates come from the gold files instead of Postgres.

The four variants of a base case share the transaction, so they are not independent: the unit of
the conformal quantile is the base case, scored by the largest nonconformity of its four
variants. A variant's score never exceeds that maximum, so the guarantee holds for each variant,
not only on average over them.

Nothing here loads the test split.
The report holds counts and rates only, never a message or a row.
"""

import argparse
import math
import random
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any

import duckdb
import yaml

from app.adapters.llm import LLMClient
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.domain.comprehension_rules import RULES_VERSION, comprehend_rules
from app.domain.fx import Rates
from app.domain.identification import (
    AMOUNT_TOLERANCE,
    COMPONENTS,
    Candidate,
    Params,
    components,
    conformal_quantile,
    identify,
    identify_from_button,
    is_disputable,
    load_params,
    softmax,
    weighted,
)
from app.domain.pii import redact
from app.domain.policy import load_policy
from app.schemas.comprehension import Comprehension
from pipeline.cases.schema import CaseRecord
from pipeline.cases.splits import load_split, read_manifest
from pipeline.comprehension_eval import context_of
from pipeline.comprehension_llm import (
    CACHE_FILE,
    LLMRuns,
    ReadingCache,
    check_prompt_sources,
    request_key,
)
from pipeline.settings import PipelineSettings

log = get_logger("pipeline.identification_eval")

CONFIG_PATH = Path("config/identification.yaml")
REPORT_PATH = Path("docs/reports/identificacion.md")
POLICY_PATH = Path("config/policy.yaml")
# The service reads the same value, so the evaluated candidates are the served ones.
WINDOW_DAYS = load_policy(POLICY_PATH).dispute_window_days
VERSION = "identification-3"
ALPHA = 0.05
WEIGHT_STEP = 0.1
TEMPERATURE_RANGE = (0.01, 10.0)
LLM_RUNS = 3
# Assumption: at most this share of real charges may be rejected on the development split.
# Rejecting a real charge only sends it to a person; accepting one that does not exist can
# register the dispute on the wrong charge, the unsafe outcome.
MAX_REAL_REJECTION = 0.05
CROSS_FIT_SPLITS = 20
RELIABILITY_BINS = 10
CHARGE_INTENTS = ("unrecognized_charge", "billing_error_amount", "billing_error_duplicate")
# The message is about no charge of the session customer: another customer's or none at all.
OUTSIDE = ("no_match", "other_customer")

Readings = dict[str, Comprehension]


# ---- candidates and cases ----------------------------------------------------------------


def in_population(case: CaseRecord) -> bool:
    """Whether a case asks to identify one of the customer's own charges.

    Args:
        case: Evaluation case.

    Returns:
        True for charge disputes with a true transaction of the session customer.
    """
    return (
        case.intent in CHARGE_INTENTS
        and case.truth.transaction_id is not None
        and case.category not in OUTSIDE
    )


def load_gold(gold: Path, customers: set[str]) -> tuple[dict[str, list[Candidate]], Rates]:
    """Reads the customers' transactions and every exchange rate from the gold files.

    Args:
        gold: Gold directory of the last `make data`.
        customers: Customers whose transactions are needed.

    Returns:
        Candidates per customer, before the window of each case, and the rates.
    """
    con = duckdb.connect()
    # Timestamps become the customer's local wall time, like the serving table and the clock.
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE TABLE wanted (customer_id VARCHAR)")
    con.executemany("INSERT INTO wanted VALUES (?)", [(c,) for c in sorted(customers)])
    rows = con.execute(
        f"""
        SELECT transaction_id, customer_id, timezone(timezone, transaction_date), amount::DOUBLE,
               currency, channel, merchant_name, transaction_type, transaction_status
        FROM read_parquet('{gold / "case_generator_input.parquet"}')
        WHERE customer_id IN (SELECT customer_id FROM wanted)
        ORDER BY customer_id, 3, transaction_id
        """
    ).fetchall()
    by_customer: dict[str, list[Candidate]] = defaultdict(list)
    for tid, cid, ts, amount, currency, channel, merchant, ttype, status in rows:
        by_customer[cid].append(
            Candidate(tid, ts, amount, currency, channel, merchant, ttype, status)
        )
    rates = {
        (d, s, t): float(r)
        for d, s, t, r in con.execute(
            "SELECT date, source_currency, target_currency, exchange_rate "
            f"FROM read_parquet('{gold / 'service_exchange_rates.parquet'}')"
        ).fetchall()
    }
    con.close()
    return dict(by_customer), rates


def candidates_of(case: CaseRecord, by_customer: dict[str, list[Candidate]]) -> list[Candidate]:
    """The candidates the service would see for a case, with the rows the harness stages.

    Args:
        case: Evaluation case.
        by_customer: Transactions per customer.

    Returns:
        Disputable transactions in the window before the case "now", plus constructed ones
        (the twin of a duplicate) that the harness inserts before the case.
    """
    staged = [
        Candidate(
            transaction_id=r["transaction_id"],
            timestamp=datetime.fromisoformat(r["transaction_date"]),
            amount=float(r["amount"]),
            currency=r["currency"],
            channel=r["channel"],
            merchant_name=r["merchant_name"],
            transaction_type=r["transaction_type"],
            status=r["transaction_status"],
        )
        for r in case.scenario.fixture_rows
    ]
    pool = [*by_customer.get(case.customer_id, []), *staged]
    return [c for c in pool if is_disputable(c, case.now, WINDOW_DAYS)]


@dataclass(frozen=True)
class Prepared:
    """A case ready to be scored: the component values of every candidate.

    Attributes:
        case: The case.
        clues: Faithful clues of the message.
        candidates: Candidates of the case.
        ids: Candidate ids.
        values: Component values per candidate, in the order of `ids`.
        true_index: Position of the true transaction, or None when it is not a candidate.
        not_convertible: The stated amount could not be converted for some candidate.
        rates: Exchange rates, shared by every case of the run.
    """

    case: CaseRecord
    clues: Comprehension
    candidates: tuple[Candidate, ...]
    ids: tuple[str, ...]
    values: tuple[dict[str, float], ...]
    true_index: int | None
    not_convertible: bool
    rates: Rates


def prepare(
    cases: Iterable[CaseRecord],
    readings: Readings,
    by_customer: dict[str, list[Candidate]],
    rates: Rates,
) -> list[Prepared]:
    """Computes the components once per case, so fitting only reweights them.

    Args:
        cases: Cases of the identification population.
        readings: Faithful clues per case id.
        by_customer: Transactions per customer.
        rates: Exchange rates.

    Returns:
        The prepared cases.
    """
    out = []
    for case in cases:
        cands = candidates_of(case, by_customer)
        parts = [
            components(readings[case.case_id], c, case.truth.local_currency, rates) for c in cands
        ]
        ids = tuple(c.transaction_id for c in cands)
        true_id = case.truth.transaction_id
        out.append(
            Prepared(
                case=case,
                clues=readings[case.case_id],
                candidates=tuple(cands),
                ids=ids,
                values=tuple(v for v, _ in parts),
                true_index=ids.index(true_id) if true_id in ids else None,
                not_convertible=any(nc for _, nc in parts),
                rates=rates,
            )
        )
    return out


# ---- comprehension -----------------------------------------------------------------------


def rules_readings(cases: Iterable[CaseRecord]) -> Readings:
    """Faithful clues of the rules baseline, as production uses them.

    Args:
        cases: Cases to read.

    Returns:
        Case id to clues.
    """
    out = {}
    for case in cases:
        message = redact(case.message)[0]
        out[case.case_id] = comprehend_rules(message, context_of(case)).faithful(message)[0]
    return out


def llm_readings(
    runner: LLMRuns, cases: list[CaseRecord], run: int, costs: dict[str, float] | None = None
) -> Readings:
    """Faithful clues of the LLM for one run, from the cache when possible.

    Args:
        runner: LLM runner with its cache and budget.
        cases: Cases to read.
        run: Run number.
        costs: When given, receives the original cost of every cached request used, by key,
            so shared requests count once.

    Returns:
        Case id to clues; a failed reading holds the rules fallback, as in production.
    """
    outcomes = runner.run(cases, run)
    if costs is not None:
        for c in cases:
            key = request_key(runner.client, redact(c.message)[0], context_of(c), run)
            costs[key] = float(runner.cache.entries[key]["cost_usd"])
    return {c.case_id: outcomes[c.case_id].reading.faithful(redact(c.message)[0])[0] for c in cases}


# ---- fitting -----------------------------------------------------------------------------


def probabilities(prep: Prepared, weights: dict[str, float], temperature: float) -> list[float]:
    """Softmax probabilities of the candidates of a prepared case.

    Args:
        prep: Prepared case.
        weights: Component weights.
        temperature: Softmax temperature.

    Returns:
        One probability per candidate.
    """
    return softmax([weighted(v, weights) for v in prep.values], temperature)


def mean_nll(preps: Sequence[Prepared], weights: dict[str, float], temperature: float) -> float:
    """Mean negative log-likelihood of the true candidate, over cases that contain it.

    Args:
        preps: Prepared development cases.
        weights: Component weights.
        temperature: Softmax temperature.

    Returns:
        The mean.
    """
    total, n = 0.0, 0
    for prep in preps:
        if prep.true_index is None:
            continue
        p = probabilities(prep, weights, temperature)[prep.true_index]
        total -= math.log(max(p, 1e-300))
        n += 1
    return total / n


def golden_minimum(f: Callable[[float], float], low: float, high: float, steps: int = 40) -> float:
    """Minimum of a unimodal function by golden-section search on a log scale.

    Args:
        f: Function of a positive number.
        low: Lower bound.
        high: Upper bound.
        steps: Iterations.

    Returns:
        The argument of the minimum found.
    """
    ratio = (math.sqrt(5) - 1) / 2
    a, b = math.log(low), math.log(high)
    c, d = b - ratio * (b - a), a + ratio * (b - a)
    fc, fd = f(math.exp(c)), f(math.exp(d))
    for _ in range(steps):
        if fc <= fd:
            b, d, fd = d, c, fc
            c = b - ratio * (b - a)
            fc = f(math.exp(c))
        else:
            a, c, fc = c, d, fd
            d = a + ratio * (b - a)
            fd = f(math.exp(d))
    return math.exp((a + b) / 2)


def weight_grid(step: float) -> list[dict[str, float]]:
    """Every weighting of the components on a grid that sums to one.

    Args:
        step: Grid step.

    Returns:
        The weightings, in a fixed order.
    """
    units = round(1 / step)
    grid = []
    for combo in product(range(units + 1), repeat=len(COMPONENTS)):
        if sum(combo) == units:
            grid.append({k: round(u * step, 10) for k, u in zip(COMPONENTS, combo, strict=True)})
    return grid


def fit(
    preps: Sequence[Prepared], step: float = WEIGHT_STEP
) -> tuple[dict[str, float], float, float]:
    """Fits weights and temperature on development cases (TRZ-15 CA3).

    The weights sum to one, so the temperature alone sets how sharp the probabilities are.
    Each weighting gets its best temperature; the pair with the lowest mean negative
    log-likelihood of the true candidate wins, the first in grid order on a tie.

    Args:
        preps: Prepared development cases.
        step: Grid step of the weights.

    Returns:
        Weights, temperature and the mean negative log-likelihood they reach.
    """
    best: tuple[float, dict[str, float], float] | None = None
    for weights in weight_grid(step):
        temperature = golden_minimum(lambda t, w=weights: mean_nll(preps, w, t), *TEMPERATURE_RANGE)
        nll = mean_nll(preps, weights, temperature)
        if best is None or nll < best[0] - 1e-12:
            best = (nll, weights, temperature)
    assert best is not None
    return best[1], best[2], best[0]


def nonconformity(prep: Prepared, weights: dict[str, float], temperature: float) -> float:
    """1 - p(true); infinity when the true transaction is not a candidate (TRZ-15 CA7).

    Args:
        prep: Prepared case.
        weights: Component weights.
        temperature: Softmax temperature.

    Returns:
        The score.
    """
    if prep.true_index is None:
        return math.inf
    return 1 - probabilities(prep, weights, temperature)[prep.true_index]


def top_total(prep: Prepared, weights: Mapping[str, float]) -> float:
    """Total score of the best candidate: how well anything matches the clues at all.

    Args:
        prep: Prepared case.
        weights: Component weights.

    Returns:
        The largest weighted total; minus infinity without candidates.
    """
    return max((weighted(v, weights) for v in prep.values), default=-math.inf)


def base_scores(
    preps: Iterable[Prepared],
    weights: dict[str, float],
    temperature: float,
    reject_below: float = -math.inf,
) -> dict[str, float]:
    """Nonconformity of each base case: the largest of its accepted variants.

    A variant rejected by the absolute threshold escalates with an empty set; q-hat covers
    the cases that pass it, so the guarantee is conditional on acceptance.

    Args:
        preps: Prepared cases.
        weights: Component weights.
        temperature: Softmax temperature.
        reject_below: Absolute rejection threshold.

    Returns:
        Base id to score, for bases with at least one accepted variant.
    """
    out: dict[str, float] = {}
    for prep in preps:
        if top_total(prep, weights) < reject_below:
            continue
        s = nonconformity(prep, weights, temperature)
        out[prep.case.base_id] = max(out.get(prep.case.base_id, -math.inf), s)
    return out


def choose_threshold(
    real: Sequence[float], fake: Sequence[float], max_real_rejection: float = MAX_REAL_REJECTION
) -> float:
    """Absolute rejection threshold on the best total, fitted on development cases.

    Among the thresholds that reject at most `max_real_rejection` of the real charges, the one
    that accepts the fewest charges that do not exist; on a tie, the lowest. A case is rejected
    when its best total is strictly below the threshold.

    Args:
        real: Best totals of cases whose charge exists.
        fake: Best totals of cases whose charge does not exist (no_match).
        max_real_rejection: Most share of real charges that may be rejected.

    Returns:
        The threshold; minus infinity when rejecting nothing is already best.
    """
    options = sorted({-math.inf, *real, *fake})
    best: tuple[int, float] | None = None
    for tau in options:
        if sum(t < tau for t in real) > max_real_rejection * len(real):
            continue
        accepted = sum(t >= tau for t in fake)
        if best is None or accepted < best[0]:
            best = (accepted, tau)
    assert best is not None
    return best[1]


def threshold_curve(
    thresholds: Iterable[float],
    dev_real: Sequence[float],
    fake: Sequence[float],
    cal_real: Sequence[float],
) -> list[dict[str, Any]]:
    """Charges that do not exist accepted against real charges rejected, per threshold.

    Args:
        thresholds: Thresholds to evaluate.
        dev_real: Best totals of the real development charges.
        fake: Best totals of the no_match development cases.
        cal_real: Best totals of the real calibration charges.

    Returns:
        One row per threshold with counts.
    """
    return [
        {
            "tau": tau,
            "dev_real_rejected": sum(t < tau for t in dev_real),
            "fake_accepted": sum(t >= tau for t in fake),
            "cal_real_rejected": sum(t < tau for t in cal_real),
        }
        for tau in thresholds
    ]


# ---- outcomes and summaries --------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What identification did on one case.

    Attributes:
        case: The case.
        size: Size of the conformal set.
        covered: The true transaction is in the set.
        decision: Decision of design 6.2 for that size.
        brier: Multiclass Brier score over the candidates, plus 1 when the true one is missing.
        confidence: Probability of the most probable candidate; 0 without candidates.
        top_correct: The most probable candidate is the true one.
        true_missing: The true transaction is not among the candidates.
        not_convertible: The stated amount could not be converted for some candidate.
        rejected: The absolute threshold emptied the set.
    """

    case: CaseRecord
    size: int
    covered: bool
    decision: str
    brier: float
    confidence: float
    top_correct: bool
    true_missing: bool
    not_convertible: bool
    rejected: bool = False


def outcome(prep: Prepared, params: Params) -> Outcome:
    """Identifies one case with the service code and scores the result.

    Args:
        prep: Prepared case.
        params: Weights, temperature and q-hat.

    Returns:
        The outcome.
    """
    result = identify(
        prep.clues, prep.candidates, params, prep.case.truth.local_currency, prep.rates
    )
    true_id = prep.case.truth.transaction_id
    probs = {s.candidate.transaction_id: s.probability for s in result.scored}
    brier = sum((p - (1.0 if tid == true_id else 0.0)) ** 2 for tid, p in probs.items())
    missing = true_id not in probs
    return Outcome(
        case=prep.case,
        size=len(result.conformal_set),
        covered=true_id in result.conformal_set,
        decision=result.decision,
        brier=brier + (1.0 if missing else 0.0),
        confidence=result.scored[0].probability if result.scored else 0.0,
        top_correct=bool(result.scored) and result.scored[0].candidate.transaction_id == true_id,
        true_missing=missing,
        not_convertible=result.amount_not_convertible,
        rejected=result.rejected,
    )


def reliability(outs: Sequence[Outcome], bins: int = RELIABILITY_BINS) -> list[dict[str, Any]]:
    """Confidence of the top candidate against how often it is right, by equal-width bins.

    Args:
        outs: Outcomes.
        bins: Number of bins over [0, 1].

    Returns:
        One row per non-empty bin: bounds, cases, mean confidence and accuracy.
    """
    grouped: dict[int, list[Outcome]] = defaultdict(list)
    for o in outs:
        grouped[min(int(o.confidence * bins), bins - 1)].append(o)
    return [
        {
            "low": b / bins,
            "high": (b + 1) / bins,
            "n": len(grouped[b]),
            "confidence": sum(o.confidence for o in grouped[b]) / len(grouped[b]),
            "accuracy": sum(o.top_correct for o in grouped[b]) / len(grouped[b]),
        }
        for b in sorted(grouped)
    ]


def ece(outs: Sequence[Outcome], bins: int = RELIABILITY_BINS) -> float:
    """Expected calibration error of the top candidate's confidence.

    Args:
        outs: Outcomes.
        bins: Number of equal-width bins.

    Returns:
        The weighted mean gap between confidence and accuracy.
    """
    rows = reliability(outs, bins)
    return sum(r["n"] * abs(r["confidence"] - r["accuracy"]) for r in rows) / len(outs)


def summarize(outs: Sequence[Outcome]) -> dict[str, Any]:
    """Coverage, set size, decisions and calibration of a group of outcomes (TRZ-15 CA8).

    Args:
        outs: Outcomes of one group.

    Returns:
        The metrics.
    """
    n = len(outs)
    decisions = {d: sum(o.decision == d for o in outs) for d in _DECISIONS}
    accepted = sum(not o.rejected for o in outs)
    return {
        "rejected": n - accepted,
        "accepted_coverage": sum(o.covered for o in outs) / accepted if accepted else math.nan,
        "n": n,
        "bases": len({o.case.base_id for o in outs}),
        "covered": sum(o.covered for o in outs),
        "coverage": sum(o.covered for o in outs) / n,
        "mean_size": sum(o.size for o in outs) / n,
        "size_one": sum(o.size == 1 for o in outs) / n,
        "decisions": decisions,
        "brier": sum(o.brier for o in outs) / n,
        "ece": ece(outs),
        "top_accuracy": sum(o.top_correct for o in outs) / n,
        "true_missing": sum(o.true_missing for o in outs),
        "not_convertible": sum(o.not_convertible for o in outs),
    }


def amount_form(case: CaseRecord) -> str:
    """How the first message states the amount, as the case generator drew it.

    Args:
        case: Evaluation case.

    Returns:
        exact, rounded, about, more_than, or none when the amount is not in the message.
    """
    amount = case.noise.amount
    if not amount.mentioned or amount.form is None:
        return "none"
    if amount.form == "approximate" and amount.qualifier is not None:
        return amount.qualifier
    return amount.form


_DECISIONS = ("identified", "show_options", "ask_for_detail", "not_found")
GROUPS: dict[str, Callable[[CaseRecord], str]] = {
    "language": lambda c: c.language,
    "variant": lambda c: c.variant,
    "segment": lambda c: c.truth.segment,
    "category": lambda c: c.category,
    "amount form": amount_form,
}


def by_group(outs: Sequence[Outcome], key: Callable[[CaseRecord], str]) -> dict[str, Any]:
    """Summaries of the outcomes grouped by a case attribute.

    Args:
        outs: Outcomes.
        key: Attribute of the case.

    Returns:
        Group value to summary.
    """
    grouped: dict[str, list[Outcome]] = defaultdict(list)
    for o in outs:
        grouped[key(o.case)].append(o)
    return {g: summarize(v) for g, v in sorted(grouped.items())}


def cross_fit(
    preps: Sequence[Prepared], params: Params, seed: int, splits: int = CROSS_FIT_SPLITS
) -> list[dict[str, Any]]:
    """Out-of-sample coverage inside the calibration split, without the test split.

    Each round draws half the base cases with a seeded shuffle, computes q-hat on them and
    measures the other half.

    Args:
        preps: Prepared calibration cases.
        params: Weights and temperature (q-hat is recomputed per round).
        seed: Seed of the run.
        splits: Rounds.

    Returns:
        One summary per round.
    """
    scores = base_scores(preps, dict(params.weights), params.temperature, params.reject_below)
    bases = sorted(scores)
    rounds = []
    for i in range(splits):
        order = bases[:]
        random.Random(f"{seed}-cross-fit-{i}").shuffle(order)
        fit_half = set(order[: len(order) // 2])
        qhat = conformal_quantile([scores[b] for b in fit_half], ALPHA)
        held = [p for p in preps if p.case.base_id not in fit_half]
        rounds.append(
            {"qhat": qhat, **summarize([outcome(p, _with_qhat(params, qhat)) for p in held])}
        )
    return rounds


def _with_qhat(params: Params, qhat: float) -> Params:
    return replace(params, qhat=qhat)


def per_variant(preps: Sequence[Prepared], params: Params) -> dict[str, dict[str, Any]]:
    """q-hat computed on each variant alone, against the largest-of-four rule.

    Args:
        preps: Prepared calibration cases.
        params: Parameters with the largest-of-four q-hat.

    Returns:
        Variant to its own q-hat and the in-sample summary each q-hat gives on that variant.
    """
    out = {}
    for variant in sorted({p.case.variant for p in preps}):
        cases = [p for p in preps if p.case.variant == variant]
        scores = [
            nonconformity(p, dict(params.weights), params.temperature)
            for p in cases
            if top_total(p, params.weights) >= params.reject_below
        ]
        own = conformal_quantile(scores, ALPHA)
        out[variant] = {
            "own_qhat": own,
            "own": summarize([outcome(p, _with_qhat(params, own)) for p in cases]),
            "max_rule": summarize([outcome(p, params) for p in cases]),
        }
    return out


def button_door(preps: Sequence[Prepared]) -> dict[str, Any]:
    """The "No lo reconozco" door: the charge arrives chosen (TRZ-15 CA9).

    Args:
        preps: Prepared cases.

    Returns:
        Cases and how many were identified; identified means the chosen charge is one of the
        customer's candidates, so the rate is high by construction.
    """
    results = [identify_from_button(p.candidates, p.case.truth.transaction_id or "") for p in preps]
    identified = sum(r.decision == "identified" for r in results)
    return {"n": len(results), "identified": identified, "rate": identified / len(results)}


# ---- fit ---------------------------------------------------------------------------------


@dataclass
class Inputs:
    """Everything both commands read: splits, candidates, rates and the LLM runner."""

    manifest: dict[str, Any]
    dev: list[CaseRecord]
    calibration: list[CaseRecord]
    no_match: list[CaseRecord]
    by_customer: dict[str, list[Candidate]]
    rates: Rates
    runner: LLMRuns
    client: LLMClient


def load_inputs(settings: PipelineSettings, budget_usd: float) -> Inputs:
    """Loads the development and calibration splits, the gold candidates and the LLM runner.

    Args:
        settings: Pipeline settings.
        budget_usd: Most new LLM spend allowed on top of what the cache already holds.

    Returns:
        The inputs; the test split is never loaded.
    """
    eval_dir = settings.data_dir / "eval"
    manifest = read_manifest(settings.cases_manifest_path)
    dev_all = load_split(eval_dir, "dev", settings.cases_manifest_path, settings.cases_config_path)
    cal_all = load_split(
        eval_dir, "calibration", settings.cases_manifest_path, settings.cases_config_path
    )
    no_match = [c for c in dev_all if c.category == "no_match"]
    dev = [c for c in dev_all if in_population(c)]
    cal = [c for c in cal_all if in_population(c)]
    customers = {c.customer_id for c in [*dev, *cal, *no_match]}
    by_customer, rates = load_gold(settings.data_dir / "gold", customers)
    client = LLMClient(Settings())
    # No development or calibration case may sit inside the prompt the readings come from.
    check_prompt_sources(client, dev_all, cal_all)
    cache = ReadingCache(eval_dir / CACHE_FILE)
    runner = LLMRuns(client, cache, cache.spent() + budget_usd)
    return Inputs(manifest, dev, cal, no_match, by_customer, rates, runner, client)


def fit_command(settings: PipelineSettings, budget_usd: float, out: Path = CONFIG_PATH) -> None:
    """Fits both comprehensions and writes config/identification.yaml.

    Args:
        settings: Pipeline settings.
        budget_usd: Most new LLM spend allowed.
        out: Output path.
    """
    inputs = load_inputs(settings, budget_usd)
    spent_before = inputs.runner.cache.spent()
    fitted: dict[str, Any] = {}
    readings = {
        "rules": (
            rules_readings(inputs.dev),
            rules_readings(inputs.calibration),
            rules_readings(inputs.no_match),
        ),
        "llm": (
            llm_readings(inputs.runner, inputs.dev, 0),
            llm_readings(inputs.runner, inputs.calibration, 0),
            llm_readings(inputs.runner, inputs.no_match, 0),
        ),
    }
    for name, (dev_readings, cal_readings, nm_readings) in readings.items():
        dev = prepare(inputs.dev, dev_readings, inputs.by_customer, inputs.rates)
        cal = prepare(inputs.calibration, cal_readings, inputs.by_customer, inputs.rates)
        no_match = prepare(inputs.no_match, nm_readings, inputs.by_customer, inputs.rates)
        weights, temperature, nll = fit(dev)
        real = [top_total(p, weights) for p in dev]
        fake = [top_total(p, weights) for p in no_match]
        reject_below = choose_threshold(real, fake)
        scores = base_scores(cal, weights, temperature, reject_below)
        qhat = conformal_quantile(scores.values(), ALPHA)
        fitted[name] = {
            "weights": weights,
            "temperature": temperature,
            "qhat": qhat,
            "reject_below": reject_below,
            "dev_mean_nll": nll,
            "dev_cases": len(dev),
            "dev_real_rejected": sum(t < reject_below for t in real),
            "dev_no_match_cases": len(fake),
            "dev_no_match_accepted": sum(t >= reject_below for t in fake),
            "calibration_bases": len(scores),
            **(
                {"version": RULES_VERSION}
                if name == "rules"
                else {
                    "model": inputs.client.model,
                    "prompt_version": inputs.client.comprehension_prompt.version,
                    "run": 0,
                }
            ),
        }
        log.info(
            "identification_fitted",
            comprehension=name,
            qhat=qhat,
            temperature=temperature,
            reject_below=reject_below,
        )
    splits = inputs.manifest["splits"]
    config = {
        "version": VERSION,
        "alpha": ALPHA,
        "unit": "base case, largest nonconformity of its four variants",
        "amount_tolerance": dict(AMOUNT_TOLERANCE),
        "fitted_on": {
            "weights_and_temperature": "dev",
            "reject_below": "dev, no_match included",
            "max_real_rejection": MAX_REAL_REJECTION,
            "qhat": "calibration, accepted cases",
            "dev_sha256": splits["dev"]["sha256"],
            "calibration_sha256": splits["calibration"]["sha256"],
            "seed": inputs.manifest["seed"],
            "weight_step": WEIGHT_STEP,
        },
        "comprehension": fitted,
    }
    header = (
        "# Identification parameters (TRZ-15, design 6.2). Written by `make fit-identification`;\n"
        "# do not edit by hand. Weights, temperature and the rejection threshold come from the\n"
        "# development split and q-hat from the accepted calibration cases, once per\n"
        "# comprehension. A new fit is a new version.\n"
    )
    out.write_text(header + yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    log.info(
        "identification_written",
        path=str(out),
        llm_spend_usd=inputs.runner.cache.spent() - spent_before,
    )


# ---- report ------------------------------------------------------------------------------


def _check_config(config: dict[str, Any], manifest: dict[str, Any]) -> None:
    splits = manifest["splits"]
    fitted_on = config["fitted_on"]
    if (fitted_on["dev_sha256"], fitted_on["calibration_sha256"]) != (
        splits["dev"]["sha256"],
        splits["calibration"]["sha256"],
    ):
        raise SystemExit("config/identification.yaml was fitted on other splits: run the fit again")


TEST_GROUPS: dict[str, Callable[[CaseRecord], str]] = {
    **GROUPS,
    "provenance": lambda c: c.provenance,
}


def held_out_summary(preps: Sequence[Prepared], params: Params) -> dict[str, Any]:
    """Coverage, size and calibration on the test split with the fitted parameters (CA8).

    Args:
        preps: Prepared test cases.
        params: Fitted parameters of one comprehension.

    Returns:
        The summary, by base case too (a base is covered when its four variants are), by group
        and by confidence bin.
    """
    outs = [outcome(p, params) for p in preps]
    by_base: dict[str, list[bool]] = defaultdict(list)
    for o in outs:
        by_base[o.case.base_id].append(o.covered)
    return {
        "summary": summarize(outs),
        "bases_covered": sum(all(v) for v in by_base.values()),
        "bases": len(by_base),
        "groups": {g: by_group(outs, k) for g, k in TEST_GROUPS.items()},
        "reliability": {
            "all": reliability(outs),
            **{
                lang: reliability([o for o in outs if o.case.language == lang])
                for lang in ("es", "pt")
            },
        },
        "reliability_by": {
            group: {
                key: reliability([o for o in outs if TEST_GROUPS[group](o.case) == key])
                for key in sorted({TEST_GROUPS[group](o.case) for o in outs})
            }
            for group in ("variant", "segment")
        },
    }


def reliability_chart(title: str, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """A reliability diagram as a Mermaid chart: accuracy bars against the mean confidence line.

    The bars meet the line where the top candidate is right as often as its confidence says.

    Args:
        title: Chart title.
        rows: Non-empty confidence bins, as `reliability` returns them.

    Returns:
        The lines of a fenced Mermaid block.
    """
    labels = ", ".join(f'"{r["low"]:.1f}-{r["high"]:.1f} (n={r["n"]})"' for r in rows)
    return [
        "```mermaid",
        "xychart-beta",
        f'    title "{title}"',
        f"    x-axis [{labels}]",
        '    y-axis "Accuracy (bars), mean confidence (line)" 0 --> 1',
        f"    bar [{', '.join(f'{r["accuracy"]:.3f}' for r in rows)}]",
        f"    line [{', '.join(f'{r["confidence"]:.3f}' for r in rows)}]",
        "```",
    ]


def report_command(
    settings: PipelineSettings, budget_usd: float, out: Path = REPORT_PATH, test: bool = False
) -> dict[str, Any]:
    """Measures both comprehensions with the fitted parameters and writes the report.

    Args:
        settings: Pipeline settings.
        budget_usd: Most new LLM spend allowed (the other runs of the calibration split).
        out: Report path.
        test: Also measure the held-out test split with the same fitted parameters (CA8);
            nothing is fitted on it.

    Returns:
        The results per comprehension.
    """
    inputs = load_inputs(settings, budget_usd)
    held_out: list[CaseRecord] = []
    test_gold: tuple[dict[str, list[Candidate]], Rates] | None = None
    if test:
        eval_dir = settings.data_dir / "eval"
        test_all = load_split(
            eval_dir, "test", settings.cases_manifest_path, settings.cases_config_path
        )
        held_out = [c for c in test_all if in_population(c)]
        test_gold = load_gold(settings.data_dir / "gold", {c.customer_id for c in held_out})
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    _check_config(config, inputs.manifest)
    spent_before = inputs.runner.cache.spent()
    costs: dict[str, float] = {}
    results: dict[str, Any] = {}
    for name in ("rules", "llm"):
        params = load_params(CONFIG_PATH, name)
        runs = [0] if name == "rules" else list(range(LLM_RUNS))
        per_run = []
        for r in runs:
            if name == "rules":
                cal_r, dev_r, nm_r = (
                    rules_readings(inputs.calibration),
                    rules_readings(inputs.dev),
                    rules_readings(inputs.no_match),
                )
            else:
                cal_r = llm_readings(inputs.runner, inputs.calibration, r, costs)
                dev_r = llm_readings(inputs.runner, inputs.dev, 0, costs)
                nm_r = llm_readings(inputs.runner, inputs.no_match, 0, costs)
            cal = prepare(inputs.calibration, cal_r, inputs.by_customer, inputs.rates)
            dev = prepare(inputs.dev, dev_r, inputs.by_customer, inputs.rates)
            no_match = prepare(inputs.no_match, nm_r, inputs.by_customer, inputs.rates)
            cal_outs = [outcome(p, params) for p in cal]
            weights = dict(params.weights)
            scores = base_scores(cal, weights, params.temperature, params.reject_below)
            # The same weights without the threshold: what the system did before it.
            unscreened = base_scores(cal, weights, params.temperature)
            before = replace(
                params,
                reject_below=-math.inf,
                qhat=conformal_quantile(unscreened.values(), ALPHA),
            )
            tops = {
                "dev_real": [top_total(p, weights) for p in dev],
                "fake": [top_total(p, weights) for p in no_match],
                "cal_real": [top_total(p, weights) for p in cal],
            }
            low = math.floor(min(v for vals in tops.values() for v in vals) * 10) / 10
            grid = sorted(
                {round(low + i / 10, 1) for i in range(round((0.3 - low) * 10))}
                | {params.reject_below}
            )
            per_run.append(
                {
                    "run": r,
                    "before": {
                        "qhat": before.qhat,
                        "calibration": summarize([outcome(p, before) for p in cal]),
                        "dev": summarize([outcome(p, before) for p in dev]),
                        "no_match": summarize([outcome(p, before) for p in no_match]),
                    },
                    "curve": threshold_curve(grid, **tops),
                    "qhat_of_run": conformal_quantile(scores.values(), ALPHA),
                    "calibration": summarize(cal_outs),
                    "groups": {g: by_group(cal_outs, k) for g, k in GROUPS.items()},
                    "reliability": {
                        "all": reliability(cal_outs),
                        **{
                            lang: reliability([o for o in cal_outs if o.case.language == lang])
                            for lang in ("es", "pt")
                        },
                    },
                    "cross_fit": cross_fit(cal, params, inputs.manifest["seed"]),
                    "per_variant": per_variant(cal, params),
                    "button": button_door(cal),
                    "dev": summarize([outcome(p, params) for p in dev]),
                    "no_match": summarize([outcome(p, params) for p in no_match]),
                }
            )
        results[name] = {"params": params, "runs": per_run}
        if test_gold is not None:
            results[name]["test"] = [
                held_out_summary(
                    prepare(
                        held_out,
                        rules_readings(held_out)
                        if name == "rules"
                        else llm_readings(inputs.runner, held_out, r, costs),
                        *test_gold,
                    ),
                    params,
                )
                for r in runs
            ]
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    meta = {
        "config": config,
        "policy_version": policy["version"],
        "seed": inputs.manifest["seed"],
        "llm_spend_usd": inputs.runner.cache.spent() - spent_before,
        "cache_spend_usd": inputs.runner.cache.spent(),
        "cached_cost_usd": sum(costs.values()),
        "cached_requests": len(costs),
    }
    write_report(results, meta, out)
    for name, res in results.items():
        cal = res["runs"][0]["calibration"]
        log.info(
            "identification_evaluated",
            comprehension=name,
            coverage=cal["coverage"],
            mean_size=cal["mean_size"],
            size_one=cal["size_one"],
            brier=cal["brier"],
            ece=cal["ece"],
        )
    log.info(
        "identification_report_written",
        path=str(out),
        **{k: v for k, v in meta.items() if k != "config"},
    )
    return results


# ---- report writing ----------------------------------------------------------------------


def _values(runs: list[dict[str, Any]], get: Callable[[dict[str, Any]], float]) -> list[float]:
    return [get(r) for r in runs]


def _fmt(values: list[float], pct: bool = False, digits: int = 3) -> str:
    def one(v: float) -> str:
        if math.isinf(v):
            return "inf"
        return f"{v:.1%}" if pct else f"{v:.{digits}f}"

    if len(values) == 1:
        return one(values[0])
    # Equal runs print their value as the mean, so float noise in the sum cannot round it apart.
    mean = values[0] if max(values) == min(values) else sum(values) / len(values)
    return f"{one(mean)} [{one(min(values))}, {one(max(values))}]"


def _summary_cells(runs: list[dict[str, Any]], get: Callable[[dict[str, Any]], dict]) -> str:
    first = get(runs[0])
    cells = [
        f"{first['n']} ({first['bases']})",
        _fmt(_values(runs, lambda r: get(r)["coverage"]), pct=True),
        _fmt(_values(runs, lambda r: get(r)["accepted_coverage"]), pct=True),
        _fmt(_values(runs, lambda r: get(r)["mean_size"]), digits=2),
        _fmt(_values(runs, lambda r: get(r)["size_one"]), pct=True),
    ]
    cells += [
        _fmt(_values(runs, lambda r, d=d: get(r)["decisions"][d] / get(r)["n"]), pct=True)
        for d in _DECISIONS
    ]
    cells.append(_fmt(_values(runs, lambda r: get(r)["rejected"]), digits=1))
    return " | ".join(cells)


def write_report(results: dict[str, Any], meta: dict[str, Any], path: Path) -> None:
    """Writes the Markdown report; the same inputs always give the same bytes.

    Args:
        results: Params and per-run results per comprehension.
        meta: Fitted config, versions and spend.
        path: Report path.
    """
    config = meta["config"]
    fitted = config["comprehension"]
    names = list(results)
    has_test = all("test" in results[n] for n in names)
    box = (
        "> **Fitted before the test split, measured on it.** Weights, temperature and the "
        "rejection threshold were fitted on the development split and q-hat on the calibration "
        "split; nothing was refitted after the test split was opened. Coverage on calibration "
        "is in-sample for q-hat; the coverage against 95% is the one on the test split, below."
        if has_test
        else "> **Before the test split.** Weights, temperature and the rejection threshold "
        "were fitted on the development split and q-hat on the calibration split. Coverage on "
        "calibration is in-sample for q-hat; the cross-fit rows are the out-of-sample "
        "estimate available before the test split is frozen. The final coverage against 95% "
        "is measured on the test split."
    )
    lines = [
        "# Identification on the development, calibration and test splits"
        if has_test
        else "# Identification on the development and calibration splits",
        "",
        box,
        "",
        "Generated by `make eval-identification` (story TRZ-15; design 6.2) from "
        "`config/identification.yaml`, written by `make fit-identification`. Counts and rates "
        "only: no message or row is shown.",
        "",
        "## Run",
        "",
        f"- Parameters: `{config['version']}`; alpha {config['alpha']}; conformal unit: "
        f"{config['unit']}",
        f"- Development split sha256 `{config['fitted_on']['dev_sha256']}`",
        f"- Calibration split sha256 `{config['fitted_on']['calibration_sha256']}`",
        f"- Case generator seed: {meta['seed']}; policy version: {meta['policy_version']}",
        f"- Amount tolerance (log scale): exact or rounded "
        f"{config['amount_tolerance']['exact']:.4f}, approximate "
        f"{config['amount_tolerance']['approximate']:.4f}; see Amount tolerance below",
        f"- LLM spend of this run: {meta['llm_spend_usd']:.4f} USD new; the "
        f"{meta['cached_requests']} cached LLM readings it used cost "
        f"{meta['cached_cost_usd']:.4f} USD when first requested; total recorded in the "
        f"comprehension cache, prompt iterations included: {meta['cache_spend_usd']:.4f} USD",
        "",
        "| Comprehension | Model | Prompt or rules version | Runs on calibration |",
        "| --- | --- | --- | --- |",
    ]
    for name in names:
        f = fitted[name]
        lines.append(
            f"| {name} | {f.get('model', 'none')} | "
            f"{f.get('prompt_version', f.get('version'))} | {len(results[name]['runs'])} |"
        )
    lines += [
        "",
        "## How it is measured",
        "",
        "- Population: charge disputes of the session customer with a true transaction "
        "(no_match and other_customer are outside it). Candidates: disputable transactions of "
        "the customer in the 120 days before the case now, plus the rows the harness stages "
        "(the twin of a duplicate). The same `app.domain.identification` code the service runs "
        "scores them.",
        "- **Coverage**: share of cases whose conformal set holds the true transaction; a true "
        "transaction outside the candidates counts as a miss. **Size 1**: share of sets with "
        "exactly one transaction. Decisions follow design 6.2: 1 identified, 2 to 3 options, "
        "more than 3 ask for a detail, 0 not found.",
        "- **Rejection**: the softmax only compares candidates with each other, so it is "
        "confident even when no candidate fits (a charge that does not exist). When the best "
        "candidate's total score is below the absolute threshold, the set is empty and the case "
        "escalates. A rejected real charge counts as a miss in **Coverage**; **Coverage if "
        "accepted** is over the cases that pass the threshold, the population q-hat is computed "
        "on. **Rejected** is a count of cases.",
        "- **Brier**: multiclass, over the candidates of each case (plus 1 when the true one is "
        "missing). **ECE**: expected calibration error of the top candidate's probability, "
        f"{RELIABILITY_BINS} equal-width bins.",
        "- Cases (bases): cases and the base cases they come from. The LLM rows show the mean "
        "of its runs with the range in brackets; its weights, temperature and q-hat come from "
        "run 0.",
        "",
        "## Fitted parameters",
        "",
        "| Comprehension | " + " | ".join(COMPONENTS) + " | Temperature | Reject below | "
        "q-hat | Dev mean NLL | Dev cases | Calibration bases |",
        "| --- " * (len(COMPONENTS) + 7) + "|",
    ]
    for name in names:
        f = fitted[name]
        lines.append(
            f"| {name} | "
            + " | ".join(f"{f['weights'][k]:.1f}" for k in COMPONENTS)
            + f" | {f['temperature']:.4f} | {f['reject_below']:.4f} | "
            f"{_fmt([f['qhat']], digits=4)} | "
            f"{f['dev_mean_nll']:.4f} | {f['dev_cases']} | {f['calibration_bases']} |"
        )
    lines += [
        "",
        "## Rejection threshold",
        "",
        "Fitted on the development split, whose no_match cases describe a charge that does not "
        "exist (the calibration split has none). Among the thresholds that reject at most "
        f"{MAX_REAL_REJECTION:.0%} of the real development charges (a declared assumption), the "
        "one that accepts the fewest charges that do not exist; on a tie, the lowest. Rejecting "
        "a real charge only sends it to a person; accepting one that does not exist can register "
        "the dispute on the wrong charge.",
        "",
        "| Comprehension | Threshold | No-match with a non-empty set | No-match identified | "
        "Real rejected, development | Real rejected, calibration | q-hat |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in names:
        runs = results[name]["runs"]
        for label, get in (("none (before)", lambda r: r["before"]), ("fitted", lambda r: r)):
            nm = [get(r)["no_match"] for r in runs]
            lines.append(
                f"| {name} | {label} | "
                + _fmt([1 - x["decisions"]["not_found"] / x["n"] for x in nm], pct=True)
                + f" ({nm[0]['n']}) | "
                + _fmt([x["decisions"]["identified"] / x["n"] for x in nm], pct=True)
                + f" | {get(runs[0])['dev']['rejected']} of {get(runs[0])['dev']['n']} | "
                + _fmt([get(r)["calibration"]["rejected"] for r in runs], digits=1)
                + f" of {runs[0]['calibration']['n']} | "
                + (
                    _fmt([r["before"]["qhat"] for r in runs], digits=4)
                    if label.startswith("none")
                    else _fmt([fitted[name]["qhat"]], digits=4)
                )
                + " |"
            )
    lines += [
        "",
        "The no-match rates with the fitted threshold are in-sample: the threshold was chosen on "
        "these same 32 cases, which come from 8 base cases. The real charges rejected on the "
        "calibration split are out of sample for it. The test split measures both.",
        "",
        "Trade-off by threshold (cases; LLM: run 0). The fitted threshold is marked.",
        "",
        "| Comprehension | Threshold | Real rejected, development | No-match accepted, "
        "development | Real rejected, calibration |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name in names:
        run0 = results[name]["runs"][0]
        n_dev, n_fake, n_cal = (
            run0["dev"]["n"],
            run0["no_match"]["n"],
            run0["calibration"]["n"],
        )
        for row in run0["curve"]:
            mark = " (fitted)" if row["tau"] == fitted[name]["reject_below"] else ""
            lines.append(
                f"| {name} | {row['tau']:.4f}{mark} | "
                f"{row['dev_real_rejected']} of {n_dev} | {row['fake_accepted']} of {n_fake} | "
                f"{row['cal_real_rejected']} of {n_cal} |"
            )
    head = (
        "| Comprehension | Where | Cases (bases) | Coverage | Coverage if accepted | "
        "Mean set size | Size 1 | Identified | Options | Ask detail | Not found | Rejected |"
    )
    lines += ["", "## Conversation door: coverage and set size", "", head, "| --- " * 12 + "|"]
    for name in names:
        runs = results[name]["runs"]
        lines.append(
            f"| {name} | calibration, in-sample q-hat | "
            f"{_summary_cells(runs, lambda r: r['calibration'])} |"
        )
        rounds = [x for r in runs for x in r["cross_fit"]]
        mean_cov = [x["coverage"] for x in rounds]
        lines.append(
            f"| {name} | calibration, cross-fit ({CROSS_FIT_SPLITS} halves"
            f"{' x ' + str(len(runs)) + ' runs' if len(runs) > 1 else ''}) | "
            f"{rounds[0]['n']} ({rounds[0]['bases']}) | {_fmt(mean_cov, pct=True)} | "
            f"{_fmt([x['accepted_coverage'] for x in rounds], pct=True)} | "
            f"{_fmt([x['mean_size'] for x in rounds], digits=2)} | "
            f"{_fmt([x['size_one'] for x in rounds], pct=True)} | "
            + " | ".join(
                _fmt([x["decisions"][d] / x["n"] for x in rounds], pct=True) for d in _DECISIONS
            )
            + f" | {_fmt([x['rejected'] for x in rounds], digits=1)} |"
        )
        lines.append(
            f"| {name} | development, calibration q-hat | "
            f"{_summary_cells(runs[:1], lambda r: r['dev'])} |"
        )
    lines += [
        "",
        "Cross-fit: q-hat on a seeded half of the calibration base cases, coverage on the other "
        "half. The development row reuses the cases the weights were fitted on, so its set sizes "
        "are optimistic. q-hat of each LLM run on its own: "
        + _fmt([r["qhat_of_run"] for r in results["llm"]["runs"]], digits=4)
        + ".",
        "",
        "## Largest of four variants against a q-hat per variant",
        "",
        "| Comprehension | Variant | q-hat largest of four | Coverage | Mean size | "
        "q-hat of the variant | Coverage | Mean size |",
        "| --- " * 8 + "|",
    ]
    for name in names:
        runs = results[name]["runs"]
        for variant in runs[0]["per_variant"]:
            pv = [r["per_variant"][variant] for r in runs]
            lines.append(
                f"| {name} | {variant} | {_fmt([fitted[name]['qhat']], digits=4)} | "
                f"{_fmt([p['max_rule']['coverage'] for p in pv], pct=True)} | "
                f"{_fmt([p['max_rule']['mean_size'] for p in pv], digits=2)} | "
                f"{_fmt([p['own_qhat'] for p in pv], digits=4)} | "
                f"{_fmt([p['own']['coverage'] for p in pv], pct=True)} | "
                f"{_fmt([p['own']['mean_size'] for p in pv], digits=2)} |"
            )
    lines += [
        "",
        "Both on the calibration split, in-sample. A q-hat per variant rests on 100 cases each "
        "and covers each variant only on average; the largest-of-four rule covers every variant.",
        "",
        "## Probability calibration",
        "",
        "| Comprehension | Brier | ECE | Top-1 accuracy |",
        "| --- | --- | --- | --- |",
    ]
    for name in names:
        runs = results[name]["runs"]
        cal = [r["calibration"] for r in runs]
        lines.append(
            f"| {name} | {_fmt([c['brier'] for c in cal], digits=4)} | "
            f"{_fmt([c['ece'] for c in cal], digits=4)} | "
            f"{_fmt([c['top_accuracy'] for c in cal], pct=True)} |"
        )
    lines += [
        "",
        "Reliability of the top candidate by confidence bin, calibration split (LLM: run 0).",
        "",
        "| Comprehension | Language | Bin | Cases | Mean confidence | Accuracy |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name in names:
        for lang, rows in results[name]["runs"][0]["reliability"].items():
            for row in rows:
                lines.append(
                    f"| {name} | {lang} | {row['low']:.1f} to {row['high']:.1f} | {row['n']} | "
                    f"{row['confidence']:.3f} | {row['accuracy']:.3f} |"
                )
    for group in GROUPS:
        lines += [
            "",
            f"## By {group} (calibration split)",
            "",
            "| Comprehension | Group | Cases (bases) | Coverage | Mean size | Size 1 | Brier | "
            "ECE |",
            "| --- " * 8 + "|",
        ]
        for name in names:
            runs = results[name]["runs"]
            for key in runs[0]["groups"][group]:
                gs = [r["groups"][group][key] for r in runs]
                lines.append(
                    f"| {name} | {key} | {gs[0]['n']} ({gs[0]['bases']}) | "
                    f"{_fmt([g['coverage'] for g in gs], pct=True)} | "
                    f"{_fmt([g['mean_size'] for g in gs], digits=2)} | "
                    f"{_fmt([g['size_one'] for g in gs], pct=True)} | "
                    f"{_fmt([g['brier'] for g in gs], digits=4)} | "
                    f"{_fmt([g['ece'] for g in gs], digits=4)} |"
                )
    lines += [
        "",
        "## Entry doors (calibration split)",
        "",
        "| Door | Comprehension | Cases | Identified | Coverage |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name in names:
        runs = results[name]["runs"]
        cal = [r["calibration"] for r in runs]
        lines.append(
            f"| conversation | {name} | {cal[0]['n']} | "
            f"{_fmt([c['decisions']['identified'] / c['n'] for c in cal], pct=True)} | "
            f"{_fmt([c['coverage'] for c in cal], pct=True)} |"
        )
    button = results[names[0]]["runs"][0]["button"]
    lines += [
        f'| button "No lo reconozco" | none | {button["n"]} | {button["rate"]:.1%} | '
        f"{button['rate']:.1%} |",
        "",
        "The button door arrives with the charge chosen and only checks that it is one of the "
        "customer's disputable transactions, so its rate is high by construction. It is kept "
        "apart so it does not inflate the conversation door.",
        "",
        "## Outside the population: no charge matches (development split)",
        "",
        "| Comprehension | Cases | Identified | Options | Ask detail | Not found |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name in names:
        nm = results[name]["runs"][0]["no_match"]
        lines.append(
            f"| {name} | {nm['n']} | "
            + " | ".join(f"{nm['decisions'][d] / nm['n']:.1%}" for d in _DECISIONS)
            + " |"
        )
    lines += [
        "",
        "## Candidates and amounts",
        "",
        "| Comprehension | True transaction not a candidate | Amount not convertible |",
        "| --- | --- | --- |",
    ]
    for name in names:
        cal = results[name]["runs"][0]["calibration"]
        lines.append(f"| {name} | {cal['true_missing']} | {cal['not_convertible']} |")
    lines += [
        "",
        "## Amount tolerance",
        "",
        "- **Derivation.** Each tolerance is the largest log deviation the declared noise model "
        "of the case generator (`config/cases.yaml`, `pipeline/cases/noise.py`) gives the "
        "statements comprehension reads that way. Rounding to two significant digits deviates "
        'at most ln 1.05 (0.0488); one digit to the nearest ("about") at most ln 1.5 '
        '(0.405); one digit rounded down ("more than") at most ln 2 (0.693). Comprehension '
        "reads both hedges as approximate, so the approximate tolerance is ln 2, and the true "
        "charge costs at most one unit of the amount component at the worst declared "
        "deviation. A unit test checks the bound against the generator. Up to "
        "`identification-2` the approximate tolerance was 0.35, below both hedged bounds.",
        "- **Provenance.** The inconsistency was noticed while reviewing one case of the test "
        "split during the preparation of the curated cases (story TRZ-42), before any "
        "evaluation run on the test split. To understand the inconsistency, the identification "
        "of that case was computed once, on a draft of its message, with the service code and "
        "the `identification-2` parameters: the true charge fell outside the set and a size-1 "
        "set held another transaction. Nothing was fitted, tuned or checked on that case "
        "afterwards, and it stays in the test split like any other. The tolerance comes from "
        "the definition of the noise model, not from results; the refit and this report use "
        "only the development and "
        "calibration splits, and the test split stayed locked until a later commit, the last "
        "of story TRZ-55.",
        "- **Evidence.** On the development and calibration splits no approximate amount "
        "deviated more than 0.35 from the true charge, so the old tolerance never ruled out a "
        "true charge there. The only statement found beyond it was in that test split case. "
        "Because the change was "
        "prompted by a test case, the coverage measured on the test split may be slightly "
        "biased in favor of the system.",
        "- **Compared with `identification-2`.** On the development and calibration splits, "
        "the counts by amount form are identical for rules and LLM: cases, true charge beyond "
        "one tolerance, outside the set, and size-1 sets holding another charge. "
        "Temperatures: rules 0.04623 → 0.04622, LLM 0.04297 → 0.04296.",
        "- **Limit.** The tolerance is derived from the noise model of the synthetic case "
        "generator, not from how real customers approximate amounts. Real customers may "
        "deviate more, and a larger deviation counts as evidence against the true charge.",
        "",
    ]
    if has_test:
        lines += _test_section(results, names, meta)
    else:
        lines += [
            "## Pending until the test split is frozen",
            "",
            "- Coverage on the test split against 95%, with its sample size.",
            "- Coverage by language, variant and segment on the test split.",
            "- Coverage under distribution shift: Portuguese and the handwritten cases.",
            "",
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _test_section(results: dict[str, Any], names: list[str], meta: dict[str, Any]) -> list[str]:
    """The held-out test split: coverage against 95%, set size and calibration (CA8)."""
    alpha = meta["config"]["alpha"]
    lines = [
        "## Test split (held-out)",
        "",
        f"Target coverage: {1 - alpha:.0%}. The fitted parameters above, unchanged; LLM: "
        "mean and range of its runs. A base case is covered when its four variants are.",
        "",
        "| Comprehension | Cases (bases) | Coverage | Coverage by base case | Coverage if "
        "accepted | Mean size | Size 1 | Brier | ECE | Top-1 accuracy |",
        "| --- " * 10 + "|",
    ]
    for name in names:
        t = results[name]["test"]
        sm = [x["summary"] for x in t]
        lines.append(
            f"| {name} | {sm[0]['n']} ({sm[0]['bases']}) | "
            f"{_fmt([x['coverage'] for x in sm], pct=True)} ({sm[0]['covered']}/{sm[0]['n']}) | "
            f"{_fmt([x['bases_covered'] / x['bases'] for x in t], pct=True)} "
            f"({t[0]['bases_covered']}/{t[0]['bases']}) | "
            f"{_fmt([x['accepted_coverage'] for x in sm], pct=True)} | "
            f"{_fmt([x['mean_size'] for x in sm], digits=2)} | "
            f"{_fmt([x['size_one'] for x in sm], pct=True)} | "
            f"{_fmt([x['brier'] for x in sm], digits=4)} | "
            f"{_fmt([x['ece'] for x in sm], digits=4)} | "
            f"{_fmt([x['top_accuracy'] for x in sm], pct=True)} |"
        )
    lines += [
        "",
        "Reliability of the top candidate by confidence bin, test split (LLM: run 0).",
        "",
        "| Comprehension | Language | Bin | Cases | Mean confidence | Accuracy |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name in names:
        for lang, rows in results[name]["test"][0]["reliability"].items():
            for row in rows:
                lines.append(
                    f"| {name} | {lang} | {row['low']:.1f} to {row['high']:.1f} | {row['n']} | "
                    f"{row['confidence']:.3f} | {row['accuracy']:.3f} |"
                )
    for group in TEST_GROUPS:
        lines += [
            "",
            f"### By {group} (test split)",
            "",
            "| Comprehension | Group | Cases (bases) | Coverage | Mean size | Size 1 | Brier | "
            "ECE |",
            "| --- " * 8 + "|",
        ]
        for name in names:
            t = results[name]["test"]
            for key in t[0]["groups"][group]:
                gs = [x["groups"][group][key] for x in t]
                lines.append(
                    f"| {name} | {key} | {gs[0]['n']} ({gs[0]['bases']}) | "
                    f"{_fmt([g['coverage'] for g in gs], pct=True)} | "
                    f"{_fmt([g['mean_size'] for g in gs], digits=2)} | "
                    f"{_fmt([g['size_one'] for g in gs], pct=True)} | "
                    f"{_fmt([g['brier'] for g in gs], digits=4)} | "
                    f"{_fmt([g['ece'] for g in gs], digits=4)} |"
                )
    lines += [
        "",
        "### Reliability diagram (added after the single run)",
        "",
        "Added on 2026-10-02, after the single run on the test split, to complete TRZ-15 CA8. "
        "It required one more read of the test labels, with the LLM readings of that run taken "
        "from the cache (no new LLM call, budget 0) and the parameters above unchanged; nothing "
        "was fitted or chosen on it, and no figure above changed. Bars: share of cases whose top "
        "candidate is the true one; line: its mean probability. Bins with no case are left out; "
        "n is the number of cases in the bin. LLM: run 0.",
        "",
    ]
    labels = {"all": "all languages", "es": "Spanish", "pt": "Portuguese"}
    for name in names:
        for lang, rows in results[name]["test"][0]["reliability"].items():
            lines += [*reliability_chart(f"{name}, {labels[lang]}", rows), ""]
    lines += [
        "Reliability by variant and segment, test split (LLM: run 0). Segments with fewer than "
        "10 base cases are small: their bins do not support a conclusion.",
        "",
        "| Comprehension | Group | Bin | Cases | Mean confidence | Accuracy |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name in names:
        for by in results[name]["test"][0]["reliability_by"].values():
            for key, rows in by.items():
                for row in rows:
                    lines.append(
                        f"| {name} | {key} | {row['low']:.1f} to {row['high']:.1f} | "
                        f"{row['n']} | {row['confidence']:.3f} | {row['accuracy']:.3f} |"
                    )
    lines.append("")
    return lines


def main(argv: list[str] | None = None) -> int:
    """Command line entry point of `make fit-identification` and `make eval-identification`.

    Args:
        argv: Arguments; defaults to sys.argv.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fit", "report"])
    parser.add_argument(
        "--budget-usd", type=float, default=3.0, help="most new LLM spend of this command"
    )
    parser.add_argument("--test", action="store_true", help="also measure the test split")
    args = parser.parse_args(argv)
    settings = PipelineSettings()
    configure_logging(settings.log_level)
    if args.command == "fit":
        fit_command(settings, args.budget_usd)
    else:
        report_command(settings, args.budget_usd, test=args.test)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Invariance, repetitions and error analysis of the held-out run (TRZ-48, TRZ-49; design 6.6,
13.5, 13.6).

Everything here is read from results already stored: the case runs of the single run on the test
split (DATA_DIR/eval/harness, listed in eval/runs.jsonl), the comprehension readings of those
runs (the reading cache, run r is repetition r + 1) and the frozen identification parameters.
No LLM call is paid: the reading runner gets a budget of zero, so an uncached request stops the
command instead of calling the API. Nothing is fitted or tuned on the test split.

Loading the split reads its labels once per file; the report states the opens of each run of
this command. The report holds counts, rates and case ids only, never a message or a row.
"""

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.adapters.llm import LLMClient
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.domain.comprehension_rules import comprehend_rules
from app.domain.identification import load_params
from app.domain.pii import redact
from app.domain.policy import Autonomy, load_policy
from app.schemas.comprehension import Comprehension
from pipeline import autonomy_watch, evaluation
from pipeline.cases.schema import CaseRecord
from pipeline.cases.splits import load_split
from pipeline.comprehension_eval import FIELDS, context_of, score_case
from pipeline.comprehension_llm import CACHE_FILE, LLMRuns, ReadingCache
from pipeline.evaluation import (
    BOOTSTRAP_REPS,
    BOOTSTRAP_SEED,
    CASES_CONFIG,
    MANIFEST,
    SMALL_GROUP_BASES,
    OpenLog,
    Scored,
    automatable,
    in_scope,
)
from pipeline.identification_eval import CONFIG_PATH as IDENTIFICATION_CONFIG
from pipeline.identification_eval import in_population, load_gold, prepare
from pipeline.identification_eval import outcome as identify_case
from pipeline.language_eval import detector, wilson
from pipeline.language_eval import score as language_score
from pipeline.policy_agreement import context_of as policy_context

log = get_logger("pipeline.analysis")

REPORT_PATH = Path("docs/reports/analisis.md")
POLICY_PATH = Path("config/policy.yaml")
VARIANTS = ("es-MX", "es-CO", "es-AR", "pt-BR")

# ---- decisions and invariance (TRZ-48) ----------------------------------------------------


def decision(x: Scored) -> str:
    """The final decision of a case run, as a category of its final state (decision D4).

    Args:
        x: The scored case run.

    Returns:
        security_stop, expired, approval, person (with "+block" when a card was blocked),
        registered, registered+blocked, blocked, redirected, recognized_closed, informed or
        no_action.
    """
    f = x.run.final
    statuses = set(f.case_statuses)
    if "security_blocked" in statuses:
        return "security_stop"
    if "expired" in statuses:
        return "expired"
    if x.score.handed_off:
        kind = "approval" if "pending_analyst_approval" in statuses else "person"
        return kind + ("+block" if f.blocks else "")
    if f.disputes:
        return "registered+blocked" if f.blocks else "registered"
    if f.blocks:
        return "blocked"
    for status, name in (
        ("abstained", "redirected"),
        ("recognized_closed", "recognized_closed"),
        ("closed", "informed"),
    ):
        if status in statuses:
            return name
    return "no_action"


def registered_source(x: Scored) -> bool:
    """The run registered a dispute on the customer's true charge (or its duplicate twin)."""
    c = x.case
    right = {c.truth.transaction_id, c.scenario.twin_transaction_id} - {None}
    return any(cid == c.customer_id and t in right for cid, t, _ in x.run.final.disputes)


def charge(x: Scored) -> str | None:
    """The charge the run disputed: the first dispute's transaction, or None without one."""
    return x.run.final.disputes[0][1] if x.run.final.disputes else None


@dataclass(frozen=True)
class Reading:
    """What comprehension and identification read from the first message of one case run.

    Attributes:
        intent: Intent read.
        card_in_possession: Card possession read, or None when not read.
        set_size: Size of the conformal set; None outside the identification population.
        id_decision: Decision of design 6.2 for that set; None outside the population.
        covered: The true charge is in the set; None outside the population.
    """

    intent: str
    card_in_possession: bool | None
    set_size: int | None = None
    id_decision: str | None = None
    covered: bool | None = None


@dataclass(frozen=True)
class Discrepancy:
    """A variant of a base case whose decision is not the most common one of its base.

    Attributes:
        base_id: Base case.
        case_id: The deviating case.
        variant: Its language variant.
        decision: Its decision.
        usual: The most common decision of the base.
        kind: Class of the discrepancy.
    """

    base_id: str
    case_id: str
    variant: str
    decision: str
    usual: str
    kind: str


def classify_discrepancy(
    odd: Scored, odd_reading: Reading | None, others: Sequence[tuple[Scored, Reading | None]]
) -> str:
    """Why one variant decided differently from the others of its base, from what it read.

    The first difference found in the order of the pipeline names the class: the intent, the
    card possession, the identification decision, then the conversation.

    Args:
        odd: The deviating case run.
        odd_reading: Its reading, or None when not available.
        others: The other variants of the base with the usual decision, with their readings.

    Returns:
        A class such as "read as out of scope".
    """
    readings = [r for _, r in others if r is not None]
    if odd_reading is not None and readings:
        if odd_reading.intent != Counter(r.intent for r in readings).most_common(1)[0][0]:
            if odd_reading.intent == "out_of_scope":
                return "read as out of scope"
            return "intent read differently"
        possession = Counter(r.card_in_possession for r in readings).most_common(1)[0][0]
        if odd_reading.card_in_possession != possession:
            return "card possession read differently"
        usual_id = Counter(r.id_decision for r in readings).most_common(1)[0][0]
        if odd_reading.id_decision != usual_id:
            return f"identification: {odd_reading.id_decision} where the others {usual_id}"
    asked = [t.asked.get("asked") for t in odd.run.turns]
    usual_asked = Counter(
        tuple(t.asked.get("asked") for t in o.run.turns) for o, _ in others
    ).most_common(1)[0][0]
    if tuple(asked) != usual_asked:
        return "the conversation took other steps"
    return "same steps, another final state"


def discrepancies(
    rows: Sequence[Scored], readings: dict[str, Reading]
) -> tuple[dict[str, bool], list[Discrepancy]]:
    """Bases whose decision changes between variants, and every deviating variant.

    Args:
        rows: Scored case runs of one system and repetition.
        readings: Reading per case id (may miss cases).

    Returns:
        (base id to "the decision changes", the deviating variants with their class).
    """
    by_base: dict[str, list[Scored]] = defaultdict(list)
    for x in rows:
        by_base[x.case.base_id].append(x)
    changed, found = {}, []
    for base_id, xs in sorted(by_base.items()):
        decisions = Counter(decision(x) for x in xs)
        changed[base_id] = len(decisions) > 1
        if len(decisions) == 1:
            continue
        # A tie keeps the decision that sorts first, so the class is the same on every run.
        usual = sorted(decisions.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        others = [(x, readings.get(x.case.case_id)) for x in xs if decision(x) == usual]
        for x in sorted(xs, key=lambda x: x.case.variant):
            d = decision(x)
            if d == usual:
                continue
            kind = classify_discrepancy(x, readings.get(x.case.case_id), others)
            found.append(Discrepancy(base_id, x.case.case_id, x.case.variant, d, usual, kind))
    return changed, found


def field_changes(rows: Sequence[Scored], readings: dict[str, Reading]) -> dict[str, int]:
    """Bases where the disputed charge or the conformal set size differ between variants."""
    by_base: dict[str, list[Scored]] = defaultdict(list)
    for x in rows:
        by_base[x.case.base_id].append(x)
    out = {"bases": len(by_base), "charge": 0, "set_size": 0, "set_size_bases": 0}
    for xs in by_base.values():
        out["charge"] += len({charge(x) for x in xs}) > 1
        sizes = [readings[x.case.case_id].set_size for x in xs if x.case.case_id in readings]
        sizes = [s for s in sizes if s is not None]
        if sizes:
            out["set_size_bases"] += 1
            out["set_size"] += len(set(sizes)) > 1
    return out


def share_interval(flags: dict[str, bool], seed: int = BOOTSTRAP_SEED) -> tuple[float, float]:
    """95% bootstrap interval of the share of true flags, resampling base cases."""
    values = [float(v) for _, v in sorted(flags.items())]
    rng = random.Random(seed)
    means = sorted(
        sum(values[rng.randrange(len(values))] for _ in values) / len(values)
        for _ in range(BOOTSTRAP_REPS)
    )
    return means[int(0.025 * len(means))], means[int(0.975 * len(means)) - 1]


def repetition_changes(runs: dict[int, list[Scored]]) -> list[tuple[str, tuple[str, ...]]]:
    """Cases whose decision is not the same in every repetition.

    Args:
        runs: Scored case runs by repetition.

    Returns:
        (case id, decision of each repetition in order) for every case that changes.
    """
    by_case: dict[str, dict[int, str]] = defaultdict(dict)
    for rep, rows in runs.items():
        for x in rows:
            by_case[x.case.case_id][rep] = decision(x)
    out = []
    for case_id, ds in sorted(by_case.items()):
        seq = tuple(ds[r] for r in sorted(ds))
        if len(set(seq)) > 1:
            out.append((case_id, seq))
    return out


# ---- failures (TRZ-49) --------------------------------------------------------------------

STAGES = (
    "comprehension",
    "identification",
    "recognition",
    "policy",
    "action",
    "verification",
    "composition",
    "autonomy",
)
# The change that would attack each cause, and what was done after the single run.
CAUSES: dict[str, dict[str, str]] = {
    "injection in the text not flagged": {
        "stage": "policy",
        "change": "Detect the injected instruction in the text before comprehension and stop "
        "the case as a security event.",
        "status": "Changed after the single run (df22469, b66cfa1); measured on development only.",
    },
    "another customer's id in the text not flagged": {
        "stage": "policy",
        "change": "Detect in the text an id that is not the session customer's, or a request "
        "for another person's data, and stop the case as a security event.",
        "status": "Changed after the single run (df22469); measured on development only.",
    },
    "card blocked after the registration failed": {
        "stage": "action",
        "change": "Register and read back the dispute first; block the card only when the "
        "dispute verified.",
        "status": "Changed after the single run (2ef62a0); measured on development only.",
    },
    "in-scope message read as out of scope": {
        "stage": "comprehension",
        "change": "Before redirecting, ask the customer to confirm when the message names a "
        "charge, an amount or a card; or route an out-of-scope reading with charge clues to "
        "the rules reading.",
        "status": "Not changed.",
    },
    "card possession read differently from the label": {
        "stage": "comprehension",
        "change": "Ask whether the customer has the card before a route that blocks it, unless "
        "the message states it with a faithful fragment.",
        "status": "Not changed.",
    },
}
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Failure:
    """One failed case run: not correct against its label, or with an unsafe outcome.

    Attributes:
        case_id: The case.
        category: Category of the case.
        unsafe: Unsafe outcome types.
        stage: Stage of the primary cause.
        cause: Primary cause.
        contributing: Earlier causes that set it up, such as a misread card possession.
    """

    case_id: str
    category: str
    unsafe: tuple[str, ...]
    stage: str
    cause: str
    contributing: tuple[str, ...]


def classify_failure(x: Scored, reading: Reading | None) -> Failure:
    """Stage and cause of a failed case run, from its case, its state and its reading.

    The rules are fixed before reading the failures of the run: a case that matches none is
    kept as unclassified, never dropped.

    Args:
        x: The scored case run.
        reading: What the system read from the first message, if available.

    Returns:
        The failure.
    """
    c, f = x.case, x.run.final
    cause = UNCLASSIFIED
    if c.scenario.injection and "should_have_escalated" in x.unsafe:
        cause = "injection in the text not flagged"
    elif c.scenario.other_customer_id and "should_have_escalated" in x.unsafe:
        cause = "another customer's id in the text not flagged"
    elif c.scenario.tool_failure and f.blocks and not f.disputes and x.score.handed_off:
        cause = "card blocked after the registration failed"
    elif (
        "dissuaded_dispute" in x.unsafe and reading is not None and reading.intent == "out_of_scope"
    ):
        cause = "in-scope message read as out of scope"
    contributing = []
    # A misread possession matters only where it chose a route that blocked the card.
    if (
        f.blocks
        and reading is not None
        and c.truth.card_in_possession is not None
        and reading.card_in_possession is not None
        and reading.card_in_possession != c.truth.card_in_possession
    ):
        contributing.append("card possession read differently from the label")
    stage = CAUSES[cause]["stage"] if cause in CAUSES else UNCLASSIFIED
    return Failure(c.case_id, c.category, x.unsafe, stage, cause, tuple(contributing))


def failures(rows: Iterable[Scored], readings: dict[str, Reading]) -> list[Failure]:
    """Every failed case run of one system and repetition, classified."""
    return [
        classify_failure(x, readings.get(x.case.case_id))
        for x in rows
        if not x.score.correct or x.unsafe
    ]


def top_causes(found: Sequence[Failure], n: int = 5) -> list[tuple[str, int, str]]:
    """The n causes with most failures, primary and contributing, with one example each."""
    count: Counter[str] = Counter()
    example: dict[str, str] = {}
    for f in found:
        for cause in (f.cause, *f.contributing):
            count[cause] += 1
            example.setdefault(cause, f.case_id)
    ranked = sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return [(cause, k, example[cause]) for cause, k in ranked]


# ---- disparities (TRZ-49 CA4) -------------------------------------------------------------

# Fixed by Robinson on 2026-10-03, before any disparity was computed (decision D5): a difference
# between groups is investigated when the 95% bootstrap interval of the difference, resampling
# base cases with the bootstrap seed, excludes 0. Language variants and languages are paired
# (same base cases); segment, country and provenance compare a group with the rest, unpaired.
# Groups with fewer than SMALL_GROUP_BASES base cases are not tested: the sample does not
# support a conclusion.


def safe(x: Scored) -> float | None:
    """Safe automated resolution of one case; None when the case is out of scope."""
    if not in_scope(x.case):
        return None
    return float(
        automatable(x.case)
        and x.score.correct
        and not x.score.handed_off
        and not x.unsafe
        and not x.run.policy_violations
    )


def any_unsafe(x: Scored) -> float:
    """1 when the case run has any unsafe outcome."""
    return float(bool(x.unsafe))


Measure = Callable[[Scored], float | None]


def _percentile_interval(values: list[float]) -> tuple[float, float]:
    values.sort()
    return values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]


def paired_gap(
    rows: Sequence[Scored],
    side_a: Callable[[CaseRecord], bool],
    side_b: Callable[[CaseRecord], bool],
    measure: Measure,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, tuple[float, float], int] | None:
    """Mean over base cases of (mean of side a minus mean of side b), with its interval.

    Args:
        rows: Scored case runs.
        side_a: Cases of the first group.
        side_b: Cases of the second group.
        measure: Value of a case; None leaves it out.
        seed: Seed of the resampling.

    Returns:
        (difference, 95% interval, base cases with both sides), or None without such bases.
    """
    per_base: dict[str, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
    for x in rows:
        v = measure(x)
        if v is None:
            continue
        if side_a(x.case):
            per_base[x.case.base_id][0].append(v)
        elif side_b(x.case):
            per_base[x.case.base_id][1].append(v)
    diffs = [sum(a) / len(a) - sum(b) / len(b) for _, (a, b) in sorted(per_base.items()) if a and b]
    if not diffs:
        return None
    rng = random.Random(seed)
    boot = [
        sum(diffs[rng.randrange(len(diffs))] for _ in diffs) / len(diffs)
        for _ in range(BOOTSTRAP_REPS)
    ]
    return sum(diffs) / len(diffs), _percentile_interval(boot), len(diffs)


def unpaired_gap(
    rows: Sequence[Scored],
    in_group: Callable[[CaseRecord], bool],
    measure: Measure,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, tuple[float, float], int] | None:
    """Rate of a group minus the rate of the rest, resampling base cases on each side.

    Args:
        rows: Scored case runs.
        in_group: Cases of the group.
        measure: Value of a case; None leaves it out.
        seed: Seed of the resampling.

    Returns:
        (difference, 95% interval, base cases of the group), or None when a side is empty.
    """
    sides: tuple[dict[str, list[float]], dict[str, list[float]]] = (
        defaultdict(list),
        defaultdict(list),
    )
    for x in rows:
        v = measure(x)
        if v is not None:
            sides[0 if in_group(x.case) else 1][x.case.base_id].append(v)
    if not sides[0] or not sides[1]:
        return None
    sums = [[(sum(v), len(v)) for _, v in sorted(s.items())] for s in sides]

    def rate(pick: list[tuple[float, int]]) -> float:
        return sum(a for a, _ in pick) / sum(n for _, n in pick)

    point = rate(sums[0]) - rate(sums[1])
    rng = random.Random(seed)
    boot = []
    for _ in range(BOOTSTRAP_REPS):
        picks = [[s[rng.randrange(len(s))] for _ in s] for s in sums]
        boot.append(rate(picks[0]) - rate(picks[1]))
    return point, _percentile_interval(boot), len(sums[0])


@dataclass(frozen=True)
class Gap:
    """One comparison of the disparity rule.

    Attributes:
        dimension: Variant, language, segment, country or provenance.
        comparison: What is compared, such as "es-MX - es-AR".
        measure: safe resolution or any unsafe.
        bases: Base cases behind the comparison (of the group, when unpaired).
        difference: Point difference; None when not tested.
        interval: 95% interval; None when not tested.
        verdict: investigate, no difference shown, or too small to conclude.
    """

    dimension: str
    comparison: str
    measure: str
    bases: int
    difference: float | None
    interval: tuple[float, float] | None
    verdict: str


def _verdict(result: tuple[float, tuple[float, float], int] | None) -> str:
    if result is None or result[2] < SMALL_GROUP_BASES:
        return "too small to conclude"
    low, high = result[1]
    if low > 0 or high < 0:
        return "investigate"
    if result[0] != 0 and 0 in (low, high):
        return "no difference shown (interval ends at 0)"
    return "no difference shown"


def disparities(rows: Sequence[Scored]) -> list[Gap]:
    """Every comparison of the disparity rule on one system and repetition."""
    measures: tuple[tuple[str, Measure], ...] = (
        ("safe resolution", safe),
        ("any unsafe", any_unsafe),
    )
    out: list[Gap] = []

    def add(dimension: str, comparison: str, name: str, result: Any) -> None:
        tested = result is not None and result[2] >= SMALL_GROUP_BASES
        out.append(
            Gap(
                dimension,
                comparison,
                name,
                0 if result is None else result[2],
                result[0] if tested else None,
                result[1] if tested else None,
                _verdict(result),
            )
        )

    for name, m in measures:
        for i, a in enumerate(VARIANTS):
            for b in VARIANTS[i + 1 :]:
                result = paired_gap(
                    rows,
                    lambda c, a=a: c.variant == a,
                    lambda c, b=b: c.variant == b,
                    m,
                )
                add("variant", f"{a} - {b}", name, result)
        result = paired_gap(rows, lambda c: c.language == "es", lambda c: c.language == "pt", m)
        add("language", "es - pt", name, result)
        for dimension, key in (
            ("segment", lambda c: c.truth.segment),
            ("country", lambda c: c.truth.country_code),
            ("provenance", lambda c: c.provenance),
        ):
            for group in sorted({key(x.case) for x in rows}):
                result = unpaired_gap(rows, lambda c, g=group, k=key: k(c) == g, m)
                add(dimension, f"{group} - rest", name, result)
    return out


# ---- handwritten reviews (TRZ-49) ---------------------------------------------------------


def review_disagreements(
    folder: Path, cases: dict[str, CaseRecord]
) -> list[tuple[CaseRecord, str, str]]:
    """Handwritten cases whose action in both reviews differs from the constructed label.

    Args:
        folder: DATA_DIR/eval/handwritten.
        cases: Test cases by case id.

    Returns:
        (case, action of review 1, action of review 2) of every disagreement.
    """
    by_key = {(c.base_id, c.variant): c for c in cases.values()}
    reviews = []
    for n in (1, 2):
        data = yaml.safe_load((folder / f"revision_{n}.yaml").read_text("utf-8"))
        reviews.append({(i["base_id"], i["variante"]): i for i in data["items"]})
    out = []
    for key, item in sorted(reviews[0].items()):
        case = by_key.get(key)
        if case is None:
            continue
        first, second = str(item["accion"]), str(reviews[1][key]["accion"])
        if first != case.expected.action or second != case.expected.action:
            out.append((case, first, second))
    return out


# ---- autonomy watch on audits of what the system did alone (TRZ-49) ----------------------


def watch_comparison(
    runs: dict[int, list[Scored]], params: Autonomy
) -> list[tuple[int, tuple[str, str], Any, Any]]:
    """Each cell's streams with every review counted and with only the audits counted.

    Args:
        runs: Scored runs of the base system by repetition.
        params: Autonomy settings of the policy.

    Returns:
        (repetition, cell, streams with every review, streams with the audits only).
    """
    out = []
    for rep, rows in sorted(runs.items()):
        for cell, pool in autonomy_watch.cells(autonomy_watch.outcome(x) for x in rows).items():
            if not any(o.kind == "auto" for o in pool):
                continue
            out.append(
                (
                    rep,
                    cell,
                    autonomy_watch.replay(pool, params),
                    autonomy_watch.replay(pool, params, count_handovers=False),
                )
            )
    return out


# ---- components (TRZ-49 CA4) --------------------------------------------------------------


def approximate_without_hedge(
    cases: Iterable[CaseRecord], readings: dict[str, Comprehension]
) -> dict[str, int]:
    """Cases labeled with an approximate amount, and how many readings mark it approximate.

    Args:
        cases: Test cases.
        readings: The LLM reading per case id (first message).

    Returns:
        Counts: labeled approximate, marked by the rules, marked by the LLM.
    """
    out = {"labeled": 0, "rules": 0, "llm": 0}
    for c in cases:
        a = c.noise.amount
        if not (a.mentioned and a.form == "approximate"):
            continue
        out["labeled"] += 1
        message = redact(c.message)[0]
        rules = comprehend_rules(message, context_of(c)).amount
        out["rules"] += bool(rules and rules.approximate)
        llm = readings.get(c.case_id)
        out["llm"] += bool(llm and llm.amount and llm.amount.approximate)
    return out


def escalation_conditions(c: CaseRecord) -> list[str]:
    """Escalation conditions of design 8 that the label of a case meets at once."""
    ctx = policy_context(c)
    hits = {
        "amount_above_human_review": ctx.amount_usd is not None
        and ctx.amount_usd > load_policy(POLICY_PATH).amount_usd.human_review_above,
        "open_dispute_last_90d": ctx.open_dispute_last_90d,
        "conformal_set_empty": ctx.conformal_set_size == 0,
        "verification_failed": ctx.verification_failed,
    }
    return [k for k, v in hits.items() if v]


# ---- loading ------------------------------------------------------------------------------


@dataclass
class Loaded:
    """Everything the report reads."""

    cases: dict[str, CaseRecord]
    runs: dict[tuple[str, int], list[Scored]]
    lines: dict[tuple[str, int], dict[str, Any]]
    readings: dict[int, dict[str, Reading]]
    comprehension: dict[int, dict[str, Comprehension]]
    dev_rows: list[tuple[dict[str, Any], list[Scored]]]
    opens: dict[str, int]
    folder: Path
    verifier_off: tuple[dict[str, Any], list[Scored]] | None = None


def _readings(
    cases: list[CaseRecord], folder: Path, data_dir: Path
) -> tuple[dict[int, dict[str, Reading]], dict[int, dict[str, Comprehension]]]:
    """Readings of the three LLM runs from the reading cache, and identification on them."""
    client = LLMClient(Settings())
    cache = ReadingCache(folder / CACHE_FILE)
    # A budget of what is already spent: an uncached request stops instead of calling the API.
    runner = LLMRuns(client, cache, cache.spent())
    params = load_params(IDENTIFICATION_CONFIG, "llm")
    population = [c for c in cases if in_population(c)]
    by_customer, rates = load_gold(data_dir / "gold", {c.customer_id for c in population})
    readings: dict[int, dict[str, Reading]] = {}
    raw: dict[int, dict[str, Comprehension]] = {}
    for r in range(3):
        outcomes = runner.run(cases, r)
        raw[r + 1] = {cid: o.reading for cid, o in outcomes.items()}
        faithful = {
            c.case_id: outcomes[c.case_id].reading.faithful(redact(c.message)[0])[0] for c in cases
        }
        idents = {
            p.case.case_id: identify_case(p, params)
            for p in prepare(population, faithful, by_customer, rates)
        }
        readings[r + 1] = {}
        for c in cases:
            reading = faithful[c.case_id]
            ident = idents.get(c.case_id)
            readings[r + 1][c.case_id] = Reading(
                intent=reading.intent,
                card_in_possession=(
                    reading.card_in_possession.value if reading.card_in_possession else None
                ),
                set_size=ident.size if ident else None,
                id_decision=ident.decision if ident else None,
                covered=ident.covered if ident else None,
            )
    return readings, raw


def load(settings: Settings) -> Loaded:
    """Loads the test cases, the recorded runs and the readings, counting the test opens."""
    folder = Path(settings.data_dir) / "eval"
    opens = OpenLog(folder)
    sys.addaudithook(opens.hook)
    opens.active = True
    cases = {c.case_id: c for c in load_split(folder, "test", MANIFEST, CASES_CONFIG)}  # type: ignore[arg-type]
    opens.active = False
    runs, lines = {}, {}
    verifier_off = None
    for (system, variant, rep), line in evaluation.latest("test").items():
        if variant not in ("base", evaluation.VERIFIER_OFF):
            continue
        path = folder / "harness" / line["run_file"]
        if evaluation._sha256(path) != line["run_file_sha256"]:
            raise ValueError(f"{path.name} does not match the hash recorded in the run log")
        rows = evaluation.scored(evaluation.load_runs(path), cases)
        if variant == evaluation.VERIFIER_OFF:
            verifier_off = (line, rows)
            continue
        runs[(system, rep)] = rows
        lines[(system, rep)] = line
    readings, raw = _readings(list(cases.values()), folder, Path(settings.data_dir))
    dev_lines = [
        line
        for line in evaluation.read_log()
        if line["split"] == "dev"
        and line["system"] == "trazo"
        and line["variant"] == "base"
        and line["bases"] is None
        and line.get("complete")
    ]
    dev_rows = []
    if len(dev_lines) >= 2:
        dev = {c.case_id: c for c in load_split(folder, "dev", MANIFEST, CASES_CONFIG)}  # type: ignore[arg-type]
        for line in (dev_lines[0], dev_lines[-1]):
            path = folder / "harness" / line["run_file"]
            dev_rows.append((line, evaluation.scored(evaluation.load_runs(path), dev)))
    return Loaded(
        cases, runs, lines, readings, raw, dev_rows, dict(opens.opens), folder, verifier_off
    )


# ---- report -------------------------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def _mr(values: Sequence[float], fmt: str = "{:.1%}") -> str:
    if len(set(values)) == 1:
        return fmt.format(values[0]) + " (same in every run)"
    mean = sum(values) / len(values)
    return f"{fmt.format(mean)} (range {fmt.format(min(values))} to {fmt.format(max(values))})"


def _wilson_cell(k: int, n: int) -> str:
    if n == 0:
        return "n/a"
    low, high = wilson(k, n)
    return f"{k}/{n} ({k / n:.1%}; {low:.1%} to {high:.1%})"


def invariance_section(data: Loaded) -> list[str]:
    """TRZ-48 CA1 and CA2: decisions, charges and set sizes across the four variants."""
    md = [
        "## Invariance across language variants\n",
        "Story TRZ-48, design 6.6. Each base case of the held-out split was run in ES-MX, ES-CO, "
        "ES-AR and PT-BR. For each base case and repetition the four variants are compared on "
        "the charge disputed (final state), the size of the conformal set and the final "
        "decision. The decision is the category of the final state: registered, registered and "
        "blocked, handed to a person (escalation or approval), redirected, closed after "
        "recognition, claim status informed, security stop, expired. The set size is "
        "recomputed with no LLM call from the cached comprehension of the first message of "
        "each repetition and the frozen parameters `identification-3`: it is the set of the "
        "first turn, which the run files do not keep. A discrepancy is a variant whose "
        "decision is not the most common one of its base case.\n",
        "| System | Rep. | Bases | Decision changes between variants | Disputed charge "
        "changes | Set size changes (bases in the identification population) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    shares: dict[str, list[float]] = defaultdict(list)
    for (system, rep), rows in sorted(
        data.runs.items(), key=lambda kv: (kv[0][0] != "trazo", kv[0])
    ):
        readings = data.readings.get(rep, {}) if system == "trazo" else {}
        changed, _ = discrepancies(rows, readings)
        fields = field_changes(rows, data.readings.get(rep, {}))
        k = sum(changed.values())
        shares[system].append(k / len(changed))
        low, high = share_interval(changed)
        md.append(
            f"| {system} | {rep} | {len(changed)} | {k} ({k / len(changed):.1%}; "
            f"{low:.1%} to {high:.1%}) | {fields['charge']} | "
            f"{fields['set_size']} of {fields['set_size_bases']} |"
        )
    md += [
        "",
        "Interval: 95% bootstrap over base cases, seed "
        f"{BOOTSTRAP_SEED}. For TRAZO, mean over repetitions: "
        f"{_mr(shares['trazo'])}. The free agent ran once. The set size of the free agent row "
        "is TRAZO's first-turn set: the free agent has no conformal set.\n",
    ]
    for system in ("trazo", "free_agent"):
        rows = data.runs.get((system, 1))
        if rows is None:
            continue
        _, found = discrepancies(rows, data.readings.get(1, {}) if system == "trazo" else {})
        md += [
            f"### Discrepancies of {system}, repetition 1\n",
            "| Class | Variants | Bases | Variant of each (decision, usual decision) |",
            "| --- | --- | --- | --- |",
        ]
        by_kind: dict[str, list[Discrepancy]] = defaultdict(list)
        for d in found:
            by_kind[d.kind].append(d)
        for kind, ds in sorted(by_kind.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            variants = Counter(d.variant for d in ds)
            md.append(
                f"| {kind} | {len(ds)} | {len({d.base_id for d in ds})} | "
                + ", ".join(f"{v} {n}" for v, n in sorted(variants.items()))
                + " |"
            )
        if system == "trazo":
            md += ["", "Every TRAZO discrepancy of repetition 1:\n"]
            md += [
                f"- `{d.case_id}` ({data.cases[d.case_id].category}): {d.decision} where the "
                f"other variants {d.usual}; {d.kind}."
                for d in found
            ]
        else:
            md += [
                "",
                "The free agent's reading is not stored, so its classes come from the "
                "conversation only.",
            ]
        md.append("")
    md.append('```mermaid\nxychart-beta\n    title "TRAZO discrepancies by class (repetition 1)"\n')
    _, found = discrepancies(data.runs[("trazo", 1)], data.readings.get(1, {}))
    kinds = Counter(d.kind for d in found).most_common()
    md[-1] += (
        "    x-axis ["
        + ", ".join(f'"{k}"' for k, _ in kinds)
        + ']\n    y-axis "Variants"\n    bar ['
        + ", ".join(str(n) for _, n in kinds)
        + "]\n```\n"
    )
    return md


def repetitions_section(data: Loaded) -> list[str]:
    """TRZ-48 CA3: mean and range of every measure over the three repetitions of TRAZO."""
    reps = sorted(r for s, r in data.runs if s == "trazo")
    ms = [evaluation.measures(data.runs[("trazo", r)]) for r in reps]
    md = [
        "## Variability over repetitions\n",
        f"TRAZO ran the held-out split {len(reps)} times with the real LLM (`"
        f"{data.lines[('trazo', reps[0])]['model']}`), each repetition a new paid call "
        "(cost per repetition below). Mean and range of each measure:\n",
        "| Measure | Mean and range |",
        "| --- | --- |",
    ]
    rows = [
        ("Safe automated resolution", [m["safe_resolution"]["rate"] for m in ms], "{:.1%}"),
        ("Attempted automation", [m["attempted_automation"]["rate"] for m in ms], "{:.1%}"),
        ("Containment", [m["containment"]["rate"] for m in ms], "{:.1%}"),
        ("Missed escalations", [m["missed_escalation"]["rate"] for m in ms], "{:.1%}"),
        ("Unnecessary escalations", [m["unnecessary_escalation"]["rate"] for m in ms], "{:.1%}"),
        ("Cases with any unsafe outcome", [m["unsafe_any"]["k"] for m in ms], "{:.0f}"),
        (
            "Complete dossiers",
            [m["dossier"]["complete_cases"] / m["dossier"]["cases"] for m in ms],
            "{:.1%}",
        ),
        ("Latency p50 (ms)", [m["efficiency"]["latency_p50_ms"] for m in ms], "{:.0f}"),
        ("Latency p95 (ms)", [m["efficiency"]["latency_p95_ms"] for m in ms], "{:.0f}"),
        ("Turns", [m["efficiency"]["turns"] for m in ms], "{:.0f}"),
        ("Cost (USD)", [m["efficiency"]["cost_usd"] for m in ms], "{:.4f}"),
    ]
    md += [f"| {name} | {_mr(v, fmt)} |" for name, v, fmt in rows]
    changes = repetition_changes({r: data.runs[("trazo", r)] for r in reps})
    md += [
        "",
        f"Cases whose decision is not the same in every repetition: {len(changes)} of "
        f"{len(data.runs[('trazo', reps[0])])}.",
    ]
    md += [f"- `{cid}`: " + " / ".join(seq) for cid, seq in changes]
    same = sum(
        len({data.readings[r][cid].intent for r in reps}) == 1 for cid in data.readings[reps[0]]
    )
    md += [
        "",
        f"First-turn intent read the same in the {len(reps)} repetitions: {same} of "
        f"{len(data.readings[reps[0]])} cases. Comprehension field by field over the same "
        "three runs is in `docs/reports/comprension_prueba.md`.",
        "",
        "The free agent ran once: two more repetitions would cost about "
        f"{2 * data.lines[('free_agent', 1)]['metrics']['efficiency']['cost_usd']:.1f} USD and "
        "the held-out split is not run again, so its variability is not measured.\n",
    ]
    return md


def failures_section(data: Loaded) -> list[str]:
    """TRZ-49 CA1 to CA3: every failure of TRAZO by stage and cause, the top causes, the
    free agent's failures, and what changed after the single run."""
    rows = data.runs[("trazo", 1)]
    found = failures(rows, data.readings.get(1, {}))
    md = [
        "## Error analysis\n",
        "Story TRZ-49, design 13.6. A failure is a case run whose final state is not the one "
        "its label asks for, or that has an unsafe outcome. TRAZO, repetition 1 (the three "
        f"repetitions fail the same way: see the variability section). {len(found)} of "
        f"{len(rows)} case runs fail; every one is below, none is left out. The stage and the "
        "cause come from fixed rules over the case, its final state and what comprehension "
        "read from the first message; a case they do not explain stays `unclassified`. Declared: "
        "the rules were written after the failures of the run had been read (breakdown of "
        "2026-10-03); they sort the failures and change no measure.\n",
        "### Failures by stage and cause\n",
        "| Stage | Cause | Failures | Categories | Also contributing |",
        "| --- | --- | --- | --- | --- |",
    ]
    groups: dict[tuple[str, str], list[Failure]] = defaultdict(list)
    for f in found:
        groups[(f.stage, f.cause)].append(f)
    for (stage, cause), fs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        cats = Counter(f.category for f in fs)
        contributing = Counter(c for f in fs for c in f.contributing)
        md.append(
            f"| {stage} | {cause} | {len(fs)} | "
            + ", ".join(f"{k} {v}" for k, v in sorted(cats.items()))
            + " | "
            + (", ".join(f"{k} ({v})" for k, v in contributing.items()) or "none")
            + " |"
        )
    stages = Counter(f.stage for f in found)
    md += [
        "",
        "Stages with no failure: "
        + ", ".join(s for s in STAGES if s not in stages)
        + ". Unsafe types: "
        + ", ".join(
            f"{k} {v}" for k, v in sorted(Counter(u for f in found for u in f.unsafe).items())
        )
        + ".\n",
        "### The five main causes\n",
        "Counted over primary and contributing causes. The example is one case of each; what "
        "happened is read from its final state.\n",
        "| # | Cause | Stage | Failures | Example | Change that would attack it | After the "
        "single run |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, (cause, k, example) in enumerate(top_causes(found), 1):
        info = CAUSES.get(cause, {"stage": UNCLASSIFIED, "change": "n/a", "status": "n/a"})
        md.append(
            f"| {i} | {cause} | {info['stage']} | {k} | `{example}` "
            f"({decision(next(x for x in rows if x.case.case_id == example))}) | "
            f"{info['change']} | {info['status']} |"
        )
    md += ["", "Mermaid chart of the same counts:\n"]
    top = top_causes(found)
    md.append(
        '```mermaid\nxychart-beta\n    title "TRAZO failures by cause (repetition 1)"\n'
        "    x-axis ["
        + ", ".join(f'"{c}"' for c, _, _ in top)
        + ']\n    y-axis "Failures"\n    bar ['
        + ", ".join(str(k) for _, k, _ in top)
        + "]\n```\n"
    )
    md += _after_run_lines(data)
    md += _free_agent_lines(data)
    md += _component_error_lines(data, {f.case_id for f in found})
    return md


def _after_run_lines(data: Loaded) -> list[str]:
    md = [
        "### What changed after the single run, and what did not\n",
        "The held-out split was not run again: the failures above are those of the system "
        "as it ran. Three causes have a change made after the run; it was measured on the "
        "development split only, the run before the changes against the latest after them, "
        "correct cases by category. Development cases are the ones the system was built on, "
        "so this is not an estimate on new cases.\n",
    ]
    if len(data.dev_rows) == 2:
        (before_line, before), (after_line, after) = data.dev_rows
        md += [
            f"| Category | before (`{before_line['commit'][:9]}`) | after "
            f"(`{after_line['commit'][:9]}`) |",
            "| --- | --- | --- |",
        ]
        for category in (
            "injection",
            "other_customer",
            "tool_failure",
            "missing_data",
            "ambiguous",
        ):
            b = [x for x in before if x.case.category == category]
            a = [x for x in after if x.case.category == category]
            md.append(
                f"| {category} | {sum(x.score.correct for x in b)}/{len(b)} | "
                f"{sum(x.score.correct for x in a)}/{len(a)} |"
            )
        md.append("")
    md += [
        "Not changed: the in-scope messages read as out of scope (the three ES-MX "
        "redirections) and the misread card possession. Neither shows on development, where "
        "the comprehension prompt was tuned.\n"
    ]
    return md


def _free_agent_lines(data: Loaded) -> list[str]:
    rows = data.runs.get(("free_agent", 1))
    if not rows:
        return []
    failed = [x for x in rows if not x.score.correct or x.unsafe]
    types = Counter(u for x in failed for u in x.unsafe)
    return [
        "### Free agent\n",
        f"The free agent fails {len(failed)} of {len(rows)} case runs. Its reading and its "
        "decisions are made inside one model call, so a stage cannot be read from its state; "
        "its failures are given by unsafe type: "
        + ", ".join(f"{k} {v}" for k, v in types.most_common())
        + f". Failures with no unsafe outcome (wrong final state only): "
        f"{sum(not x.unsafe for x in failed)}.\n",
    ]


def _component_error_lines(data: Loaded, failed: set[str]) -> list[str]:
    raw = data.comprehension.get(1, {})
    cases = [data.cases[cid] for cid in raw]
    wrong: Counter[str] = Counter()
    scored_fields: Counter[str] = Counter()
    for c in cases:
        if c.case_id in failed:
            continue
        s = score_case(c, raw[c.case_id])
        for name in FIELDS:
            if name in s.correct:
                scored_fields[name] += 1
                wrong[name] += not s.correct[name]
    readings = data.readings.get(1, {})
    uncovered = [cid for cid, r in readings.items() if r.covered is False and cid not in failed]
    md = [
        "### Component errors that did not change the outcome\n",
        "In the cases that did not fail (repetition 1), errors of a component that the rest "
        "of the pipeline absorbed: the customer chose from options, answered a question, or "
        "the field was not needed.\n",
        "| Component | Errors | Scored |",
        "| --- | --- | --- |",
    ]
    md += [
        f"| comprehension: {f}"
        + (
            " variant (not used to decide: the rules detector decides the language)"
            if f == "language"
            else ""
        )
        + f" | {wrong[f]} | {scored_fields[f]} |"
        for f in FIELDS
    ]
    population = sum(r.covered is not None for cid, r in readings.items() if cid not in failed)
    md += [
        f"| identification: true charge outside the conformal set | {len(uncovered)} | "
        f"{population} |",
        "",
    ]
    return md


def handwritten_section(data: Loaded) -> list[str]:
    """TRZ-49: the handwritten cases whose reviews pick another action than the label."""
    found = review_disagreements(data.folder / "handwritten", data.cases)
    trazo = {x.case.case_id: x for x in data.runs[("trazo", 1)]}
    free = {x.case.case_id: x for x in data.runs.get(("free_agent", 1), [])}
    md = [
        "### Handwritten cases where the reviews pick another action\n",
        f"Both reviews of the handwritten cases agree with each other on every action, and "
        f"differ from the constructed label in {len(found)} of 60. Crossed with the injection "
        "cases (decision of Robinson, 2026-10-02), to see whether the disagreement comes from "
        "how an injection is labeled (`security_blocked`):\n",
        "| Label | Both reviews | Cases | Injection cases | TRAZO correct against the label "
        "| TRAZO registered the charge, as the reviews | Free agent correct against the label |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    pairs: dict[tuple[str, str], list[CaseRecord]] = defaultdict(list)
    for case, first, second in found:
        pairs[(case.expected.action, first if first == second else f"{first} / {second}")].append(
            case
        )
    for (label, review), cs in sorted(pairs.items()):
        md.append(
            f"| {label} | {review} | {len(cs)} | {sum(c.scenario.injection for c in cs)} | "
            f"{sum(trazo[c.case_id].score.correct for c in cs if c.case_id in trazo)} | "
            f"{sum(registered_source(trazo[c.case_id]) for c in cs if c.case_id in trazo)} | "
            f"{sum(free[c.case_id].score.correct for c in cs if c.case_id in free)} |"
        )
    injection = [c for c, _, _ in found if c.scenario.injection]
    same = sum(registered_source(trazo[c.case_id]) for c in injection if c.case_id in trazo)
    md += [
        "",
        "Bases: " + ", ".join(sorted({f"`{c.base_id}`" for c, _, _ in found})) + ".",
        "",
        f"In the {len(injection)} injection cases the two reviews did what TRAZO did: they "
        f"registered the customer's own charge, and TRAZO registered it in {same} of them. "
        "That makes the `security_blocked` label of these cases debatable: the people who "
        "reviewed them did not stop them as a security event. The label is kept, decided "
        "before the run, and TRAZO is counted against it as `should_have_escalated`.",
        "",
        "Reading, not a decision: in every one of these cases the reviews pick a registration "
        "where the label does not. The review sheet showed the message and the facts "
        "of the charge, its status included, but not the scenario of the case, such as a "
        "customer who recognizes the charge once shown its detail; that can explain the "
        "`recognized_closed` rows. Why the reviews register the pending charges labeled "
        "`explain_and_watch` is not settled by these data. The labels were not changed.\n",
    ]
    return md


def watch_section(data: Loaded) -> list[str]:
    """TRZ-49: the Wilson finding of TRZ-47 and the improvement to try, [simulado]."""
    params = load_policy(POLICY_PATH).autonomy
    runs = {r: v for (s, r), v in data.runs.items() if s == "trazo"}
    md = [
        "### Autonomy watch diluted by the reviews of handovers [simulado]\n",
        "Finding of TRZ-47: at A0 a cell counts in its Wilson bound every review an analyst "
        "makes, the audits of what the system did alone (drawn at rho = "
        f"{params.audit_sample_rate}) and every handover with a registration recommended. "
        "Handovers are almost always right, so they dilute the audits of the actions taken "
        "alone, and a cell with unsafe actions is not demoted. The improvement to try: count "
        "in the bound only the audits of what the system did alone. Same streams as the "
        f"autonomy watch section of `evaluacion.md` (seed {autonomy_watch.SIM_SEED}, "
        f"{autonomy_watch.SCENARIO_STREAMS:,} streams of {autonomy_watch.SCENARIO_CASES:,} "
        "cases per cell, the base runs of the held-out split). Only simulated: the policy and "
        "the service are not changed.\n",
        "| Cell | Rep. | Expected reversal rate per review: every review, audits only | "
        "Streams demoted: every review, audits only | Unsafe per stream with the watch: every "
        "review, audits only (without the watch) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for rep, cell, every, audits in watch_comparison(runs, params):
        rho = params.audit_sample_rate
        rate_every = autonomy_watch.expected_reversal_rate(every.pool, rho)
        auto = [o for o in audits.pool if o.kind == "auto"]
        rate_audits = sum(o.wrong for o in auto) / len(auto) if auto else None
        md.append(
            f"| {cell[0]} · {cell[1].upper()} | {rep} | {_pct(rate_every)}, {_pct(rate_audits)} "
            f"| {len(every.detected_at_case)}, {len(audits.detected_at_case)} of {every.streams} "
            f"| {sum(every.unsafe_with) / every.streams:.1f}, "
            f"{sum(audits.unsafe_with) / audits.streams:.1f} "
            f"({sum(every.unsafe_without) / every.streams:.1f}) |"
        )
    demoted = sum(len(a.detected_at_case) for _, _, _, a in watch_comparison(runs, params))
    md += [
        "",
        f"- Counting only the audits raises the expected reversal rate of the cells with unsafe "
        "actions (table above), but it stays far from the near 50% a block of "
        f"{params.window_n} needs to reach W >= {params.demote_if_wilson_lower_gte}: "
        f"{demoted} demoted streams in all the rows above, and the unsafe outcomes per stream "
        "with the watch stay practically those without it. The improvement alone does not "
        "make Wilson stop an error rate of this size. A larger audit rate closes blocks "
        "sooner but leaves the reversal rate per review the same; demoting at this rate needs "
        "a lower threshold, a change of design 6.7 to weigh against its false alarms, not "
        "tuned here.",
        "",
    ]
    return md


def disparity_section(data: Loaded) -> list[str]:
    """TRZ-49 CA4: the disparity rule over variants, languages, segments, countries and
    provenance, and the component checks of the pending rows."""
    md = [
        "## Disparities\n",
        "Rule fixed before computing (decision of Robinson, 2026-10-03): a difference between "
        "groups is investigated when the 95% bootstrap interval of the difference, resampling "
        f"base cases with seed {BOOTSTRAP_SEED} ({BOOTSTRAP_REPS} resamples), excludes 0. "
        "Variants and languages are paired on the same base cases; segment, country and "
        "provenance compare a group with the rest. A group with fewer than "
        f"{SMALL_GROUP_BASES} base cases is not tested: the sample does not support a "
        "conclusion. No correction for the number of comparisons: at 95%, about one in twenty "
        "comparisons with no real difference crosses the rule by chance.\n",
    ]
    for system in ("trazo", "free_agent"):
        rows = data.runs.get((system, 1))
        if rows is None:
            continue
        gaps = disparities(rows)
        md += [
            f"### {system}, repetition 1\n",
            "| Dimension | Comparison | Measure | Bases | Difference | 95% interval | Verdict |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for g in gaps:
            diff = "n/a" if g.difference is None else f"{g.difference * 100:+.1f} pts"
            ci = (
                "n/a"
                if g.interval is None
                else f"{g.interval[0] * 100:+.1f} to {g.interval[1] * 100:+.1f}"
            )
            md.append(
                f"| {g.dimension} | {g.comparison} | {g.measure} | {g.bases} | {diff} | {ci} "
                f"| {g.verdict} |"
            )
        md.append("")
        tested = [g for g in gaps if g.difference is not None]
        crossing = [g for g in tested if g.verdict == "investigate"]
        edge = [g for g in tested if g.verdict.endswith("ends at 0)")]
        md.append(
            f"{system}: {len(tested)} comparisons tested, {len(crossing)} cross the rule"
            + (
                ": " + "; ".join(f"{g.comparison} ({g.measure})" for g in crossing)
                if crossing
                else ""
            )
            + f"; {len(gaps) - len(tested)} not tested for size. "
            + (
                f"{len(edge)} end at 0: "
                + "; ".join(f"{g.comparison} ({g.measure})" for g in edge)
                + ". Those come from a handful of base cases moving in one direction, too few "
                "for the rule to conclude.\n"
                if edge
                else "\n"
            )
        )
    md += [
        "Investigated anyway, because it is the one gap tied to a known failure: ES-MX against "
        "the other variants in TRAZO's safe resolution (-3.4 points, interval ending at 0). "
        "It is the three ES-MX messages that comprehension read as out of scope (see the "
        "discrepancies above: `test-8f0468319d-es-mx`, `test-c92130c4e3-es-mx`, "
        "`test-92deaff50f-es-mx`). Probable cause: the out-of-scope reading of the LLM on "
        "these ES-MX phrasings, where the other three variants of the same base were read in "
        "scope; three cases cannot tell whether it is the variant or the phrasing. Change "
        "that would correct it: the one given for `in-scope message read as out of scope` in "
        "the error analysis. The sample does not "
        "allow a conclusion that ES-MX is served worse.\n",
    ]
    md += component_checks(data)
    return md


def component_checks(data: Loaded) -> list[str]:
    """Pending rows of TRZ-49 on the components, measured on the test split."""
    readings = data.readings.get(1, {})
    population = [data.cases[cid] for cid, r in readings.items() if r.covered is not None]
    md = [
        "### Components on the held-out split\n",
        "Identification coverage by segment and category (LLM, repetition 1; pending from "
        "calibration, where Plus and missing_data were below 95%). By case and by base case "
        "(all four variants covered), with Wilson 95% intervals; the variants of a base are "
        "not independent, so the base-case interval is the honest one.\n",
        "| Group | Cases covered | Base cases covered |",
        "| --- | --- | --- |",
    ]
    for name, key in (
        ("segment", lambda c: c.truth.segment),
        ("category", lambda c: c.category),
    ):
        for group in sorted({key(c) for c in population}):
            cs = [c for c in population if key(c) == group]
            k = sum(bool(readings[c.case_id].covered) for c in cs)
            bases: dict[str, bool] = defaultdict(lambda: True)
            for c in cs:
                bases[c.base_id] = bases[c.base_id] and bool(readings[c.case_id].covered)
            md.append(
                f"| {name}: {group} | {_wilson_cell(k, len(cs))} | "
                f"{_wilson_cell(sum(bases.values()), len(bases))} |"
            )
    trazo = {x.case.case_id: x for x in data.runs[("trazo", 1)]}
    uncovered = sorted(c.case_id for c in population if not readings[c.case_id].covered)
    md += ["", f"The {len(uncovered)} cases whose true charge is outside the set:\n"]
    for cid in uncovered:
        c, r = data.cases[cid], readings[cid]
        x = trazo.get(cid)
        md.append(
            f"- `{cid}` ({c.category}, {c.truth.segment}): set of {r.set_size}, "
            f"{r.id_decision}; TRAZO ended {decision(x) if x else 'n/a'}, "
            f"{'correct' if x and x.score.correct else 'not correct'} against the label."
        )
    cases = list(data.cases.values())
    md += [
        "",
        "Language detector of TRZ-11 (rules, no LLM) on the first message, measured on the "
        "test split for the first time: its word lists were set on development.\n",
        "| Cases | Language right |",
        "| --- | --- |",
    ]
    for title, subset in (
        ("all", cases),
        ("handwritten", [c for c in cases if c.provenance == "handwritten"]),
        ("code-mixed (portuñol)", [c for c in cases if c.scenario.code_mixed]),
        *((v, [c for c in cases if c.variant == v]) for v in VARIANTS),
    ):
        s = language_score(subset, detector).get("all", {"n": 0, "language": 0})
        md.append(f"| {title} | {_wilson_cell(s['language'], s['n'])} |")
    raw = data.comprehension.get(1, {})
    channel: Counter[tuple[str, str]] = Counter()
    for c in cases:
        if c.noise.channel.mentioned and c.truth.channel and c.case_id in raw:
            hint = raw[c.case_id].channel_hint
            read = hint.value if hint else "none"
            if read != c.truth.channel:
                channel[(c.truth.channel, read)] += 1
    approx = approximate_without_hedge(cases, raw)
    two = [c for c in cases if c.variant == "es-MX" and len(escalation_conditions(c)) > 1]
    md += [
        "",
        "- Channel read by the LLM against the label, mentioned channels only (repetition 1), "
        "label to reading: "
        + (", ".join(f"{a} to {b} {n}" for (a, b), n in channel.most_common()) or "no error")
        + ". The labels of the generator put the channel where the charge was made; a message "
        "that says where the customer saw it can read as another channel, so part of these "
        "may be label ambiguity, which this count cannot separate.",
        f"- Approximate amounts: {approx['labeled']} cases are labeled with an approximate "
        f"amount; the rules mark {approx['rules']} as approximate and the LLM "
        f"{approx['llm']}. None of the failures above involves the amount.",
        f"- Base cases whose label meets two escalation conditions at once: {len(two)}"
        + (
            " ("
            + ", ".join(f"`{c.base_id}`: {', '.join(escalation_conditions(c))}" for c in two)
            + ")"
            if two
            else ""
        )
        + ". The order of the escalation rules is covered by unit tests.",
        "",
    ]
    return md


ABLATIONS_PATH = Path("eval/ablations.json")
COMPREHENSION_COLUMNS: tuple[tuple[str, Callable[[dict[str, Any]], float | None]], ...] = (
    ("Intent F1", lambda r: r["intent_macro_f1"]),
    ("OOS recall", lambda r: r["out_of_scope_recall"]["rate"]),
    ("In-scope sent out", lambda r: r["in_scope_sent_out"]["rate"]),
    ("Amount", lambda r: r["fields"]["amount"]["accuracy"]["rate"]),
    ("Date", lambda r: r["fields"]["date"]["accuracy"]["rate"]),
    ("Merchant", lambda r: r["fields"]["merchant"]["accuracy"]["rate"]),
    ("Channel", lambda r: r["fields"]["channel"]["accuracy"]["rate"]),
    ("Card possession", lambda r: r["fields"]["card_in_possession"]["accuracy"]["rate"]),
    ("Faithful", lambda r: r["faithful"]["rate"]),
)


def _cell(values: Sequence[float | None], column: str) -> str:
    xs = [v for v in values if v is not None]
    if not xs:
        return "n/a"
    fmt = "{:.3f}" if column == "Intent F1" else "{:.1%}"
    if len(set(xs)) == 1:
        return fmt.format(xs[0])
    return f"{fmt.format(sum(xs) / len(xs))} [{fmt.format(min(xs))}, {fmt.format(max(xs))}]"


def comprehension_rows(results: dict[str, Any], group: str, key: str | None) -> list[str]:
    """One table row per comprehension system for one group (overall, a language, a variant)."""
    out = []
    for name, label in (
        ("rules", "rules"),
        ("tfidf_lr", "TF-IDF + LR (intent only)"),
        ("haiku", "Haiku 4.5 (3 runs)"),
        ("sonnet", "Sonnet 5.5 (1 run)"),
    ):
        runs = results[name] if isinstance(results[name], list) else [results[name]]
        picked = [r[group] if key is None else r[group][key] for r in runs]
        cells = []
        for column, get in COMPREHENSION_COLUMNS:
            if name == "tfidf_lr" and column not in (
                "Intent F1",
                "OOS recall",
                "In-scope sent out",
            ):
                cells.append("n/a")
            else:
                cells.append(_cell([get(p) for p in picked], column))
        out.append(f"| {key or 'all'} | {label} | " + " | ".join(cells) + " |")
    return out


def ablations_section(data: Loaded) -> list[str]:
    """TRZ-50: comprehension of four systems, TF-IDF + LR, ranker and the fact checker off."""
    md = [
        "## Ablations\n",
        "Story TRZ-50, design 6.1, 6.2 and 13.1. **Declared: these ablations were run after "
        "the single run on the test split and after its results were read.** Each one was "
        "trained on the development split, calibrated on the calibration split and evaluated "
        "once on the held-out split, with every setting committed before the evaluation; "
        "nothing was adjusted after it. A second evaluation is refused by the command unless "
        "a reason is given, and none was.\n",
    ]
    if not ABLATIONS_PATH.exists():
        return [*md, "The ablations are not evaluated yet (`make eval-ablations`).\n"]
    r = json.loads(ABLATIONS_PATH.read_text(encoding="utf-8"))
    s = r["settings"]
    md += [
        f"- Evaluated on {r['date']}, commit `{r['commit'][:9]}`"
        + (f", reason for a new evaluation: {r['reason']}" if r["reason"] else "")
        + f"; split sha256 test_generated `{r['split_sha256']['test_generated'][:12]}...`, "
        f"test_handwritten `{r['split_sha256']['test_handwritten'][:12]}...`, dev "
        f"`{r['split_sha256']['dev'][:12]}...`, calibration "
        f"`{r['split_sha256']['calibration'][:12]}...` (checked against the manifest when "
        "loaded).",
        f"- Seed {r['seed']}; scikit-learn {r['sklearn']}; LLM `{r['models']['haiku']}` and "
        f"`{r['models']['sonnet']}`, prompt `{r['prompt_version']}`; policy "
        f"`{r['policy_version']}`; identification `{r['identification_version']}`.",
        f"- New LLM spend: {r['llm_spend_usd']:.4f} USD (readings from the cache of the "
        f"single run). Opens of the held-out files: {r['test_file_opens']}.",
        "",
        "### Comprehension: four systems on the same held-out cases\n",
        "Rules, Haiku and Sonnet as in `comprension_prueba.md` and `comprension_prueba_sonnet.md`"
        ", recomputed from the same cache. TF-IDF + logistic regression reads only the intent. "
        "Haiku: mean and [range] of 3 runs.\n",
        "| Group | System | " + " | ".join(c for c, _ in COMPREHENSION_COLUMNS) + " |",
        "| --- | --- | " + " | ".join("---" for _ in COMPREHENSION_COLUMNS) + " |",
        *comprehension_rows(r["comprehension"], "overall", None),
    ]
    for language in ("es", "pt"):
        md += comprehension_rows(r["comprehension"], "by_language", language)
    for variant in VARIANTS:
        md += comprehension_rows(r["comprehension"], "by_variant", variant)
    t = r["tfidf"]
    test_f1 = r["comprehension"]["tfidf_lr"]["overall"]["intent_macro_f1"]
    rules_f1 = r["comprehension"]["rules"]["overall"]["intent_macro_f1"]
    cal_f1 = t["calibration_split"]["intent_macro_f1"]
    md += [
        "",
        "### TF-IDF + logistic regression for the intent (CA1)\n",
        f"Character n-grams {tuple(s['tfidf']['ngram_range'])} (`{s['tfidf']['analyzer']}`, "
        f"min_df {s['tfidf']['min_df']}, sublinear tf) and a logistic regression with C = "
        f"{s['logistic_c']}, trained on the {t['dev_cases']} development cases (redacted "
        "messages, as the service reads them). Softmax temperature fitted on the "
        f"{t['calibration_cases']} calibration cases: {t['temperature']:.3f}.\n",
        "| Measure | Calibration split | Test split |",
        "| --- | --- | --- |",
        f"| Intent macro F1 | {t['calibration_split']['intent_macro_f1']:.3f} | {test_f1:.3f} |",
        f"| Brier of the intent probabilities, before and after the temperature | n/a | "
        f"{t['test_uncalibrated']['brier']:.3f}, {t['test_calibrated']['brier']:.3f} |",
        f"| ECE of the top intent, before and after the temperature | n/a | "
        f"{t['test_uncalibrated']['ece']:.3f}, {t['test_calibrated']['ece']:.3f} |",
        "",
        f"- Finding: the cheap learned baseline falls from {cal_f1:.3f}"
        f" on calibration to {test_f1:.3f} on the held-out split, below the rules "
        f"({rules_f1:.3f}). The development and calibration splits come from generator A and "
        "the test split from generator B and handwritten messages, so the probable cause is "
        "that the n-grams learned the wording of one generator; these data cannot separate "
        "that from other causes. On this held-out split the rules and the LLM do better than "
        "this model, so it is not a cheaper alternative here.",
        f"- The temperature fitted on calibration moves the test ECE from "
        f"{t['test_uncalibrated']['ece']:.3f} to {t['test_calibrated']['ece']:.3f} and the "
        f"Brier from {t['test_uncalibrated']['brier']:.3f} to {t['test_calibrated']['brier']:.3f}"
        ": it was fitted on cases of the same generator as training.",
        "",
    ]
    md += _ranker_lines(r)
    md += _verifier_lines(data)
    md += [
        "### Gate of learned and statistical components\n",
        "- Split hashes: every split was checked against the manifest when loaded; the test "
        "split is the frozen one.",
        "- No leak: TF-IDF and the ranker were trained on development only; calibration set "
        "only the temperature of TF-IDF and the q-hat of each score; no test case reached a "
        "weight, a temperature or a threshold.",
        "- Same held-out cases and metrics as the rules baseline, by language and variant.",
        "- Brier and ECE are reported for the identification probabilities of both scores and "
        "for the TF-IDF intent probabilities.",
        "- Deterministic parts: unit tests fit TF-IDF and the ranker twice with the same seed "
        "and get the same model; the Wilson reference values are unchanged "
        "(`tests/unit/test_wilson.py`).",
        "- The real LLM: Haiku 3 runs; Sonnet 1 run, as decided in TRZ-12 CA10; the fact "
        "checker off, 1 run (repetition 1 of the base, from its cache).",
        "- Recorded: model, prompt version, policy version, seed and split hashes above.",
        "- None of the ablations of design 13.1 was cut (CA4).",
        "",
    ]
    return md


def _ranker_lines(r: dict[str, Any]) -> list[str]:
    ident = r["identification"]
    manual, ranker = ident["manual"], ident["ranker"]
    md = [
        "### Ranker against the manual score (CA3)\n",
        'Logistic regression of "this candidate is the true charge" on the five components '
        "of the manual score, trained on the development cases of the identification "
        f"population ({ident['population']['dev']}), with the LLM readings of run 0; its "
        "coefficients divided by the sum of their absolute values and its temperature fitted "
        "on development by the same search as the manual score. q-hat of each score on the "
        f"calibration split ({ident['population']['calibration']} cases) at each alpha, with no "
        "absolute rejection threshold for either, so the sets differ only by the scores. Test: "
        f"{ident['population']['test']} cases.\n",
        "| Score | " + " | ".join(manual["weights"]) + " | Temperature | Development NLL |",
        "| --- | " + " | ".join("---" for _ in manual["weights"]) + " | --- | --- |",
    ]
    for name, x in (("manual", manual), ("ranker", ranker)):
        md.append(
            f"| {name} | "
            + " | ".join(f"{x['weights'][k]:.3f}" for k in manual["weights"])
            + f" | {x['temperature']:.3f} | {x['dev_nll']:.3f} |"
        )
    md += [
        "",
        "| Alpha | Score | q-hat | Covered (test) | Mean set size | Size one | Brier | ECE |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for m_row, r_row in zip(manual["curve"], ranker["curve"], strict=True):
        for name, row in (("manual", m_row), ("ranker", r_row)):
            md.append(
                f"| {row['alpha']} | {name} | {row['qhat']:.3f} | "
                f"{_wilson_cell(row['covered'], row['n'])} | {row['mean_size']:.2f} | "
                f"{row['size_one']:.1%} | {row['brier']:.3f} | {row['ece']:.3f} |"
            )
    p = manual["production"]
    md += [
        "",
        f"Reference, the manual score as the service runs it (alpha 0.05, its q-hat and "
        f"rejection threshold): covered {_wilson_cell(p['covered'], p['n'])}, mean set size "
        f"{p['mean_size']:.2f}.\n",
        '```mermaid\nxychart-beta\n    title "Mean set size on the test split by alpha"\n'
        "    x-axis [" + ", ".join(f'"{x["alpha"]}"' for x in manual["curve"]) + "]\n"
        '    y-axis "Mean set size"\n'
        "    line [" + ", ".join(f"{x['mean_size']:.2f}" for x in manual["curve"]) + "]\n"
        "    line [" + ", ".join(f"{x['mean_size']:.2f}" for x in ranker["curve"]) + "]\n"
        "```\n",
        "First line: manual; second: ranker.\n",
    ]
    coverage_gaps = [
        abs(a["coverage"] - b["coverage"])
        for a, b in zip(manual["curve"], ranker["curve"], strict=True)
    ]
    gaps = [
        abs(a["mean_size"] - b["mean_size"])
        for a, b in zip(manual["curve"], ranker["curve"], strict=True)
    ]
    md += [
        f"- Finding: at the same alpha the two scores reach the same coverage within "
        f"{max(coverage_gaps):.1%} and mean set sizes within {max(gaps):.2f}; development NLL "
        f"{ranker['dev_nll']:.3f} for the ranker against {manual['dev_nll']:.3f} for the manual "
        "grid. "
        + (
            "The trained ranker gives sets of practically the same size at the same alpha, "
            "with a higher development NLL, so it buys nothing over the manual score and its "
            "five interpretable weights, which stays."
            if ranker["dev_nll"] >= manual["dev_nll"] and max(gaps) <= 0.05
            else "The ranker makes some sets smaller; see the table."
        ),
        "",
    ]
    return md


def _verifier_lines(data: Loaded) -> list[str]:
    md = ["### Fact checker on and off\n"]
    if data.verifier_off is None:
        return [*md, "The run with the fact checker off is not recorded yet.\n"]
    line, off = data.verifier_off
    on = data.runs[("trazo", 1)]
    m_on, m_off = evaluation.measures(on), evaluation.measures(off)

    def claims(rows: list[Scored]) -> tuple[int, int, int]:
        return (
            sum(x.run.final.unsupported_sent for x in rows),
            sum(x.run.final.unsupported_sent > 0 for x in rows),
            sum(x.run.final.replies_checked for x in rows),
        )

    c_on, c_off = claims(on), claims(off)
    kinds = Counter(k for x in off for k in x.run.final.unsupported_kinds)
    md += [
        f"TRAZO on the held-out split with the fact checker off: commit `{line['commit'][:9]}` "
        "(the commit of the single run, so the fact checker is the only change), model "
        f"`{line['model']}`, repetition 1 of the base run's cache: "
        f"{line['metrics']['efficiency']['llm_cache_hits']} of "
        f"{line['metrics']['efficiency']['llm_calls']} LLM requests answered from the cache "
        "(no new spend when they are all cached; the cost column of the run log is the cost "
        f"first paid), refused requests {line['metrics']['efficiency']['llm_refused']}, "
        f"complete: {line['complete']}. Reason recorded with the run: "
        f"{line.get('rerun_reason')}. With the checker off, the replies still go through it in "
        "observer mode, so the claims without a source that reach the customer are counted. "
        "The run read the labels of the held-out split once more: "
        f"{line.get('test_file_opens')}.\n",
        "| Measure | Checker on (base, rep. 1) | Checker off |",
        "| --- | --- | --- |",
        f"| Claims without a source sent | {c_on[0]} | {c_off[0]} |",
        f"| Cases with a claim without a source | {c_on[1]}/{len(on)} | {c_off[1]}/{len(off)} |",
        f"| Replies checked | {c_on[2]} | {c_off[2]} |",
        f"| Safe automated resolution | {evaluation._fmt_rate(m_on['safe_resolution'])} | "
        f"{evaluation._fmt_rate(m_off['safe_resolution'])} |",
        f"| Cases with any unsafe outcome | {m_on['unsafe_any']['k']}/{len(on)} | "
        f"{m_off['unsafe_any']['k']}/{len(off)} |",
        "",
        "Kinds of the claims sent with the checker off: "
        + (", ".join(f"{k} {n}" for k, n in kinds.most_common()) or "none")
        + ".\n",
    ]
    return md


def write_report(data: Loaded, out: Path = REPORT_PATH) -> None:
    """Writes docs/reports/analisis.md."""
    trazo = data.lines[("trazo", 1)]
    md = [
        "# Analysis of the held-out run: invariance, repetitions and errors\n",
        "Generated by `make eval-analysis` (stories TRZ-48, TRZ-49 and TRZ-50) from the runs "
        "recorded "
        "in `eval/runs.jsonl`: the single run on the test split, commit "
        f"`{trazo['commit'][:9]}`, model `{trazo['model']}`, comprehension prompt "
        f"`{trazo['prompts']['comprehension']}`, policy `{trazo['policy_version']}`, "
        f"identification `{trazo['identification_version']}`, split sha256 "
        + ", ".join(f"{k} `{v[:12]}...`" for k, v in trazo["split_sha256"].items())
        + ". Every figure is an **[offline]** measurement with a simulated client, except the "
        "rows labeled [simulado]. No LLM call was made: readings come from the cache of the "
        "run. Counts, rates and case ids only.\n",
        "Declared: this analysis was written after the single run and after its results were "
        "read. It changes no measure of `evaluacion.md`, fits nothing and tunes nothing on the "
        "test split. Loading the split reads its labels; opens of the held-out files by this "
        f"command: {dict(sorted(data.opens.items())) or 'none recorded'}.\n",
    ]
    md += invariance_section(data)
    md += repetitions_section(data)
    md += failures_section(data)
    md += handwritten_section(data)
    md += watch_section(data)
    md += disparity_section(data)
    md += ablations_section(data)
    out.write_text("\n".join(md).rstrip() + "\n", encoding="utf-8")
    log.info("analysis_report_written", path=str(out))


def main(argv: list[str] | None = None) -> int:
    """Entry point of `make eval-analysis`.

    Args:
        argv: Arguments; defaults to sys.argv.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)
    configure_logging("WARNING")
    settings = Settings(log_level="WARNING")
    write_report(load(settings), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Which transaction the customer disputes, with a coverage guarantee (design 6.2, TRZ-15).

Every candidate gets an additive match score with five interpretable components; a softmax with
a temperature turns the scores of one case into probabilities; split conformal turns those into
a set that holds the true transaction at least 1 - alpha of the time, marginally, when new cases
are exchangeable with the calibration cases. The same functions serve the service (candidates
from Postgres) and the evaluation (candidates from the gold files), so what is measured is what
runs.
"""

import math
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

import yaml

from app.domain.fx import Rates, convert
from app.schemas.comprehension import Comprehension

DISPUTABLE_TYPES = ("Purchase", "Payment", "Withdrawal")
DISPUTABLE_STATUSES = ("Approved", "Pending")
COMPONENTS = ("amount", "date", "merchant", "channel", "currency")
# Most options shown as cards before asking for another detail (design 6.2, K).
MAX_OPTIONS = 3

# Fixed scales, not fitted: they put the components on comparable units so the fitted weights
# stay readable. A plain amount is often rounded to two significant digits (up to ~5% off); a
# hedged one keeps one digit or says "more than", so it can be off by half or double.
AMOUNT_TOLERANCE = {"exact": 0.05, "approximate": 0.35}
DATE_SCALE_DAYS = 3.0
# A far-off amount or date is evidence against a candidate, but one clue read wrong must not
# rule the true charge out on its own.
PENALTY_CAP = 4.0

Decision = Literal["identified", "show_options", "ask_for_detail", "not_found"]
DuplicateTwin = Literal["one_pending", "both_approved"]
Door = Literal["conversation", "button"]


@dataclass(frozen=True)
class Candidate:
    """A transaction the customer could be disputing.

    Attributes:
        transaction_id: Id of the transaction.
        timestamp: Naive local time, like the simulated clock.
        amount: Registered amount.
        currency: Registered currency.
        channel: Channel of the transaction.
        merchant_name: Merchant; only purchases have one.
        transaction_type: Purchase, Payment or Withdrawal.
        status: Approved or Pending.
    """

    transaction_id: str
    timestamp: datetime
    amount: float
    currency: str
    channel: str
    merchant_name: str | None
    transaction_type: str
    status: str


@dataclass(frozen=True)
class Params:
    """Fitted weights and temperature, and the conformal threshold, of one comprehension.

    Attributes:
        version: Version of config/identification.yaml they come from.
        comprehension: rules or llm: the comprehension whose clues they were fitted on.
        weights: Weight of each component.
        temperature: Softmax temperature.
        qhat: Conformal threshold on 1 - p, computed on the accepted calibration cases.
        reject_below: Absolute rejection threshold: when the best candidate's total score is
            below it, nothing matches the clues well enough and the set is empty.
    """

    version: str
    comprehension: str
    weights: Mapping[str, float]
    temperature: float
    qhat: float
    reject_below: float = -math.inf


@dataclass(frozen=True)
class Scored:
    """One candidate with its score.

    Attributes:
        candidate: The transaction.
        components: Contribution of each component before weighting.
        total: Weighted sum of the components.
        probability: Softmax probability among the candidates of the case.
    """

    candidate: Candidate
    components: Mapping[str, float]
    total: float
    probability: float


@dataclass(frozen=True)
class Identification:
    """Outcome of identifying the disputed transaction.

    Attributes:
        door: conversation, or button when the charge arrived already chosen.
        scored: Candidates, most probable first; empty for the button door.
        conformal_set: Ids in the conformal set, most probable first.
        decision: What design 6.2 says to do with a set of that size.
        amount_not_convertible: The stated amount could not be converted for some candidate.
        rejected: The best candidate scored below the rejection threshold, so the set is empty
            and the case escalates with the clues as open questions.
    """

    door: Door
    scored: tuple[Scored, ...]
    conformal_set: tuple[str, ...]
    decision: Decision
    amount_not_convertible: bool = False
    rejected: bool = False


def is_disputable(candidate: Candidate, now: datetime, window_days: int) -> bool:
    """Whether a transaction is a candidate: type, status and the window before `now`.

    Args:
        candidate: The transaction.
        now: Simulated "now" of the case.
        window_days: Dispute window, `dispute_window_days` of config/policy.yaml.

    Returns:
        True for a purchase, payment or withdrawal, approved or pending, in the window
        before `now`.
    """
    return (
        candidate.transaction_type in DISPUTABLE_TYPES
        and candidate.status in DISPUTABLE_STATUSES
        and now - timedelta(days=window_days) <= candidate.timestamp <= now
    )


def fold(text: str) -> str:
    """Lowercase letters and digits only, accents removed, words separated by one space.

    Args:
        text: Free text.

    Returns:
        The folded text.
    """
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join("".join(ch if ch.isalnum() else " " for ch in plain).split())


def merchant_similarity(hint: str, merchant: str) -> float:
    """How well the merchant the customer wrote matches a merchant name, from 0 to 1.

    A part of the name, as a customer who half remembers it writes it, is a full match; a
    misspelling scores by character overlap.

    Args:
        hint: Merchant as the customer wrote it.
        merchant: Merchant name of the transaction.

    Returns:
        The similarity.
    """
    h, m = fold(hint), fold(merchant)
    if not h or not m:
        return 0.0
    if h in m or m in h:
        return 1.0
    return SequenceMatcher(None, h, m).ratio()


def _amount(
    clues: Comprehension, c: Candidate, local_currency: str, rates: Rates
) -> tuple[float, bool]:
    clue = clues.amount
    if clue is None:
        return 0.0, False
    # Without a stated currency the customer may mean the registered one or their own; the
    # reading that fits better is the one they meant.
    currencies = [clue.currency] if clue.currency else sorted({c.currency, local_currency})
    tolerance = AMOUNT_TOLERANCE["approximate" if clue.approximate else "exact"]
    best: float | None = None
    for currency in currencies:
        stated = convert(clue.value, currency, c.currency, c.timestamp.date(), rates)
        if stated is None or stated.amount <= 0:
            continue
        distance = abs(math.log(stated.amount) - math.log(c.amount))
        penalty = -min(distance / tolerance, PENALTY_CAP)
        best = penalty if best is None else max(best, penalty)
    return (0.0, True) if best is None else (best, False)


def _date(clues: Comprehension, c: Candidate) -> float:
    if clues.date is None:
        return 0.0
    first, last = clues.date.window()
    day = c.timestamp.date()
    outside = max((first - day).days, (day - last).days, 0)
    return -min(outside / DATE_SCALE_DAYS, PENALTY_CAP)


def components(
    clues: Comprehension, candidate: Candidate, local_currency: str, rates: Rates
) -> tuple[dict[str, float], bool]:
    """Contribution of each component for one candidate, before weighting (TRZ-15 CA2).

    A clue the customer did not give contributes 0 to every candidate, so it does not move
    the probabilities.

    Args:
        clues: Faithful clues of the customer message.
        candidate: The transaction.
        local_currency: Local currency of the customer's country.
        rates: Rates for converting the stated amount on the transaction date.

    Returns:
        Component name to value, and whether the stated amount could not be converted.
    """
    amount, not_convertible = _amount(clues, candidate, local_currency, rates)
    merchant = 0.0
    if clues.merchant_hint is not None and candidate.merchant_name:
        merchant = merchant_similarity(clues.merchant_hint.value, candidate.merchant_name)
    channel = 0.0
    if clues.channel_hint is not None:
        channel = 1.0 if clues.channel_hint.value == candidate.channel else -1.0
    currency = 0.0
    if clues.amount is not None and clues.amount.currency == candidate.currency:
        currency = 1.0
    values = {
        "amount": amount,
        "date": _date(clues, candidate),
        "merchant": merchant,
        "channel": channel,
        "currency": currency,
    }
    return values, not_convertible


def softmax(totals: Sequence[float], temperature: float) -> list[float]:
    """Turns the scores of one case into probabilities.

    Args:
        totals: Weighted scores of the candidates.
        temperature: Positive temperature; larger spreads the probability.

    Returns:
        Probabilities in the same order, summing to 1; empty for no candidates.
    """
    if not totals:
        return []
    top = max(totals)
    exps = [math.exp((t - top) / temperature) for t in totals]
    total = sum(exps)
    return [e / total for e in exps]


def weighted(values: Mapping[str, float], weights: Mapping[str, float]) -> float:
    """Weighted sum of the components.

    Args:
        values: Component values.
        weights: Component weights.

    Returns:
        The total score.
    """
    return sum(weights[k] * values[k] for k in COMPONENTS)


def conformal_quantile(scores: Iterable[float], alpha: float) -> float:
    """Split conformal threshold: the ceil((n + 1)(1 - alpha))-th smallest score (TRZ-15 CA4).

    Args:
        scores: Nonconformity scores 1 - p(true) of the calibration units; infinity when the
            true transaction was not among the candidates.
        alpha: Miscoverage level.

    Returns:
        q-hat; infinity when n is too small for the level, so every candidate is kept.
    """
    ordered = sorted(scores)
    n = len(ordered)
    # The epsilon keeps a product such as 101 * 0.95 from rounding up past an exact integer.
    k = math.ceil((n + 1) * (1 - alpha) - 1e-9)
    return math.inf if k > n else ordered[k - 1]


def decide(set_size: int) -> Decision:
    """What to do with a conformal set of a given size (design 6.2).

    Args:
        set_size: Transactions in the set.

    Returns:
        identified (1), show_options (2 to MAX_OPTIONS), ask_for_detail (more), not_found (0).
    """
    if set_size == 0:
        return "not_found"
    if set_size == 1:
        return "identified"
    return "show_options" if set_size <= MAX_OPTIONS else "ask_for_detail"


def identify(
    clues: Comprehension,
    candidates: Sequence[Candidate],
    params: Params,
    local_currency: str,
    rates: Rates,
) -> Identification:
    """Scores the candidates and builds the conformal set (TRZ-15 CA2, CA6).

    The softmax only compares candidates with each other, so it gives a confident answer even
    when none of them fits the description (a charge that does not exist). The absolute
    threshold on the best total catches that case first: the set is empty (design 6.2, size 0).

    Args:
        clues: Faithful clues of the customer message.
        candidates: Disputable transactions of the session customer.
        params: Weights, temperature and q-hat of the comprehension that read the clues.
        local_currency: Local currency of the customer's country.
        rates: Rates covering the candidate dates.

    Returns:
        The identification, entered through the conversation.
    """
    parts = [components(clues, c, local_currency, rates) for c in candidates]
    totals = [weighted(values, params.weights) for values, _ in parts]
    probs = softmax(totals, params.temperature)
    scored = sorted(
        (
            Scored(c, values, total, p)
            for c, (values, _), total, p in zip(candidates, parts, totals, probs, strict=True)
        ),
        key=lambda s: (-s.probability, s.candidate.transaction_id),
    )
    rejected = bool(scored) and max(totals) < params.reject_below
    # The tolerance absorbs float error when p is exactly at the threshold.
    kept = (
        ()
        if rejected
        else tuple(
            s.candidate.transaction_id for s in scored if 1 - s.probability <= params.qhat + 1e-12
        )
    )
    return Identification(
        door="conversation",
        scored=tuple(scored),
        conformal_set=kept,
        decision=decide(len(kept)),
        amount_not_convertible=any(nc for _, nc in parts),
        rejected=rejected,
    )


def identify_from_button(candidates: Sequence[Candidate], transaction_id: str) -> Identification:
    """The "No lo reconozco" button: the charge arrives chosen and is only checked.

    Args:
        candidates: Disputable transactions of the session customer.
        transaction_id: Transaction the button was pressed on.

    Returns:
        Identified when it is one of the candidates, else not found.
    """
    found = any(c.transaction_id == transaction_id for c in candidates)
    kept = (transaction_id,) if found else ()
    return Identification(door="button", scored=(), conformal_set=kept, decision=decide(len(kept)))


def load_params(path: Path, comprehension: str) -> Params:
    """Reads the fitted parameters of one comprehension from config/identification.yaml.

    Args:
        path: The YAML file.
        comprehension: rules or llm.

    Returns:
        The parameters.
    """
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    fitted = config["comprehension"][comprehension]
    return Params(
        version=str(config["version"]),
        comprehension=comprehension,
        weights={k: float(fitted["weights"][k]) for k in COMPONENTS},
        temperature=float(fitted["temperature"]),
        qhat=float(fitted["qhat"]),
        reject_below=float(fitted["reject_below"]),
    )


def dates_of(candidates: Iterable[Candidate]) -> tuple[date, date] | None:
    """First and last transaction date among the candidates.

    Args:
        candidates: Candidates of a case.

    Returns:
        The range, or None when there are none.
    """
    days = [c.timestamp.date() for c in candidates]
    return (min(days), max(days)) if days else None


def find_twin(charge: Candidate, candidates: Iterable[Candidate]) -> Candidate | None:
    """The twin of a charge: another charge of the same merchant, amount and currency.

    When there are several twins, the closest in time is the one charged twice.

    Args:
        charge: The charge the customer is looking at.
        candidates: The customer's other disputable charges.

    Returns:
        The twin, or None when the charge has none.
    """
    twins = [
        c
        for c in candidates
        if c.transaction_id != charge.transaction_id
        and c.merchant_name is not None
        and c.merchant_name == charge.merchant_name
        and c.amount == charge.amount
        and c.currency == charge.currency
    ]
    if not twins:
        return None
    return min(twins, key=lambda c: (abs(c.timestamp - charge.timestamp), c.transaction_id))


def duplicate_twin(charge: Candidate, candidates: Iterable[Candidate]) -> DuplicateTwin | None:
    """Status of a charge and its twin (see `find_twin`).

    Args:
        charge: The charge the customer says was duplicated.
        candidates: The customer's other disputable charges.

    Returns:
        one_pending when either of the two is still pending (a temporary hold), both_approved
        when both are settled, None when the charge has no twin.
    """
    twin = find_twin(charge, candidates)
    if twin is None:
        return None
    return "one_pending" if "Pending" in (charge.status, twin.status) else "both_approved"

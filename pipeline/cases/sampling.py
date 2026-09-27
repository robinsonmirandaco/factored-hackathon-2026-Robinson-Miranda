"""Splits and base cases (TRZ-42 CA1, CA5): real cohort transactions, split by customer and time.

Customers of the serving cohort fall in one bucket (dev, calibration, test) by md5(seed:id)
inside their country x segment stratum. Each split reads only transactions of its customers
whose local date falls in its period, and its cases "happen" inside that period, so test comes
after calibration and dev in time and shares no customer with them.

Inside a split, each category walks its eligible transactions in md5(seed:split:category:id)
order and takes one per customer. A first pass keeps every marginal of country, segment, channel
and transaction type within its proportional quota; a second pass fills what the quotas left
empty. All randomness of a base case comes from md5(seed:base_id), so adding a case does not
move the draws of the others.
"""

import hashlib
import random
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import duckdb

from pipeline.cases.labels import LabelRules, amount_band, expected_action
from pipeline.cases.noise import (
    NoiseConfig,
    PresenceTable,
    draw_amount,
    draw_date,
    draw_merchant,
    draw_now,
    draw_presence,
    significant,
)
from pipeline.cases.schema import (
    AmountClue,
    BaseCase,
    Category,
    DateClue,
    Intent,
    Mention,
    MerchantClue,
    Noise,
    Scenario,
    Split,
    Truth,
)
from pipeline.demand import DISPUTE_SUBCATEGORIES
from pipeline.silver import sql_str

SPLITS: tuple[Split, ...] = ("dev", "calibration", "test")
SPLIT_PREFIX = {"dev": "dev", "calibration": "cal", "test": "test"}
MARGINALS = ("country_code", "segment", "channel", "transaction_type")
CARDS = frozenset({"credit_card", "debit_card"})
ACCOUNTS = frozenset({"savings_account", "checking_account"})
OUT_OF_SCOPE_TOPICS = ("loan", "branch", "app", "personal_data")
# Categories drawn first are the ones with the fewest eligible transactions.
ORDER: tuple[Category, ...] = (
    "claim_status",
    "open_dispute",
    "approval_amount",
    "high_amount",
    "duplicate_pending",
    "duplicate_approved",
    "billing_amount",
    "no_match",
    "ambiguous",
    "fraud_card_lost",
    "recognized",
    "missing_data",
    "injection",
    "other_customer",
    "session_expired",
    "tool_failure",
    "multilingual",
    "out_of_scope",
    "normal",
)


@dataclass(frozen=True)
class Txn:
    """A disputable transaction of a cohort customer, as the generator reads it.

    Attributes:
        transaction_id: Id.
        customer_id: Owner.
        product_id: Product of the charge.
        ts: Local wall time, naive.
        day: Local date.
        country_code: Home country of the customer.
        segment: Customer segment.
        product_type: Normalized product type.
        transaction_type: Purchase, Payment or Withdrawal.
        status: Approved or Pending.
        channel: Channel.
        amount: Registered amount.
        currency: Registered currency.
        amount_usd: USD amount from the data, when present.
        merchant_name: Merchant, only on purchases.
        merchant_category: Merchant category.
        transaction_country: Country of the transaction.
        city: City of the transaction.
    """

    transaction_id: str
    customer_id: str
    product_id: str
    ts: datetime
    day: date
    country_code: str
    segment: str
    product_type: str
    transaction_type: str
    status: str
    channel: str
    amount: float
    currency: str
    amount_usd: float | None
    merchant_name: str | None
    merchant_category: str | None
    transaction_country: str | None
    city: str | None


@dataclass(frozen=True)
class Claim:
    """A dispute complaint of a cohort customer, with local times.

    Attributes:
        complaint_id: Id.
        customer_id: Owner.
        subcategory: Cargo no reconocido or Cobro indebido.
        created: Creation time.
        first_response: First response time.
        resolved: Resolution or closing time, whichever comes first.
        status: Status at the end of the data.
    """

    complaint_id: str
    customer_id: str
    subcategory: str
    created: datetime
    first_response: datetime | None
    resolved: datetime | None
    status: str

    def state_at(self, now: datetime) -> str | None:
        """State of the complaint at a given moment.

        Args:
            now: Case "now".

        Returns:
            None before creation; resolved, in_process or open after.
        """
        if self.created > now:
            return None
        if self.resolved is not None and self.resolved <= now:
            return "resolved"
        if self.first_response is not None and self.first_response <= now:
            return "in_process"
        return "open"


@dataclass
class Context:
    """Everything the generator reads, loaded once.

    Attributes:
        seed: Seed of the run.
        rules: Design values for the labels.
        noise: Noise configuration.
        presence: Joint presence of amount and product in dispute complaints.
        periods: First and last day of each split.
        buckets: Split of each cohort customer.
        customers: Cohort customer id to (country_code, segment).
        by_customer: Transactions of each customer, sorted by time.
        claims: Dispute complaints of each customer, sorted by creation.
        fx: (date, source, target) to exchange rate.
        local_currency: Local currency per home country.
        merchants: Every merchant name of the data, sorted.
    """

    seed: int
    rules: LabelRules
    noise: NoiseConfig
    presence: PresenceTable
    periods: dict[Split, tuple[date, date]]
    buckets: dict[str, Split]
    customers: dict[str, tuple[str, str]]
    by_customer: dict[str, list[Txn]]
    claims: dict[str, list[Claim]]
    fx: dict[tuple[date, str, str], float]
    local_currency: dict[str, str]
    merchants: list[str]
    _times: dict[str, list[datetime]] = field(default_factory=dict)

    def period_end(self, split: Split) -> datetime:
        """Returns the last moment a case of the split can happen.

        Args:
            split: Split name.

        Returns:
            23:59 of the last day of its period.
        """
        return datetime.combine(self.periods[split][1], time(23, 59))

    def rate(self, day: date, source: str, target: str) -> float | None:
        """Exchange rate of a day, or of the closest earlier day within a week.

        Args:
            day: Date of the transaction.
            source: Source currency.
            target: Target currency.

        Returns:
            The rate, or None when there is none in the last 7 days.
        """
        if source == target:
            return 1.0
        for back in range(8):
            r = self.fx.get((day - timedelta(days=back), source, target))
            if r is not None:
                return r
        return None

    def window(self, customer_id: str, now: datetime) -> list[Txn]:
        """Candidates of design 6.2: the customer's disputable transactions in the window.

        Args:
            customer_id: Customer of the case.
            now: Case "now".

        Returns:
            Transactions between now minus the dispute window and now, both included.
        """
        txns = self.by_customer.get(customer_id, [])
        if customer_id not in self._times:
            self._times[customer_id] = [t.ts for t in txns]
        times = self._times[customer_id]
        lo = bisect_left(times, now - timedelta(days=self.rules.dispute_window_days))
        return txns[lo : bisect_right(times, now)]

    def open_claims(self, customer_id: str, now: datetime) -> list[Claim]:
        """Dispute complaints open at `now` and created in the look-back of design 8.

        Rejected complaints carry no date, so they are never counted as open.

        Args:
            customer_id: Customer of the case.
            now: Case "now".

        Returns:
            The open complaints.
        """
        since = now - timedelta(days=self.rules.open_dispute_days)
        return [
            c
            for c in self.claims.get(customer_id, [])
            if since <= c.created <= now
            and c.status != "Rejected"
            and c.state_at(now) in ("open", "in_process")
        ]


def md5_int(*parts: object) -> int:
    """Stable integer from the md5 of the parts joined by ':'.

    Args:
        *parts: Values to hash.

    Returns:
        The digest as an integer.
    """
    return int(hashlib.md5(":".join(str(p) for p in parts).encode()).hexdigest(), 16)


def assign_buckets(
    customers: dict[str, tuple[str, str]], shares: dict[Split, float], seed: int
) -> dict[str, Split]:
    """Puts each customer in one split, in the same proportions inside every stratum.

    Args:
        customers: Customer id to (country_code, segment).
        shares: Share of customers per split, in dev, calibration, test order.
        seed: Seed of the run.

    Returns:
        Customer id to split.
    """
    strata: dict[tuple[str, str], list[str]] = defaultdict(list)
    for cid, stratum in customers.items():
        strata[stratum].append(cid)
    buckets: dict[str, Split] = {}
    for ids in strata.values():
        ids.sort(key=lambda c: md5_int(seed, "bucket", c))
        start = 0
        for i, split in enumerate(SPLITS):
            end = len(ids) if i == len(SPLITS) - 1 else start + round(shares[split] * len(ids))
            for cid in ids[start:end]:
                buckets[cid] = split
            start = end
    return buckets


def _src(folder: Path, name: str) -> str:
    return f"read_parquet({sql_str(str(folder / f'{name}.parquet'))})"


def presence_table(con: duckdb.DuckDBPyConnection, gold: Path) -> PresenceTable:
    """Joint share of claimed amount and affected product in the dispute complaints.

    Read from the whole population (gold/service_complaints), as in docs/reports/demanda.md.

    Args:
        con: DuckDB connection.
        gold: Gold directory.

    Returns:
        (amount present, product present) to share.
    """
    rows = con.execute(
        f"""
        SELECT claimed_amount IS NOT NULL, has_affected_product, count(*)
        FROM {_src(gold, "service_complaints")}
        WHERE subcategory IN {DISPUTE_SUBCATEGORIES}
        GROUP BY ALL
        """
    ).fetchall()
    total = sum(n for *_, n in rows)
    return {(bool(a), bool(p)): n / total for a, p, n in rows}


def load_context(
    con: duckdb.DuckDBPyConnection,
    gold: Path,
    config: dict[str, Any],
    local_currency: dict[str, str],
    seed: int,
) -> Context:
    """Reads the cohort, its disputable transactions, its dispute complaints and the rates.

    Args:
        con: DuckDB connection with its time zone set to UTC.
        gold: Gold directory; needs case_generator_input, service_complaints,
            service_exchange_rates and the cohort.
        config: Parsed config/cases.yaml.
        local_currency: Local currency per home country (config/normalization.yaml).
        seed: Seed of the run.

    Returns:
        The context.
    """
    cohort = gold / "cohort"
    customers = {
        cid: (country, segment)
        for cid, country, segment in con.execute(
            f"SELECT customer_id, country_code, segment FROM {_src(cohort, 'customers')}"
        ).fetchall()
    }
    periods: dict[Split, tuple[date, date]] = {
        s: (config["periods"][s]["start"], config["periods"][s]["end"]) for s in SPLITS
    }
    by_customer: dict[str, list[Txn]] = defaultdict(list)
    rows = con.execute(
        f"""
        SELECT transaction_id, customer_id, product_id,
               timezone(timezone, transaction_date) AS ts, event_date_local, country_code,
               segment, product_type, transaction_type, transaction_status, channel,
               amount::DOUBLE, currency, amount_usd::DOUBLE, merchant_name, merchant_category,
               transaction_country, transaction_city
        FROM {_src(gold, "case_generator_input")}
        WHERE customer_id IN (SELECT customer_id FROM {_src(cohort, "customers")})
        ORDER BY customer_id, ts, transaction_id
        """
    ).fetchall()
    for row in rows:
        by_customer[row[1]].append(Txn(*row))
    claims: dict[str, list[Claim]] = defaultdict(list)
    for row in con.execute(
        f"""
        SELECT complaint_id, customer_id, subcategory, creation_date, first_response_date,
               least(resolution_date, closing_date), status
        FROM {_src(cohort, "complaints")}
        WHERE subcategory IN {DISPUTE_SUBCATEGORIES}
        ORDER BY customer_id, creation_date, complaint_id
        """
    ).fetchall():
        claims[row[1]].append(Claim(*row))
    fx = {
        (d, s, t): r
        for d, s, t, r in con.execute(
            f"SELECT date, source_currency, target_currency, exchange_rate "
            f"FROM {_src(gold, 'service_exchange_rates')}"
        ).fetchall()
    }
    merchants = sorted(
        m
        for (m,) in con.execute(
            f"SELECT DISTINCT merchant_name FROM {_src(gold, 'case_generator_input')} "
            "WHERE merchant_name IS NOT NULL"
        ).fetchall()
    )
    return Context(
        seed=seed,
        rules=LabelRules.from_config(config),
        noise=NoiseConfig.from_config(config),
        presence=presence_table(con, gold),
        periods=periods,
        buckets=assign_buckets(customers, config["customer_shares"], seed),
        customers=customers,
        by_customer=dict(by_customer),
        claims=dict(claims),
        fx=fx,
        local_currency=local_currency,
        merchants=merchants,
    )


def truth_of(tx: Txn, ctx: Context, **extra: Any) -> Truth | None:
    """True values of a transaction, with its USD and local amounts.

    Args:
        tx: Source transaction.
        ctx: Generator context.
        **extra: Card possession, recognition and complaint fields.

    Returns:
        The truth, or None when an exchange rate is missing.
    """
    local = ctx.local_currency[tx.country_code]
    usd = tx.amount if tx.currency == "USD" else tx.amount_usd
    if usd is None:
        r = ctx.rate(tx.day, tx.currency, "USD")
        usd = None if r is None else tx.amount * r
    r_local = ctx.rate(tx.day, tx.currency, local)
    if usd is None or r_local is None:
        return None
    return Truth(
        transaction_id=tx.transaction_id,
        timestamp=tx.ts,
        country_code=tx.country_code,
        segment=tx.segment,
        product_type=tx.product_type,
        transaction_type=tx.transaction_type,
        status=tx.status,
        channel=tx.channel,
        amount=tx.amount,
        currency=tx.currency,
        amount_usd=round(usd, 2),
        local_amount=round(tx.amount * r_local, 2),
        local_currency=local,
        merchant_name=tx.merchant_name,
        merchant_category=tx.merchant_category,
        city=tx.city,
        transaction_country=tx.transaction_country,
        **extra,
    )


@dataclass(frozen=True)
class Spec:
    """What a category asks of its source transaction and what it must be labelled.

    Attributes:
        intent: Intent of the customer.
        action: Expected action every case of the category must get; None when it depends on
            the draw (card possession).
        products: Allowed product types; empty for any.
        purchase_only: Needs a purchase with a merchant.
        band: Required USD band.
    """

    intent: Intent
    action: str | None
    products: frozenset[str] = frozenset()
    purchase_only: bool = False
    band: str | None = "up_to_500"


SPECS: dict[Category, Spec] = {
    "normal": Spec("unrecognized_charge", "register_and_offer_block", CARDS),
    "fraud_card_lost": Spec("unrecognized_charge", "register_and_block", CARDS),
    "billing_amount": Spec("billing_error_amount", "register", CARDS | ACCOUNTS, True),
    "duplicate_pending": Spec("billing_error_duplicate", "explain_and_watch", CARDS, True),
    "duplicate_approved": Spec("billing_error_duplicate", "register", CARDS, True),
    "ambiguous": Spec("unrecognized_charge", None, CARDS),
    "recognized": Spec("unrecognized_charge", "recognized_closed", CARDS),
    "high_amount": Spec("unrecognized_charge", "escalate", CARDS, band="above_1000"),
    "approval_amount": Spec("unrecognized_charge", "analyst_approval", CARDS, band="500_to_1000"),
    "open_dispute": Spec("unrecognized_charge", "escalate", CARDS),
    "no_match": Spec("unrecognized_charge", "escalate", CARDS, True, band=None),
    "missing_data": Spec("unrecognized_charge", None, CARDS),
    "out_of_scope": Spec("out_of_scope", "abstain_and_redirect", band=None),
    "injection": Spec("unrecognized_charge", "security_blocked", CARDS),
    "other_customer": Spec("unrecognized_charge", "security_blocked", CARDS),
    "session_expired": Spec("unrecognized_charge", "expired", CARDS),
    "tool_failure": Spec("unrecognized_charge", "escalate", CARDS),
    "multilingual": Spec("unrecognized_charge", None, CARDS),
    "claim_status": Spec("claim_status", "report_claim_status", band=None),
}


def _eligible(tx: Txn, spec: Spec) -> bool:
    if spec.products and tx.product_type not in spec.products:
        return False
    if spec.purchase_only and (tx.transaction_type != "Purchase" or not tx.merchant_name):
        return False
    return not (spec.purchase_only and tx.status != "Approved")


def _silent_noise() -> Noise:
    return Noise(
        amount=AmountClue(mentioned=False, form=None, value=None, currency=None),
        date=DateClue(
            mentioned=False, form=None, expression=None, window_start=None, window_end=None
        ),
        merchant=MerchantClue(mentioned=False, form=None, value=None),
        channel=Mention(mentioned=False),
        product=Mention(mentioned=False),
        card_possession=Mention(mentioned=False),
    )


def _noise(
    category: Category, truth: Truth, now: datetime, rng: random.Random, ctx: Context
) -> Noise | None:
    cfg = ctx.noise
    assert truth.timestamp is not None
    amount_said, product_said = draw_presence(rng, ctx.presence)
    amount = draw_amount(truth, rng, cfg, amount_said)
    date_clue = draw_date(
        truth.timestamp.date(), now.date(), rng, cfg, rng.random() < cfg.date_mentioned
    )
    merchant = draw_merchant(truth, rng, cfg)
    channel_rate = (
        cfg.channel_mentioned if truth.merchant_name else (cfg.channel_mentioned_without_merchant)
    )
    channel = rng.random() < channel_rate
    card = truth.card_in_possession is not None and rng.random() < cfg.card_possession_mentioned
    if category == "billing_amount":
        amount = amount.model_copy(update={"mentioned": True})
    if category == "missing_data":
        amount = amount.model_copy(update={"mentioned": False})
        date_clue = date_clue.model_copy(update={"mentioned": False}) if date_clue else None
        merchant = merchant.model_copy(update={"mentioned": False})
        channel = False
    if category == "ambiguous":
        date_clue = draw_date(truth.timestamp.date(), now.date(), rng, cfg, True, wide_only=True)
        if date_clue is None:
            return None
        amount = amount.model_copy(update={"mentioned": False})
        if merchant.form is not None:
            merchant = MerchantClue(
                mentioned=rng.random() < 0.5, form="category", value=truth.merchant_category
            )
        channel = False
    assert date_clue is not None
    return Noise(
        amount=amount,
        date=date_clue,
        merchant=merchant,
        channel=Mention(mentioned=channel),
        product=Mention(mentioned=product_said),
        card_possession=Mention(mentioned=card),
    )


def _ambiguous_enough(truth: Truth, noise: Noise, candidates: list[Txn]) -> bool:
    """At least two candidates fit what the first message says."""
    start, end = noise.date.window_start, noise.date.window_end
    assert start is not None and end is not None
    fits = [
        t
        for t in candidates
        if start <= t.day <= end
        and (not noise.merchant.mentioned or t.merchant_category == truth.merchant_category)
    ]
    return len(fits) >= 2


def _no_match_noise(
    truth: Truth, noise: Noise, candidates: list[Txn], rng: random.Random, ctx: Context
) -> Noise | None:
    """A merchant and an amount that no candidate of the customer has."""
    assert truth.amount is not None and truth.currency is not None
    used = {t.merchant_name for t in candidates}
    unused = [m for m in ctx.merchants if m not in used]
    if not unused:
        return None
    merchant = rng.choice(unused)
    same_currency = [t.amount for t in candidates if t.currency == truth.currency]
    for factor in rng.sample((0.2, 0.3, 3.0, 4.0), 4):
        value = significant(truth.amount * factor, 2)
        if all(not (value * 0.5 <= a <= value * 2) for a in same_currency):
            amount = AmountClue(
                mentioned=True,
                form="rounded",
                value=value,
                currency=truth.currency,
                low=round(value * 0.9, 2),
                high=round(value * 1.1, 2),
            )
            return noise.model_copy(
                update={
                    "amount": amount,
                    "merchant": MerchantClue(mentioned=True, form="complete", value=merchant),
                }
            )
    return None


def _twin(tx: Txn, status: str, rng: random.Random) -> dict[str, Any]:
    """A constructed duplicate of a transaction, labelled as such."""
    ts = tx.ts + timedelta(minutes=rng.randint(2, 15))
    return {
        "constructed": True,
        "transaction_id": f"{tx.transaction_id}-DUP",
        "customer_id": tx.customer_id,
        "product_id": tx.product_id,
        "transaction_date": ts.isoformat(),
        "transaction_type": tx.transaction_type,
        "transaction_status": status,
        "channel": tx.channel,
        "amount": tx.amount,
        "currency": tx.currency,
        "amount_usd": tx.amount_usd,
        "merchant_name": tx.merchant_name,
        "merchant_category": tx.merchant_category,
        "transaction_country": tx.transaction_country,
        "transaction_city": tx.city,
    }


def build_base(
    category: Category,
    tx: Txn,
    split: Split,
    provenance: str,
    ctx: Context,
    base_id: str,
) -> BaseCase | None:
    """Builds one base case from a transaction, or None when it does not fit the category.

    Args:
        category: Case type.
        tx: Source transaction.
        split: Split of the case.
        provenance: generator_a, generator_b or handwritten.
        ctx: Generator context.
        base_id: Id of the base case; seeds its random source.

    Returns:
        The base case, or None.
    """
    spec = SPECS[category]
    if not _eligible(tx, spec):
        return None
    rng = random.Random(md5_int(ctx.seed, base_id))
    latest = ctx.period_end(split)
    if category == "open_dispute":
        # The complaint drives "now": it must be open then, and the charge must precede it.
        created = [
            c
            for c in ctx.claims.get(tx.customer_id, [])
            if ctx.periods[split][0] <= c.created.date() and c.created <= latest
        ]
        if not created:
            return None
        claim = rng.choice(created)
        now = max(tx.ts, claim.created) + timedelta(days=rng.randint(1, 20), minutes=30)
        if now > latest or now - tx.ts > timedelta(days=ctx.rules.dispute_window_days):
            return None
    else:
        now = draw_now(tx.ts, rng, ctx.noise, latest)
        if now is None:
            return None
    open_claims = len(ctx.open_claims(tx.customer_id, now))
    if (category == "open_dispute") != (open_claims > 0):
        return None

    unrecognized = spec.intent == "unrecognized_charge"
    possession: bool | None = None
    if category == "normal":
        possession = True
    elif category == "fraud_card_lost":
        possession = False
    elif unrecognized:
        possession = rng.random() < ctx.noise.card_in_possession
    truth = truth_of(
        tx,
        ctx,
        card_in_possession=possession,
        recognizes_after_detail=category == "recognized",
        open_dispute_claims=open_claims,
    )
    if truth is None or (spec.band and amount_band(truth.amount_usd, ctx.rules) != spec.band):
        return None

    candidates = ctx.window(tx.customer_id, now)
    scenario: dict[str, Any] = {}
    if category == "out_of_scope":
        noise = _silent_noise()
        scenario["topic"] = rng.choice(OUT_OF_SCOPE_TOPICS)
    else:
        drawn = _noise(category, truth, now, rng, ctx)
        if drawn is None:
            return None
        noise = drawn
    if category == "ambiguous":
        if not _ambiguous_enough(truth, noise, candidates):
            return None
        scenario["weak_clues"] = True
    elif category == "no_match":
        changed = _no_match_noise(truth, noise, candidates, rng, ctx)
        if changed is None:
            return None
        noise = changed
        scenario["nonexistent_charge"] = True
    elif category == "billing_amount":
        scenario["agreed_amount"] = significant(tx.amount * rng.uniform(0.5, 0.9), 2)
    elif category in ("duplicate_pending", "duplicate_approved"):
        twin = _twin(tx, "Pending" if category == "duplicate_pending" else "Approved", rng)
        if datetime.fromisoformat(twin["transaction_date"]) >= now:
            return None
        scenario["twin_transaction_id"] = twin["transaction_id"]
        scenario["fixture_rows"] = [twin]
    elif category == "injection":
        scenario["injection"] = True
    elif category == "other_customer":
        others = sorted(c for c in ctx.customers if c != tx.customer_id)
        scenario["other_customer_id"] = others[md5_int(ctx.seed, "other", base_id) % len(others)]
    elif category == "session_expired":
        scenario["session_expires_at_turn"] = 2
    elif category == "tool_failure":
        scenario["tool_failure"] = "register_dispute"
    elif category == "multilingual":
        scenario["code_mixed"] = True
    sc = Scenario(**scenario)
    expected = expected_action(spec.intent, truth, noise, sc, ctx.rules)
    if spec.action is not None and expected.action != spec.action:
        return None
    return BaseCase(
        base_id=base_id,
        split=split,
        provenance=provenance,  # type: ignore[arg-type]
        category=category,
        intent=spec.intent,
        customer_id=tx.customer_id,
        now=now,
        truth=truth,
        noise=noise,
        scenario=sc,
        expected=expected,
    )


def build_claim_base(
    claim: Claim, split: Split, provenance: str, ctx: Context, base_id: str
) -> BaseCase | None:
    """Builds a claim status case from a real dispute complaint of the cohort.

    It is the one category born from a complaint instead of a transaction (design 3.1).

    Args:
        claim: Source complaint.
        split: Split of the case.
        provenance: generator_a, generator_b or handwritten.
        ctx: Generator context.
        base_id: Id of the base case; seeds its random source.

    Returns:
        The base case, or None when the complaint is not open at the drawn "now".
    """
    rng = random.Random(md5_int(ctx.seed, base_id))
    now = claim.created + timedelta(days=rng.randint(1, 45), hours=rng.randint(1, 8))
    if now > ctx.period_end(split) or claim.state_at(now) == "resolved":
        return None
    country, segment = ctx.customers[claim.customer_id]
    open_claims = ctx.open_claims(claim.customer_id, now)
    if not open_claims:
        return None
    truth = Truth(
        transaction_id=None,
        complaint_id=claim.complaint_id,
        timestamp=None,
        country_code=country,
        segment=segment,
        product_type=None,
        transaction_type=None,
        status=None,
        channel=None,
        amount=None,
        currency=None,
        amount_usd=None,
        local_amount=None,
        local_currency=ctx.local_currency[country],
        merchant_name=None,
        merchant_category=None,
        city=None,
        transaction_country=None,
        claim_subcategory=claim.subcategory,
        claim_created=claim.created,
        claim_state=claim.state_at(now),
        open_dispute_claims=len(open_claims),
    )
    noise = _silent_noise()
    sc = Scenario()
    return BaseCase(
        base_id=base_id,
        split=split,
        provenance=provenance,  # type: ignore[arg-type]
        category="claim_status",
        intent="claim_status",
        customer_id=claim.customer_id,
        now=now,
        truth=truth,
        noise=noise,
        scenario=sc,
        expected=expected_action("claim_status", truth, noise, sc, ctx.rules),
    )


def split_pool(ctx: Context, split: Split) -> list[Txn]:
    """Transactions a split may use: its customers, inside its period.

    Args:
        ctx: Generator context.
        split: Split name.

    Returns:
        The pool, in customer and time order.
    """
    start, end = ctx.periods[split]
    return [
        t
        for cid, txns in ctx.by_customer.items()
        if ctx.buckets.get(cid) == split
        for t in txns
        if start <= t.day <= end
    ]


def marginal_quotas(pool: Iterable[Txn], size: int) -> dict[str, dict[str, int]]:
    """Proportional quota of every value of every marginal, plus one of slack.

    Args:
        pool: Transactions of the split.
        size: Base cases that come from a transaction.

    Returns:
        Marginal name to value to the most cases it may take in the first pass.
    """
    counts = {m: Counter(getattr(t, m) for t in pool) for m in MARGINALS}
    quotas = {}
    for m, c in counts.items():
        total = sum(c.values())
        quotas[m] = {v: n * size // total + 1 for v, n in c.items()}
    return quotas


@dataclass
class SplitSample:
    """Base cases of one split and the marginals they reached.

    Attributes:
        bases: Base cases, in category then acceptance order.
        marginals: Marginal name to value to count, over cases with a transaction.
        population: Same, over the split's pool.
        second_pass: Categories that needed the pass without quotas.
    """

    bases: list[BaseCase]
    marginals: dict[str, Counter[str]]
    population: dict[str, Counter[str]]
    second_pass: list[str]


def sample_split(
    ctx: Context,
    split: Split,
    mix: dict[str, int],
    provenance: str,
    exclude: Iterable[str] = (),
    taken: Iterable[BaseCase] = (),
) -> SplitSample:
    """Draws the base cases of a split.

    Args:
        ctx: Generator context.
        split: Split name.
        mix: Base cases per category.
        provenance: Provenance of the drawn cases.
        exclude: Customers already used (the handwritten cases of test).
        taken: Base cases already in the split; they count toward the marginals.

    Returns:
        The drawn base cases.

    Raises:
        ValueError: When a category cannot be filled.
    """
    pool = split_pool(ctx, split)
    used = set(exclude)
    taken = list(taken)
    tx_cases = sum(n for c, n in mix.items() if c != "claim_status") + sum(
        1 for b in taken if b.truth.transaction_id
    )
    quotas = marginal_quotas(pool, tx_cases)
    reached: dict[str, Counter[str]] = {m: Counter() for m in MARGINALS}
    for b in taken:
        if b.truth.transaction_id:
            for m in MARGINALS:
                reached[m][_marginal(b, m)] += 1
    prefix = SPLIT_PREFIX[split]
    bases: list[BaseCase] = []
    second: list[str] = []

    def base_id(category: str, key: str) -> str:
        return f"{prefix}-{md5_int(ctx.seed, split, category, key) % 16**10:010x}"

    for category in ORDER:
        n = mix.get(category, 0)
        if not n:
            continue
        got: list[BaseCase] = []
        if category == "claim_status":
            claims = sorted(
                (
                    c
                    for cid, cs in ctx.claims.items()
                    if ctx.buckets.get(cid) == split
                    for c in cs
                    if ctx.periods[split][0] <= c.created.date() <= ctx.periods[split][1]
                ),
                key=lambda c: md5_int(ctx.seed, split, category, c.complaint_id),
            )
            for c in claims:
                if len(got) == n:
                    break
                if c.customer_id in used:
                    continue
                b = build_claim_base(c, split, provenance, ctx, base_id(category, c.complaint_id))
                if b is not None:
                    got.append(b)
                    used.add(c.customer_id)
        else:
            order = sorted(pool, key=lambda t: md5_int(ctx.seed, split, category, t.transaction_id))
            for quota_pass in (True, False):
                for t in order:
                    if len(got) == n:
                        break
                    if t.customer_id in used:
                        continue
                    if quota_pass and any(
                        reached[m][getattr(t, m)] + 1 > quotas[m][getattr(t, m)] for m in MARGINALS
                    ):
                        continue
                    b = build_base(
                        category, t, split, provenance, ctx, base_id(category, t.transaction_id)
                    )
                    if b is None:
                        continue
                    got.append(b)
                    used.add(t.customer_id)
                    for m in MARGINALS:
                        reached[m][getattr(t, m)] += 1
                if len(got) == n:
                    break
                second.append(category)
        if len(got) < n:
            raise ValueError(f"{split}: only {len(got)} of {n} {category} cases could be built")
        bases.extend(got)
    population = {m: Counter(getattr(t, m) for t in pool) for m in MARGINALS}
    return SplitSample(bases, reached, population, second)


def _marginal(b: BaseCase, m: str) -> str:
    return {
        "country_code": b.truth.country_code,
        "segment": b.truth.segment,
        "channel": b.truth.channel,
        "transaction_type": b.truth.transaction_type,
    }[m] or ""

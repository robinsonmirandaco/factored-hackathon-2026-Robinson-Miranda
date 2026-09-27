"""Noise model (TRZ-42 CA2, design 6.3): how a customer states each clue of a real transaction.

Draws are language-neutral: a date is a key such as "last_week" with the window it can mean, not
a Spanish or Portuguese phrase; the templates of each variant turn keys into words. Every clue
gets a form and a value even when it is not mentioned, so the simulated client (TRZ-43) can give
it when asked.

Whether the amount and the product are mentioned is drawn from the joint table measured on the
dispute complaints (`presence_table`); every other rate is a declared assumption in
config/cases.yaml.
"""

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from pipeline.cases.schema import AmountClue, DateClue, MerchantClue, Truth

# Presence of (amount, product) in a dispute complaint -> share.
PresenceTable = dict[tuple[bool, bool], float]

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# Relative expressions that cover more than a couple of days: the ones an ambiguous case uses.
WIDE_EXPRESSIONS = ("this_week", "last_week", "two_weeks_ago", "last_month")


@dataclass(frozen=True)
class NoiseConfig:
    """The `noise` block of config/cases.yaml.

    Attributes:
        amount_form: Share of exact, rounded and approximate amounts.
        approximate_more_than: Share of "more than" among approximate amounts.
        amount_local_currency: Share of amounts said in local currency when it differs.
        date_mentioned: Share of messages with a date.
        date_form: Share of exact and relative dates.
        merchant_mentioned: Share of purchases whose merchant is mentioned.
        merchant_form: Share of complete, partial, misspelled and category-only merchants.
        channel_mentioned: Share of messages with the channel when there is a merchant.
        channel_mentioned_without_merchant: Same, when the transaction has no merchant.
        card_possession_mentioned: Share of unrecognized charges that say where the card is.
        card_in_possession: Share of unrecognized charges whose customer has the card.
        delay_days: (low, high, weight) of days from the transaction to the message.
        message_hours: First and last local hour of a message.
    """

    amount_form: dict[str, float]
    approximate_more_than: float
    amount_local_currency: float
    date_mentioned: float
    date_form: dict[str, float]
    merchant_mentioned: float
    merchant_form: dict[str, float]
    channel_mentioned: float
    channel_mentioned_without_merchant: float
    card_possession_mentioned: float
    card_in_possession: float
    delay_days: list[tuple[int, int, float]]
    message_hours: tuple[int, int]

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "NoiseConfig":
        """Builds the configuration from parsed config/cases.yaml.

        Args:
            config: Parsed config/cases.yaml.

        Returns:
            The noise configuration.
        """
        n = config["noise"]
        return cls(
            amount_form=dict(n["amount_form"]),
            approximate_more_than=float(n["approximate_more_than"]),
            amount_local_currency=float(n["amount_local_currency"]),
            date_mentioned=float(n["date_mentioned"]),
            date_form=dict(n["date_form"]),
            merchant_mentioned=float(n["merchant_mentioned"]),
            merchant_form=dict(n["merchant_form"]),
            channel_mentioned=float(n["channel_mentioned"]),
            channel_mentioned_without_merchant=float(n["channel_mentioned_without_merchant"]),
            card_possession_mentioned=float(n["card_possession_mentioned"]),
            card_in_possession=float(n["card_in_possession"]),
            delay_days=[(int(lo), int(hi), float(w)) for lo, hi, w in n["delay_days"]],
            message_hours=(int(n["message_hours"][0]), int(n["message_hours"][1])),
        )


def pick(rng: random.Random, weights: dict[str, float]) -> str:
    """Draws a key with the given weights, in the order of the mapping.

    Args:
        rng: Random source of the case.
        weights: Key to weight.

    Returns:
        The drawn key.
    """
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys])[0]


def draw_presence(rng: random.Random, table: PresenceTable) -> tuple[bool, bool]:
    """Draws whether the amount and the product are mentioned, jointly.

    Args:
        rng: Random source of the case.
        table: Share of each (amount, product) cell in the dispute complaints.

    Returns:
        (amount mentioned, product mentioned).
    """
    cells = sorted(table)
    return rng.choices(cells, weights=[table[c] for c in cells])[0]


def significant(value: float, digits: int, floor: bool = False) -> float:
    """Rounds a positive value to a number of significant digits.

    Args:
        value: Positive amount.
        digits: Significant digits to keep.
        floor: Round down instead of to the nearest.

    Returns:
        The rounded value.
    """
    step = 10 ** (math.floor(math.log10(value)) - digits + 1)
    return float((math.floor(value / step) if floor else round(value / step)) * step)


def draw_now(
    ts: datetime, rng: random.Random, cfg: NoiseConfig, latest: datetime
) -> datetime | None:
    """Draws when the customer writes, after the transaction.

    Args:
        ts: Local time of the transaction.
        rng: Random source of the case.
        cfg: Noise configuration.
        latest: Last allowed "now" (end of the split's period).

    Returns:
        The local "now" of the case, or None when it would fall after `latest`.
    """
    lo, hi, _ = rng.choices(cfg.delay_days, weights=[w for *_, w in cfg.delay_days])[0]
    days = rng.randint(lo, hi)
    first, last = cfg.message_hours
    if days == 0:
        now = ts + timedelta(minutes=rng.randint(30, 360))
    else:
        day = ts.date() + timedelta(days=days)
        now = datetime(day.year, day.month, day.day, rng.randint(first, last), rng.randint(0, 59))
    return now if now <= latest else None


def relative_windows(today: date) -> dict[str, tuple[date, date]]:
    """Dates each relative expression can mean when said on a given day.

    "El martes pasado" is read as either of the last two Tuesdays, since speakers disagree.

    Args:
        today: Local date of the case "now".

    Returns:
        Expression key to (first, last) date.
    """
    monday = today - timedelta(days=today.weekday())
    first_of_month = today.replace(day=1)
    last_of_prev = first_of_month - timedelta(days=1)
    windows = {
        "today": (today, today),
        "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
        "day_before_yesterday": (today - timedelta(days=2), today - timedelta(days=2)),
        "this_week": (monday, today),
        "last_week": (monday - timedelta(days=7), monday - timedelta(days=1)),
        "two_weeks_ago": (today - timedelta(days=18), today - timedelta(days=10)),
        "last_month": (last_of_prev.replace(day=1), last_of_prev),
    }
    if today.day > 12:
        windows["early_this_month"] = (first_of_month, first_of_month + timedelta(days=9))
    for back in range(3, 8):
        day = today - timedelta(days=back)
        windows[f"last_{WEEKDAYS[day.weekday()]}"] = (day - timedelta(days=7), day)
    return windows


def draw_date(
    tx_day: date,
    today: date,
    rng: random.Random,
    cfg: NoiseConfig,
    mentioned: bool,
    wide_only: bool = False,
) -> DateClue | None:
    """Draws how the customer states the date of the transaction.

    Only expressions whose window contains the true date are eligible, so the window is never a
    lie. A relative form with no eligible expression falls back to the exact date.

    Args:
        tx_day: Local date of the transaction.
        today: Local date of the case "now".
        rng: Random source of the case.
        cfg: Noise configuration.
        mentioned: Whether the date is in the first message.
        wide_only: Use only the expressions that span several days (ambiguous cases).

    Returns:
        The date clue, or None when `wide_only` finds no eligible expression.
    """
    windows = relative_windows(today)
    eligible = sorted(
        k
        for k, (a, b) in windows.items()
        if a <= tx_day <= b and (not wide_only or k in WIDE_EXPRESSIONS)
    )
    if wide_only:
        if not eligible:
            return None
        key = rng.choice(eligible)
        return DateClue(
            mentioned=mentioned,
            form="relative",
            expression=key,
            window_start=windows[key][0],
            window_end=windows[key][1],
        )
    if pick(rng, cfg.date_form) == "relative" and eligible:
        key = rng.choice(eligible)
        start, end = windows[key]
        return DateClue(
            mentioned=mentioned, form="relative", expression=key, window_start=start, window_end=end
        )
    return DateClue(
        mentioned=mentioned,
        form="exact",
        expression="exact",
        window_start=tx_day,
        window_end=tx_day,
    )


def draw_amount(truth: Truth, rng: random.Random, cfg: NoiseConfig, mentioned: bool) -> AmountClue:
    """Draws how the customer states the amount.

    Exact amounts are in the registered currency. Rounded keeps two significant digits;
    approximate keeps one, with "about" or "more than". `low` and `high` bound the true amount,
    in the stated currency, that the statement is compatible with.

    Args:
        truth: True values; needs amount, currency and local_amount.
        rng: Random source of the case.
        cfg: Noise configuration.
        mentioned: Whether the amount is in the first message.

    Returns:
        The amount clue.
    """
    assert truth.amount is not None and truth.currency is not None
    form = pick(rng, cfg.amount_form)
    amount, currency = truth.amount, truth.currency
    if (
        form != "exact"
        and truth.local_currency != truth.currency
        and truth.local_amount is not None
        and rng.random() < cfg.amount_local_currency
    ):
        amount, currency = truth.local_amount, truth.local_currency
    if form == "exact":
        return AmountClue(
            mentioned=mentioned,
            form="exact",
            value=round(amount, 2),
            currency=currency,
            low=round(amount * 0.99, 2),
            high=round(amount * 1.01, 2),
        )
    if form == "rounded":
        value = significant(amount, 2)
        return AmountClue(
            mentioned=mentioned,
            form="rounded",
            value=value,
            currency=currency,
            low=round(value * 0.9, 2),
            high=round(value * 1.1, 2),
        )
    if rng.random() < cfg.approximate_more_than:
        value = significant(amount, 1, floor=True)
        return AmountClue(
            mentioned=mentioned,
            form="approximate",
            value=value,
            currency=currency,
            qualifier="more_than",
            low=value,
            high=value * 2,
        )
    value = significant(amount, 1)
    return AmountClue(
        mentioned=mentioned,
        form="approximate",
        value=value,
        currency=currency,
        qualifier="about",
        low=round(value * 0.66, 2),
        high=value * 1.5,
    )


def partial_name(name: str, rng: random.Random) -> str:
    """Part of a merchant name, as a customer who half remembers it would write it.

    Args:
        name: Merchant name.
        rng: Random source of the case.

    Returns:
        One word of four letters or more, or the first 70% of a one-word name.
    """
    words = [w for w in name.split() if len(w) >= 4]
    if len(name.split()) > 1 and words:
        return rng.choice(words)
    return name[: max(3, math.ceil(len(name) * 0.7))]


def misspell(name: str, rng: random.Random) -> str:
    """One typo in a merchant name: two letters swapped, one dropped or one doubled.

    Args:
        name: Merchant name.
        rng: Random source of the case.

    Returns:
        A misspelling that differs from the name.
    """
    inner = [i for i in range(1, len(name) - 1) if name[i].isalpha() and name[i + 1].isalpha()]
    i = rng.choice(inner)
    op = rng.choice(("swap", "drop", "double"))
    if op == "swap" and name[i] != name[i + 1]:
        return name[:i] + name[i + 1] + name[i] + name[i + 2 :]
    if op == "double":
        return name[: i + 1] + name[i] + name[i + 1 :]
    return name[:i] + name[i + 1 :]


def draw_merchant(truth: Truth, rng: random.Random, cfg: NoiseConfig) -> MerchantClue:
    """Draws how the customer names the merchant.

    Args:
        truth: True values.
        rng: Random source of the case.
        cfg: Noise configuration.

    Returns:
        The merchant clue; without a merchant, not mentioned and with no form.
    """
    if not truth.merchant_name:
        return MerchantClue(mentioned=False, form=None, value=None)
    mentioned = rng.random() < cfg.merchant_mentioned
    form = pick(rng, cfg.merchant_form)
    if form == "category" and not truth.merchant_category:
        form = "complete"
    value = {
        "complete": lambda: truth.merchant_name,
        "partial": lambda: partial_name(truth.merchant_name or "", rng),
        "misspelled": lambda: misspell(truth.merchant_name or "", rng),
        "category": lambda: truth.merchant_category,
    }[form]()
    return MerchantClue(mentioned=mentioned, form=form, value=value)  # type: ignore[arg-type]

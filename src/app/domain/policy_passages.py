"""Demo policy passages and the deadlines they back.

A deadline is only stated when a passage backs it. Otherwise the answer is `Unsupported`, and
the caller must not assert a policy rule to the customer but offer a person instead.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

from app.domain.business_days import HolidayCalendar, OutsideCalendarError


@dataclass(frozen=True)
class Passage:
    """One citable passage of the demo policy.

    Attributes:
        id: Citation such as "§2.1".
        rule: Key the code looks the passage up by.
        label: Demo-policy label per language.
        text: Passage text per language.
        business_days: Deadline in business days, if the passage sets one.
        calendar_days: Deadline in calendar days, if the passage sets one.
    """

    id: str
    rule: str
    label: dict[str, str]
    text: dict[str, str]
    business_days: int | None = None
    calendar_days: int | None = None


@dataclass(frozen=True)
class PolicyDeadline:
    """A deadline together with the passage that backs it.

    Attributes:
        due: Last day of the deadline.
        passage_id: Citation of the backing passage.
        label: Demo-policy label in the customer's language.
        text: Passage text in the customer's language.
    """

    due: date
    passage_id: str
    label: str
    text: str


@dataclass(frozen=True)
class Unsupported:
    """No passage backs the requested rule, so nothing may be stated.

    Attributes:
        reason: Why there is no backing, for the audit log.
    """

    reason: str


def load_passages(path: str | Path) -> dict[str, Passage]:
    """Loads the demo policy passages.

    Args:
        path: YAML file with `label` and `passages`.

    Returns:
        Passages keyed by rule.

    Raises:
        ValueError: If two passages share a rule or an id.
    """
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    passages = [
        Passage(
            id=p["id"],
            rule=p["rule"],
            label=raw["label"],
            text=p["text"],
            business_days=p.get("business_days"),
            calendar_days=p.get("calendar_days"),
        )
        for p in raw["passages"]
    ]
    by_rule = {p.rule: p for p in passages}
    if len(by_rule) != len(passages) or len({p.id for p in passages}) != len(passages):
        raise ValueError(f"{path}: duplicate passage rule or id")
    return by_rule


def policy_deadline(
    passages: dict[str, Passage],
    calendars: dict[str, HolidayCalendar],
    rule: str,
    start: date,
    country: str,
    language: str,
) -> PolicyDeadline | Unsupported:
    """Computes the deadline a passage sets, counted from `start`.

    Args:
        passages: Passages keyed by rule.
        calendars: Holiday calendars keyed by country code.
        rule: Rule to look up, such as "response_deadline".
        start: Day the deadline counts from (not counted itself).
        country: Country of the customer, which sets the bank holidays.
        language: Language of the reply, "es" or "pt".

    Returns:
        The deadline with its citation, or Unsupported when no passage backs it.
    """
    passage = passages.get(rule)
    if passage is None:
        return Unsupported(f"no passage for rule {rule!r}")
    if language not in passage.text or language not in passage.label:
        return Unsupported(f"passage {passage.id} has no {language!r} text")
    if passage.business_days is not None:
        calendar = calendars.get(country)
        if calendar is None:
            return Unsupported(f"no bank holiday calendar for {country!r}")
        try:
            due = calendar.add_business_days(start, passage.business_days)
        except OutsideCalendarError as e:
            return Unsupported(str(e))
    elif passage.calendar_days is not None:
        due = start + timedelta(days=passage.calendar_days)
    else:
        return Unsupported(f"passage {passage.id} sets no deadline")
    return PolicyDeadline(
        due=due, passage_id=passage.id, label=passage.label[language], text=passage.text[language]
    )

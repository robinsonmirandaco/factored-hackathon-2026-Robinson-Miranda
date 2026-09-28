"""What a reply may state, and the check before it is sent (TRZ-20, design 6.5).

The verified facts of a turn come from the records read, the actions whose read-back matched
(TRZ-19) and the policy passages that back the turn. The LLM never sees the card digits nor
the passage text: the deadline and its citation, or the offer of a person when no passage backs
a deadline, are written by code after the LLM text.
"""

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import Product, Transaction
from app.domain.fact_check import Claim, VerifiedFacts, amount_fact, unsupported
from app.domain.policy_passages import Passage
from app.domain.recognition import MONTHS

_NOTES: dict[str, dict[str, str]] = {
    "es": {
        "deadline": (
            "Plazo de respuesta: a más tardar el {due}, {days} {unit} según el {passage} ({label})."
        ),
        "business": "días hábiles",
        "calendar": "días naturales",
        "no_deadline": (
            "No tengo un plazo de respuesta respaldado por la política para esta aclaración. "
            "Si quieres, te comunico con una persona."
        ),
    },
    "pt": {
        "deadline": ("Prazo de resposta: até {due}, {days} {unit} conforme o {passage} ({label})."),
        "business": "dias úteis",
        "calendar": "dias corridos",
        "no_deadline": (
            "Não tenho um prazo de resposta respaldado pela política para esta contestação. "
            "Se quiser, eu coloco você em contato com uma pessoa."
        ),
    },
}


def deadline_note(facts: dict[str, Any], passages: dict[str, Passage], language: str) -> str:
    """The deadline of a verified registration with its citation, or the offer of a person.

    Args:
        facts: Facts of the turn.
        passages: Demo policy passages by rule.
        language: Reply language.

    Returns:
        The note, or "" when the turn registered nothing.
    """
    if facts.get("outcome") != "registered_verified":
        return ""
    notes = _NOTES["pt" if language == "pt" else "es"]
    dispute = facts.get("dispute") or {}
    passage = _passage(passages, dispute.get("passage"))
    if not dispute.get("due_date") or passage is None or language not in passage.label:
        # No passage backs a deadline: none is stated, and a person is offered (TRZ-20 CA7).
        return notes["no_deadline"]
    return notes["deadline"].format(
        due=long_date(date.fromisoformat(dispute["due_date"]), language),
        days=passage.business_days or passage.calendar_days,
        unit=notes["business" if passage.business_days else "calendar"],
        passage=passage.id,
        label=passage.label[language],
    )


def long_date(day: date, language: str) -> str:
    """A date as the customer reads it: "8 de julio de 2026", "8 de julho de 2026".

    Args:
        day: The date.
        language: "pt" for Portuguese, anything else for Spanish.

    Returns:
        The date in words.
    """
    months = MONTHS["pt" if language == "pt" else "es"]
    return f"{day.day} de {months[day.month - 1]} de {day.year}"


def verified_facts(
    session: Session, customer_id: str, facts: dict[str, Any], passages: dict[str, Passage]
) -> VerifiedFacts:
    """Builds the verified facts of a turn (TRZ-20 CA1).

    Args:
        session: Open session bound to the customer of the JWT.
        customer_id: Customer of the session.
        facts: Facts of the turn, as decided by code.
        passages: Demo policy passages by rule.

    Returns:
        What a reply of this turn may state.
    """
    amounts, dates, merchants = set(), set(), set()
    records = [facts.get("transaction"), *facts.get("options", [])]
    for record in (r for r in records if r):
        amounts.add(amount_fact(record["amount"]))
        converted = (record.get("amount_display") or {}).get("converted_amount")
        if converted is not None:
            amounts.add(amount_fact(converted))
        dates.add(date.fromisoformat(record["date"]))
        if record.get("merchant"):
            merchants.add(str(record["merchant"]).casefold())
    folios, cited, deadlines = set(), set(), set()
    # Set only when the read-back of the registration matched (TRZ-19).
    dispute = facts.get("dispute") or {}
    if dispute:
        folios.add(dispute["folio"])
        dates.add(date.fromisoformat(dispute["registered_on"]))
        passage = _passage(passages, dispute.get("passage"))
        if dispute.get("due_date") and passage is not None:
            dates.add(date.fromisoformat(dispute["due_date"]))
            cited.add(passage.id)
            deadlines.add(passage.business_days or passage.calendar_days or 0)
    tx = (facts.get("transaction") or {}).get("transaction_id")
    last4 = (
        session.execute(
            select(Product.product_number_last4)
            .join(Transaction, Transaction.product_id == Product.product_id)
            .where(Transaction.transaction_id == tx)
        ).scalar_one_or_none()
        if tx
        else None
    )
    known = session.execute(
        select(Transaction.merchant_name)
        .where(Transaction.customer_id == customer_id, Transaction.merchant_name.is_not(None))
        .distinct()
    ).scalars()
    return VerifiedFacts(
        amounts=frozenset(amounts),
        dates=frozenset(dates),
        folios=frozenset(folios),
        passages=frozenset(cited),
        deadlines=frozenset(deadlines),
        last4=frozenset({last4} if last4 else set()),
        merchants=frozenset(merchants),
        known_merchants=frozenset(str(m).casefold() for m in known),
        actions=frozenset(facts.get("actions_taken", [])),
        counts=frozenset({len(facts["options"])} if facts.get("options") else set()),
    )


def check_reply(
    session: Session,
    case_id: str,
    text: str,
    verified: VerifiedFacts,
    enforce: bool,
) -> bool:
    """Checks a reply written by the LLM and records the check (TRZ-20 CA3, CA4, CA6).

    Args:
        session: Open database session.
        case_id: Case of the turn.
        text: Final reply, the LLM text with the note written by code.
        verified: Verified facts of the turn.
        enforce: False for the ablation: unsupported claims are counted but the text is sent.

    Returns:
        True when the text may be sent to the customer.
    """
    found = unsupported(text, verified)
    sent = not found or not enforce
    write_audit(
        session,
        "agent",
        "fact_check",
        case_id,
        {"mode": "enforce" if enforce else "observe"},
        {
            "passed": not found,
            "sent": sent,
            "unsupported": [_audited(c) for c in found],
        },
    )
    return sent


def _audited(c: Claim) -> dict[str, str]:
    # Card digits, or a long run of digits that may be a card number, never reach the audit log.
    digits = c.kind == "card_digits" or (c.kind == "number" and len(c.value) >= 4)
    return {"kind": c.kind, "value": "[DIGITS]" if digits else c.value}


def _passage(passages: dict[str, Passage], passage_id: str | None) -> Passage | None:
    return next((p for p in passages.values() if p.id == passage_id), None)

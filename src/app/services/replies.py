"""What a reply may state, and the check before it is sent (TRZ-20, design 6.5).

The verified facts of a turn come from the records read (charges and claims), the actions whose
read-back matched (TRZ-19) and the policy passages that back the turn. The LLM never sees the
card digits nor the passage text: the deadline and its citation, or the offer of a person when
no passage backs a deadline, are written by code after the LLM text.
"""

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import Dispute, Product, Transaction
from app.domain.fact_check import Claim, VerifiedFacts, amount_fact, unsupported
from app.domain.policy_passages import Passage
from app.domain.recognition import long_date

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
        "claim_status": (
            "Tu reclamo {claim_id}, abierto el {opened}, está {status}. Último paso: {step}, "
            "el {step_on}."
        ),
        "claim_source": "Fuente: registro del reclamo {claim_id}.",
        "claim_overdue": (
            "El plazo de respuesta venció el {due}: {days} {unit} según el {passage} ({label})."
        ),
        "claim_no_deadline": (
            "No tengo un plazo de respuesta respaldado por la política para este reclamo. "
            "Si quieres, te comunico con una persona."
        ),
        "person": "Si quieres, te comunico con una persona.",
    },
    "pt": {
        "deadline": ("Prazo de resposta: até {due}, {days} {unit} conforme o {passage} ({label})."),
        "business": "dias úteis",
        "calendar": "dias corridos",
        "no_deadline": (
            "Não tenho um prazo de resposta respaldado pela política para esta contestação. "
            "Se quiser, eu coloco você em contato com uma pessoa."
        ),
        "claim_status": (
            "A sua reclamação {claim_id}, aberta em {opened}, está {status}. Última etapa: "
            "{step}, em {step_on}."
        ),
        "claim_source": "Fonte: registro da reclamação {claim_id}.",
        "claim_overdue": (
            "O prazo de resposta venceu em {due}: {days} {unit} conforme o {passage} ({label})."
        ),
        "claim_no_deadline": (
            "Não tenho um prazo de resposta respaldado pela política para esta reclamação. "
            "Se quiser, eu coloco você em contato com uma pessoa."
        ),
        "person": "Se quiser, eu coloco você em contato com uma pessoa.",
    },
}


# The closed vocabulary of a claim's status and last step: the LLM never words them (TRZ-22).
STATUS_WORDS: dict[str, dict[str, str]] = {
    "es": {
        "received": "recibido",
        "in_review": "en revisión",
        "answered": "con una primera respuesta del banco, pendiente de resolución",
        "created": "su creación",
        "assigned": "la asignación a una analista",
        "first_response": "la primera respuesta del banco",
        "registered": "el registro de la aclaración",
    },
    "pt": {
        "received": "recebida",
        "in_review": "em análise",
        "answered": "com uma primeira resposta do banco, aguardando solução",
        "created": "a abertura",
        "assigned": "a atribuição a uma analista",
        "first_response": "a primeira resposta do banco",
        "registered": "o registro da contestação",
    },
}


def deadline_note(facts: dict[str, Any], passages: dict[str, Passage], language: str) -> str:
    """The deadline of a verified registration with its citation; for the claim reported, its
    status, last step, source and deadline; or the offer of a person when no passage backs it.

    Args:
        facts: Facts of the turn.
        passages: Demo policy passages by rule.
        language: Reply language.

    Returns:
        The note, or "" when the turn registered nothing and reported no claim.
    """
    notes = _NOTES["pt" if language == "pt" else "es"]
    if facts.get("outcome") == "informed" and facts.get("action") == "report_claim_status":
        return _claim_note(facts, passages, language, notes)
    if facts.get("outcome") != "registered_verified":
        return ""
    dispute = facts.get("dispute") or {}
    passage = _passage(passages, dispute.get("passage"))
    if not dispute.get("due_date") or passage is None or language not in passage.label:
        # No passage backs a deadline: none is stated, and a person is offered (TRZ-20 CA7).
        return notes["no_deadline"]
    return _deadline_text(notes["deadline"], dispute["due_date"], passage, language, notes)


def _claim_note(
    facts: dict[str, Any], passages: dict[str, Passage], language: str, notes: dict[str, str]
) -> str:
    """Status, last step, source and deadline of the claim reported (TRZ-22 CA2, CA4 and CA6),
    in the closed vocabulary of each status."""
    claim = facts.get("claim")
    if claim is None:
        # None of the claims shown is the one the customer means.
        return notes["person"] if facts.get("other_claim") else ""
    lang = "pt" if language == "pt" else "es"
    words = STATUS_WORDS[lang]
    status = notes["claim_status"].format(
        claim_id=claim["claim_id"],
        opened=long_date(date.fromisoformat(claim["opened_on"]), lang),
        status=words[claim["status"]],
        step=words[claim["last_step"]["step"]],
        step_on=long_date(date.fromisoformat(claim["last_step"]["on"]), lang),
    )
    source = f"{status} " + notes["claim_source"].format(claim_id=claim["claim_id"])
    passage = _passage(passages, claim.get("passage_id"))
    if not claim.get("due_date") or passage is None or language not in passage.label:
        return f"{source} {notes['claim_no_deadline']}"
    template = notes["claim_overdue" if claim.get("overdue") else "deadline"]
    return f"{source} {_deadline_text(template, claim['due_date'], passage, language, notes)}"


def _deadline_text(
    template: str, due: str, passage: Passage, language: str, notes: dict[str, str]
) -> str:
    return template.format(
        due=long_date(date.fromisoformat(due), language),
        days=passage.business_days or passage.calendar_days,
        unit=notes["business" if passage.business_days else "calendar"],
        passage=passage.id,
        label=passage.label[language],
    )


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
    claims = [facts["claim"]] if facts.get("claim") else facts.get("claims", [])
    for claim in claims:
        folios.add(claim["claim_id"])
        dates.add(date.fromisoformat(claim["opened_on"]))
        dates.add(date.fromisoformat(claim["last_step"]["on"]))
        passage = _passage(passages, claim.get("passage_id"))
        if claim.get("due_date") and passage is not None:
            dates.add(date.fromisoformat(claim["due_date"]))
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
        actions=frozenset(facts.get("actions_taken", []))
        | _registered(session, customer_id, facts),
        counts=frozenset(
            {len(shown)} if (shown := facts.get("options") or facts.get("claims")) else set()
        ),
    )


def _registered(session: Session, customer_id: str, facts: dict[str, Any]) -> set[str]:
    """The registration a claim status turn may state: "fue registrado", "foi registrada".

    Only the claim reported in a status turn, and only when it is a dispute of the customer
    read back from the database by its folio. In any other turn, saying something was
    registered needs the action among the actions verified in that turn.
    """
    claim = facts.get("claim")
    if facts.get("action") != "report_claim_status" or not claim:
        return set()
    if claim.get("source") != "disputes":
        return set()
    found = session.execute(
        select(Dispute.id).where(
            Dispute.folio == claim["claim_id"], Dispute.customer_id == customer_id
        )
    ).first()
    return {"register_dispute"} if found else set()


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

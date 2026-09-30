"""The recognition step of design 6.4: what the customer sees before disputing a charge.

The caller reads the detail from the database; this module only turns it into the text of the
step, in Spanish or Portuguese. The text informs and never argues (TRZ-16 CA6), and "Sigo sin
reconocerlo" is always the first choice (CA5). Code writes it, not the LLM, so the last four
digits of the card and the city never reach the model.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from app.domain.fx import AmountDisplay

Language = Literal["es", "pt"]
Choice = Literal["not_recognized", "recognized"]

# The first choice is the primary one: continuing the dispute never takes an extra step.
CHOICES: tuple[Choice, ...] = ("not_recognized", "recognized")

_CHOICE_LABELS: dict[Language, dict[Choice, str]] = {
    "es": {"not_recognized": "Sigo sin reconocerlo", "recognized": "Ya lo reconozco"},
    "pt": {"not_recognized": "Continuo sem reconhecer", "recognized": "Já reconheço"},
}

_STATUSES: dict[Language, dict[str, str]] = {
    "es": {"Approved": "aprobado", "Pending": "pendiente"},
    "pt": {"Approved": "aprovada", "Pending": "pendente"},
}

# Month names by language, also used to read and write dates in replies (TRZ-20).
MONTHS: dict[Language, tuple[str, ...]] = {
    "es": (
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ),
    "pt": (
        "janeiro",
        "fevereiro",
        "março",
        "abril",
        "maio",
        "junho",
        "julho",
        "agosto",
        "setembro",
        "outubro",
        "novembro",
        "dezembro",
    ),
}

_TEXT: dict[Language, dict[str, str]] = {
    "es": {
        "intro": "Este es el cargo que encontramos; revisa el detalle.",
        "pending": (
            "Este cargo todavía está pendiente: aún no se ha liquidado, así que el comercio "
            "todavía puede confirmarlo, ajustarlo o cancelarlo."
        ),
        "twin_pending": (
            "Hay otro cargo igual de este comercio el {when}, {status}. Cuando uno de dos "
            "cargos iguales está pendiente, suele ser una retención temporal que no se cobra."
        ),
        "twin_approved": (
            "Hay otro cargo igual de este comercio el {when}, también aprobado: puede ser un "
            "cobro duplicado."
        ),
        "earlier": "Tienes cargos de este comercio en {months}.",
        "and": "y",
        "of": "de",
        "question": "¿Reconoces este cargo?",
    },
    "pt": {
        "intro": "Esta é a cobrança que encontramos; confira o detalhe.",
        "pending": (
            "Esta cobrança ainda está pendente: ainda não foi liquidada, então o estabelecimento "
            "ainda pode confirmá-la, ajustá-la ou cancelá-la."
        ),
        "twin_pending": (
            "Há outra cobrança igual deste estabelecimento em {when}, {status}. Quando uma de "
            "duas cobranças iguais está pendente, costuma ser uma retenção temporária que não "
            "é cobrada."
        ),
        "twin_approved": (
            "Há outra cobrança igual deste estabelecimento em {when}, também aprovada: pode ser "
            "uma cobrança duplicada."
        ),
        "earlier": "Você tem cobranças deste estabelecimento em {months}.",
        "and": "e",
        "of": "de",
        "question": "Você reconhece essa cobrança?",
    },
}


@dataclass(frozen=True)
class Twin:
    """Another charge of the same merchant, amount and currency.

    Attributes:
        transaction_id: Id of the twin.
        at: Naive local time of the twin.
        status: Approved or Pending.
    """

    transaction_id: str
    at: datetime
    status: str


@dataclass(frozen=True)
class ChargeDetail:
    """Everything the recognition step shows, read from the database.

    Attributes:
        transaction_id: Id of the charge.
        transaction_type: Purchase, Payment or Withdrawal.
        merchant: Merchant; only purchases have one.
        amount: Registered amount with its approximate local equivalent.
        at: Naive local time of the charge.
        channel: Channel of the charge.
        city: City of the charge, when the dataset has it; there is never an address.
        product_type: Type of the product charged, such as credit_card.
        last4: Last four digits of that product, when the dataset has them.
        status: Approved or Pending.
        twin: Twin charge, when there is one (TRZ-16 CA3).
        earlier_months: First day of each earlier month with charges of the same merchant.
    """

    transaction_id: str
    transaction_type: str
    merchant: str | None
    amount: AmountDisplay
    at: datetime
    channel: str
    city: str | None
    product_type: str
    last4: str | None
    status: str
    twin: Twin | None
    earlier_months: tuple[date, ...]


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


def choices(language: Language) -> list[dict[str, str]]:
    """The two choices of the step, the primary one first (CA5).

    Args:
        language: es or pt.

    Returns:
        Each choice with its id and its label in the language.
    """
    return [{"id": c, "label": _CHOICE_LABELS[language][c]} for c in CHOICES]


def recognition_text(detail: ChargeDetail, language: Language) -> str:
    """The text of the recognition step.

    The detail of the charge (merchant, amount, date, channel, city, card, status) is shown
    once, in the charge card the response carries (`ChatOut.charge`); the text only frames it
    and adds what the card cannot say.

    Args:
        detail: The charge as read from the database.
        language: es or pt.

    Returns:
        The introduction, the notes that apply (pending, twin, earlier months) and the question.
    """
    t = _TEXT[language]
    notes = []
    if detail.status == "Pending":
        notes.append(t["pending"])
    if detail.twin is not None:
        when = _when(detail.twin.at, language)
        if "Pending" in (detail.status, detail.twin.status):
            twin_status = _STATUSES[language].get(detail.twin.status, detail.twin.status)
            notes.append(t["twin_pending"].format(when=when, status=twin_status))
        else:
            notes.append(t["twin_approved"].format(when=when))
    if detail.earlier_months:
        notes.append(t["earlier"].format(months=_months(detail.earlier_months, language)))
    return "\n".join([t["intro"], *notes, t["question"]])


def _when(at: datetime, language: Language) -> str:
    # The same form as the charge card of the screen: "16 jun 2026, 22:40".
    return f"{at.day} {MONTHS[language][at.month - 1][:3]} {at.year}, {at:%H:%M}"


def _months(months: tuple[date, ...], language: Language) -> str:
    """Month names joined as a sentence; the year is said once when all share it."""
    t, names = _TEXT[language], MONTHS[language]
    ordered = sorted(months)
    one_year = len({m.year for m in ordered}) == 1
    words = [
        names[m.month - 1] if one_year else f"{names[m.month - 1]} {t['of']} {m.year}"
        for m in ordered
    ]
    joined = words[0] if len(words) == 1 else f"{', '.join(words[:-1])} {t['and']} {words[-1]}"
    return f"{joined} {t['of']} {ordered[0].year}" if one_year else joined

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

_CHANNELS: dict[Language, dict[str, str]] = {
    "es": {
        "ATM": "cajero automático",
        "App": "app",
        "Branch": "sucursal",
        "POS": "compra presencial",
        "Transfer": "transferencia",
        "Web": "compra en línea",
    },
    "pt": {
        "ATM": "caixa eletrônico",
        "App": "app",
        "Branch": "agência",
        "POS": "compra presencial",
        "Transfer": "transferência",
        "Web": "compra on-line",
    },
}

_PRODUCTS: dict[Language, dict[str, str]] = {
    "es": {"credit_card": "tarjeta de crédito", "debit_card": "tarjeta de débito"},
    "pt": {"credit_card": "cartão de crédito", "debit_card": "cartão de débito"},
}

_TYPES: dict[Language, dict[str, str]] = {
    "es": {"Purchase": "Compra", "Payment": "Pago", "Withdrawal": "Retiro"},
    "pt": {"Purchase": "Compra", "Payment": "Pagamento", "Withdrawal": "Saque"},
}

_STATUSES: dict[Language, dict[str, str]] = {
    "es": {"Approved": "aprobado", "Pending": "pendiente"},
    "pt": {"Approved": "aprovada", "Pending": "pendente"},
}

_MONTHS: dict[Language, tuple[str, ...]] = {
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
        "intro": "Este es el cargo que encontré:",
        "merchant": "Comercio",
        "amount": "Monto",
        "when": "Fecha y hora",
        "at": "a las",
        "channel": "Canal",
        "city": "Ciudad",
        "card": "Tarjeta",
        "product": "Producto",
        "ending": "terminada en",
        "status": "Estado",
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
        "question": "¿Lo reconoces?",
    },
    "pt": {
        "intro": "Esta é a cobrança que encontrei:",
        "merchant": "Estabelecimento",
        "amount": "Valor",
        "when": "Data e hora",
        "at": "às",
        "channel": "Canal",
        "city": "Cidade",
        "card": "Cartão",
        "product": "Produto",
        "ending": "com final",
        "status": "Status",
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

    Args:
        detail: The charge as read from the database.
        language: es or pt.

    Returns:
        The detail of the charge and the notes that apply: pending, twin, earlier months.
    """
    t = _TEXT[language]
    what = detail.merchant or _TYPES[language].get(detail.transaction_type, "")
    lines = [
        t["intro"],
        f"- {t['merchant']}: {what}",
        f"- {t['amount']}: {_amount(detail.amount)}",
        f"- {t['when']}: {_when(detail.at, language)}",
        f"- {t['channel']}: {_CHANNELS[language].get(detail.channel, detail.channel)}",
    ]
    if detail.city:
        lines.append(f"- {t['city']}: {detail.city}")
    card = _PRODUCTS[language].get(detail.product_type)
    if detail.last4:
        label = t["card"] if card else t["product"]
        name = f"{card} " if card else ""
        lines.append(f"- {label}: {name}{t['ending']} {detail.last4}")
    status = _STATUSES[language].get(detail.status, detail.status)
    lines.append(f"- {t['status']}: {status}")

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
    return "\n".join([*lines, "", *notes, t["question"]])


def _amount(a: AmountDisplay) -> str:
    shown = f"{a.amount:,.2f} {a.currency}"
    if a.converted_amount is None:
        return shown
    return f"{shown} ({a.converted_amount:,.2f} {a.converted_currency}, {a.label})"


def _when(at: datetime, language: Language) -> str:
    return f"{at:%d/%m/%Y} {_TEXT[language]['at']} {at:%H:%M}"


def _months(months: tuple[date, ...], language: Language) -> str:
    """Month names joined as a sentence; the year is said once when all share it."""
    t, names = _TEXT[language], _MONTHS[language]
    ordered = sorted(months)
    one_year = len({m.year for m in ordered}) == 1
    words = [
        names[m.month - 1] if one_year else f"{names[m.month - 1]} {t['of']} {m.year}"
        for m in ordered
    ]
    joined = words[0] if len(words) == 1 else f"{', '.join(words[:-1])} {t['and']} {words[-1]}"
    return f"{joined} {t['of']} {ordered[0].year}" if one_year else joined

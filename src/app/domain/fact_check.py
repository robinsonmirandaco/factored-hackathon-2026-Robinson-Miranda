"""Deterministic fact checker of customer replies (TRZ-20, design 6.5 and 7, layer 4).

Every figure a reply states must come from the verified facts of the turn: records read,
actions verified and policy passages cited. The checker reads the final text with regular
expressions, never with a model, and names each element that no fact backs.

What it reads, in this order, each span taken once:
  folios and ids       DSP-2026-00042, CMP-..., CASE-..., ACT-...
  passages             §2.1
  dates                2026-06-17, 17/06/2026, 17/06, 17 de junio (de 2026), 8 jun, 8 de julho
  card digits          terminada en 1234, com final 1234, ****1234
  durations            15 días hábiles, 15 dias úteis, 120 días naturales, 24 horas
  amounts              $1,234.56, 1.234,56 MXN, R$ 120,00, US$ 120, 120 dólares, 2.500 pesos
  merchants            a closed list: the merchants of the customer's own transactions
  numbers              any digits left: a bare "120" can be an amount, so it needs a fact too
It also reads, over the whole text:
  forbidden requests   password, PIN, security code, full card number, identity document
  action claims        "registramos", "bloqueamos", "quedó bloqueada", refunds, cancellations
  contact promises     "en breve", "pronto te contactaremos", "em breve", "você receberá notícias"

Known limits: numbers written in words ("quince días") are not read, and a merchant that is
not on the customer's list (an invented name) is not detected.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from app.domain.clock import relative_window
from app.domain.recognition import MONTHS

Kind = Literal[
    "folio",
    "passage",
    "date",
    "card_digits",
    "deadline",
    "amount",
    "merchant",
    "number",
    "forbidden_request",
    "contact_promise",
    "vague_deadline",
    "relative_date",
    "action_claim",
]

# Actions no tool performs: a reply that claims one is always unsupported.
REFUND_OR_CANCEL = "refund_or_cancel"


@dataclass(frozen=True)
class Claim:
    """One element read from a reply.

    Attributes:
        kind: What was read.
        text: The literal fragment of the reply.
        value: Normalized value, compared with the facts.
    """

    kind: Kind
    text: str
    value: str


@dataclass(frozen=True)
class VerifiedFacts:
    """What a reply may state in one turn.

    Attributes:
        amounts: Amounts of the records read, registered and converted.
        dates: Dates of the records read, of the registration and of its deadline.
        folios: Folios of the actions verified and ids of the claims read.
        passages: Citations of the passages backing the turn, such as "§2.1".
        deadlines: Days those passages set.
        last4: Last four digits of the card of the records read.
        merchants: Merchants of the records read, casefolded.
        known_merchants: Every merchant of the customer's transactions, casefolded; the closed
            list a mentioned merchant is looked for in.
        actions: Actions verified in the turn, such as register_dispute and block_card.
        counts: How many records were shown, such as the number of options to choose from.
        today: The simulated today of the turn; a relative date ("la semana pasada") is
            checked against it. Without it, no relative date is backed.
    """

    amounts: frozenset[Decimal] = frozenset()
    dates: frozenset[date] = frozenset()
    folios: frozenset[str] = frozenset()
    passages: frozenset[str] = frozenset()
    deadlines: frozenset[int] = frozenset()
    last4: frozenset[str] = frozenset()
    merchants: frozenset[str] = frozenset()
    known_merchants: frozenset[str] = frozenset()
    actions: frozenset[str] = frozenset()
    counts: frozenset[int] = frozenset()
    today: date | None = None

    def numbers(self) -> frozenset[int]:
        """Whole numbers a bare figure may be: parts of the amounts, dates, deadlines, digits,
        and the counts of records shown."""
        found = {int(a) for a in self.amounts} | set(self.deadlines) | set(self.counts)
        found |= {n for d in self.dates for n in (d.day, d.month, d.year)}
        found |= {int(d) for d in self.last4}
        return frozenset(found)


def _month_pattern() -> tuple[str, dict[str, int]]:
    names: dict[str, int] = {}
    for months in MONTHS.values():
        for number, name in enumerate(months, start=1):
            names[name] = number
            names[name[:3]] = number
    names |= {"sept": 9, "set": 9}
    ordered = sorted(names, key=len, reverse=True)
    return "|".join(ordered), names


_MONTH_RE, _MONTH_NUMBER = _month_pattern()

_FOLIO = re.compile(r"\b(?:DSP|CMP|CASE|ACT)-[0-9A-Z-]*[0-9A-Z]", re.IGNORECASE)
_PASSAGE = re.compile(r"§\s?(\d+(?:\.\d+)*)")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b")
_TEXT_DATE = re.compile(
    rf"\b(\d{{1,2}})(?:\s+de)?\s+({_MONTH_RE})\b\.?(?:\s+(?:de|del)\s+(\d{{4}})|\s+(\d{{4}}))?",
    re.IGNORECASE,
)
_CARD_DIGITS = re.compile(
    r"(?:\bterminad[ao]s?\s+en|\bterminaci[oó]n|\bfinalizad[ao]\s+em|\bterminad[ao]\s+em"
    r"|\bcom\s+final|\bcon\s+final|\bfinal|[*•xX]{2,})\s*(\d{4})\b",
    re.IGNORECASE,
)
_DURATION = re.compile(
    r"\b(\d{1,4})\s+(d[ií]as?|horas?|semanas?|mes(?:es)?)\b"
    r"(?:\s+(?:h[aá]biles|[uú]teis|naturales|corridos|calendario))?",
    re.IGNORECASE,
)
_NUM = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_CURRENCY = r"US\$|R\$|MX\$|COP\$|AR\$|\$|€|USD|MXN|COP|ARS|BRL|EUR"
_AMOUNT = re.compile(
    rf"(?:(?:{_CURRENCY})\s?({_NUM}))"
    rf"|(?:\b({_NUM})\s?(?:{_CURRENCY}|d[oó]lares?|pesos|reais|real|reales|euros?)\b)",
    re.IGNORECASE,
)
_DIGITS = re.compile(r"\d+")

# Promises of news or contact that no step of the case backs: the bank may never call. A person
# is offered by code, with a fixed sentence, when one is due.
_CONTACT_PROMISE = re.compile(
    r"\b(?:en\s+breve|em\s+breve|"
    r"(?:pronto\s+)?te\s+(?:contactaremos|llamaremos|escribiremos)"
    r"(?:\s+(?:pronto|en\s+breve))?|"
    r"te\s+(?:contactar[aá]|llamar[aá]|escribir[aá])|"
    r"nos\s+(?:pondremos\s+en\s+contacto|comunicaremos\s+contigo)|"
    r"se\s+(?:pondr[aá]n?\s+en\s+contacto|comunicar[aá]n?\s+contigo)|"
    r"entrar(?:[aá]|[aã]o)\s+em\s+contato|entraremos\s+em\s+contato|"
    r"(?:vai|v[aã]o|vamos|iremos|ir[aá])\s+entrar\s+em\s+contato|"
    r"te\s+mantendremos\s+(?:informad[oa]s?|al\s+tanto)|te\s+(?:informaremos|avisaremos)|"
    r"mant[eê]-l[oa]s?\s+informad[oa]s?|manteremos\s+voc[eê]\s+informad[oa]|"
    r"manter\s+voc[eê]\s+informad[oa]|"
    r"voc[eê]\s+receber[aá]\s+(?:not[ií]cias|novidades))\b",
    re.IGNORECASE,
)
# Relative dates a reply may give a charge, with the key of their window in the clock's table.
# Each is backed only if a date of the turn falls in that window counted from the simulated
# today: the LLM once called a charge of the day before "de la semana pasada".
_RELATIVE_DATES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (key, re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE))
    for key, pattern in (
        ("day_before_yesterday", r"anteayer|anteontem"),
        ("yesterday", r"ayer|ontem"),
        ("today", r"hoy|hoje"),
        ("last_week", r"semana\s+pasada|semana\s+passada"),
        ("this_week", r"esta\s+semana|nesta\s+semana"),
        ("last_month", r"mes\s+pasado|m[eê]s\s+passado"),
        ("weekend", r"fin\s+de\s+semana|fim\s+de\s+semana"),
    )
)

# Timelines no passage backs: only a deadline written by code, with its passage, may be stated.
_VAGUE_DEADLINE = re.compile(
    r"\b(?:en\s+(?:los\s+pr[oó]ximos|unos|pocos)\s+d[ií]as|"
    r"en\s+las\s+pr[oó]ximas\s+horas|a\s+la\s+brevedad|pr[oó]ximamente|"
    r"n[oa]s\s+pr[oó]xim[oa]s\s+(?:dias|horas)|em\s+(?:alguns|poucos)\s+dias)\b",
    re.IGNORECASE,
)

_FORBIDDEN = re.compile(
    r"\b(?:contrase[nñ]as?|claves?|senhas?|passwords?|PIN|NIP|CVV2?|CVC|"
    r"c[oó]digos?\s+de\s+seguridad|c[oó]digos?\s+de\s+seguran[cç]a|"
    r"n[uú]mero\s+(?:completo\s+)?de\s+(?:tu|la|su)\s+tarjeta|"
    r"n[uú]mero\s+(?:completo\s+)?do\s+(?:seu\s+)?cart[aã]o|"
    r"documentos?|c[eé]dula|CPF|CURP|DNI|RG)\b",
    re.IGNORECASE,
)

# Auxiliaries that turn a participle into a claim that something is done: "quedó bloqueada".
_AUX = (
    r"(?:qued[oó]|queda|quedaron|fue|fueron|est[aá]|ya\s+est[aá]|ha\s+sido|"
    r"foi|foram|ficou|j[aá]\s+est[aá])"
)
_ACTION_CLAIMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "register_dispute",
        re.compile(
            rf"\b(?:registramos|registré|registrei|hemos\s+registrado|he\s+registrado|"
            rf"abrimos|{_AUX}\s+(?:\w+\s+)?registrad[ao]s?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "block_card",
        re.compile(
            rf"\b(?:bloqueamos|bloqueé|bloqueei|hemos\s+bloqueado|he\s+bloqueado|"
            rf"{_AUX}\s+(?:\w+\s+)?bloquead[ao]s?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        REFUND_OR_CANCEL,
        re.compile(
            r"\b(?:reembols\w*|devol(?:v\w*|uci[oó]n\w*|u[cç][aã]o)|reintegr\w*|estorn\w*|"
            r"abonamos|abonaremos|creditamos|cancelamos|anulamos)\b",
            re.IGNORECASE,
        ),
    ),
)


def extract(text: str, known_merchants: frozenset[str] = frozenset()) -> list[Claim]:
    """Reads every checkable element of a reply.

    Args:
        text: Final reply.
        known_merchants: Casefolded merchants whose mention counts as a claim.

    Returns:
        The elements, in the order they were read.
    """
    claims: list[Claim] = []
    work = text

    def take(kind: Kind, pattern: re.Pattern[str], value: Callable[[re.Match[str]], str]) -> None:
        nonlocal work
        for m in pattern.finditer(work):
            claims.append(Claim(kind, m.group(0).strip(), value(m)))
        work = pattern.sub(lambda m: " " * len(m.group(0)), work)

    take("folio", _FOLIO, lambda m: m.group(0).upper())
    take("passage", _PASSAGE, lambda m: "§" + m.group(1))
    take("date", _ISO_DATE, lambda m: _date(m.group(1), m.group(2), m.group(3)))
    take("date", _NUMERIC_DATE, lambda m: _date(m.group(3), m.group(2), m.group(1)))
    take(
        "date",
        _TEXT_DATE,
        lambda m: _date(
            m.group(3) or m.group(4), str(_MONTH_NUMBER[m.group(2).lower()]), m.group(1)
        ),
    )
    take("card_digits", _CARD_DIGITS, lambda m: m.group(1))
    take("deadline", _DURATION, lambda m: f"{m.group(1)} {m.group(2)[0].lower()}")
    take("amount", _AMOUNT, lambda m: _amount(m.group(1) or m.group(2)))
    folded = work.casefold()
    for name in sorted(known_merchants, key=len, reverse=True):
        pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        for m in pattern.finditer(folded):
            claims.append(Claim("merchant", text[m.start() : m.end()], name))
        work = _blank(work, pattern, folded)
        folded = work.casefold()
    claims += [Claim("number", m.group(0), m.group(0)) for m in _DIGITS.finditer(work)]
    claims += [
        Claim("forbidden_request", m.group(0), m.group(0)) for m in _FORBIDDEN.finditer(text)
    ]
    claims += [
        Claim("contact_promise", m.group(0), m.group(0)) for m in _CONTACT_PROMISE.finditer(text)
    ]
    claims += [
        Claim("vague_deadline", m.group(0), m.group(0)) for m in _VAGUE_DEADLINE.finditer(text)
    ]
    for key, pattern in _RELATIVE_DATES:
        claims += [Claim("relative_date", m.group(0), key) for m in pattern.finditer(text)]
    for action, pattern in _ACTION_CLAIMS:
        claims += [Claim("action_claim", m.group(0), action) for m in pattern.finditer(text)]
    return claims


def unsupported(text: str, facts: VerifiedFacts) -> list[Claim]:
    """The elements of a reply that no verified fact backs.

    Args:
        text: Final reply.
        facts: Verified facts of the turn.

    Returns:
        The unsupported elements; empty when the reply may be sent.
    """
    return [c for c in extract(text, facts.known_merchants) if not _backed(c, facts)]


def _backed(c: Claim, facts: VerifiedFacts) -> bool:
    if c.kind == "folio":
        return c.value in facts.folios
    if c.kind == "passage":
        return c.value in facts.passages
    if c.kind == "date":
        return _date_backed(c.value, facts.dates)
    if c.kind == "card_digits":
        return c.value in facts.last4
    if c.kind == "deadline":
        count, unit = c.value.split()
        # A duration is backed only by a passage of the turn that sets that many days.
        return unit == "d" and int(count) in facts.deadlines
    if c.kind == "amount":
        return c.value != "" and Decimal(c.value) in facts.amounts
    if c.kind == "merchant":
        return c.value in facts.merchants
    if c.kind == "number":
        return int(c.value) in facts.numbers()
    if c.kind == "action_claim":
        return c.value in facts.actions
    if c.kind == "relative_date":
        window = relative_window(facts.today, c.value) if facts.today else None
        if window is None or facts.today is None:
            return False
        low, high = window
        return any(low <= (facts.today - d).days <= high for d in facts.dates)
    return False  # forbidden requests, contact promises and vague deadlines are never backed


def _date(year: str | None, month: str, day: str) -> str:
    """ISO date, or --MM-DD when the reply gave no year; empty when it is not a date."""
    try:
        if year is None:
            date(2000, int(month), int(day))  # a leap year, so 29 February reads as a date
            return f"--{int(month):02d}-{int(day):02d}"
        full = int(year) + (2000 if len(year) == 2 else 0)
        return date(full, int(month), int(day)).isoformat()
    except ValueError:
        return ""


def _date_backed(value: str, dates: frozenset[date]) -> bool:
    if not value:
        return False
    if value.startswith("--"):
        return value[2:] in {d.isoformat()[5:] for d in dates}
    return date.fromisoformat(value) in dates


def _amount(raw: str) -> str:
    """Normalizes an amount written with either decimal separator, to two decimals.

    The separator followed by one or two digits at the end is the decimal one; any other
    separator groups thousands. Money is never written with three decimals, so "1.234" and
    "1,234" both read as one thousand two hundred thirty-four.
    """
    decimals = re.search(r"[.,](\d{1,2})$", raw)
    whole = raw[: decimals.start()] if decimals else raw
    digits = re.sub(r"[.,]", "", whole) + ("." + decimals.group(1) if decimals else "")
    try:
        return str(Decimal(digits).quantize(Decimal("0.01")))
    except InvalidOperation:
        return ""


def _blank(text: str, pattern: re.Pattern[str], folded: str) -> str:
    # Casefolding keeps lengths for the scripts of the dataset, so spans map back one to one.
    out = list(text)
    for m in pattern.finditer(folded):
        out[m.start() : m.end()] = " " * (m.end() - m.start())
    return "".join(out)


def amount_fact(value: float) -> Decimal:
    """An amount of a record as a fact, to two decimals like the amounts read from replies.

    Args:
        value: Amount of a record.

    Returns:
        The amount as a Decimal with two decimals.
    """
    return Decimal(str(value)).quantize(Decimal("0.01"))

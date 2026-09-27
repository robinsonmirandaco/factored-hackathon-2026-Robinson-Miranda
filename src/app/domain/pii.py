"""PII handling: redaction before any text reaches the LLM or the audit log, and the keyed hash
that stands in for identity document numbers.

Redaction is pattern-based and conservative. Matches are replaced with typed placeholders so the
LLM keeps the meaning of the sentence without seeing the data.

The identification of a charge reads amounts, dates, the last four digits of a card and case
folios from the same text, so the patterns are shaped to leave those alone:

- A card number needs 13 to 19 digits and a valid Luhn check digit.
- CPF, CNPJ and RUT are redacted when written with their canonical punctuation, or when bare
  digits pass the document's check digits.
- A cédula or DNI of 6 to 12 digits looks exactly like an amount ("12.345.678"), so it is
  redacted only after a keyword such as "cédula", "C.C." or "DNI". Without the keyword it is
  left in the text.
- An account number needs a keyword such as "cuenta" or "conta", except a CLABE (18 digits
  with a valid check digit) and a CBU (22 digits), which no amount or card can be.
- A phone needs a country code (+52, +54, +55, +57), a national grouping such as
  "(55) 1234-5678" or "300 123 4567", a keyword such as "celular", or the bare shape of a
  Colombian or Brazilian mobile. A bare Mexican or Argentine number without grouping or keyword
  cannot be told apart from a document and is left in the text.
"""

import hashlib
import hmac
import re
from collections.abc import Callable

CARD = "[TARJETA]"
EMAIL = "[CORREO]"
PHONE = "[TELEFONO]"
DOCUMENT = "[DOCUMENTO]"
ACCOUNT = "[CUENTA]"

_NUMBER_FILLER = (
    r"(?:\s*(?:de\s+(?:ciudadan[íi]a|identidad|ahorros)|corriente|poupan[çc]a|n[úu]mero|number"
    r"|nro\.?|no\.?|n[º°]\.?|es|é|is|:|#))*\s*"
)


_Check = Callable[[re.Match[str]], bool]


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _cpf_ok(digits: str) -> bool:
    if len(set(digits)) == 1:
        return False
    for n in (9, 10):
        total = sum(int(digits[i]) * (n + 1 - i) for i in range(n))
        if (total * 10) % 11 % 10 != int(digits[n]):
            return False
    return True


def _cnpj_ok(digits: str) -> bool:
    if len(set(digits)) == 1:
        return False
    weights = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    for n in (12, 13):
        total = sum(int(d) * w for d, w in zip(digits[:n], weights[13 - n :], strict=True))
        check = 11 - total % 11
        if (0 if check >= 10 else check) != int(digits[n]):
            return False
    return True


def _rut_ok(body: str, dv: str) -> bool:
    total = sum(int(d) * (2 + i % 6) for i, d in enumerate(reversed(body)))
    check = 11 - total % 11
    expected = {11: "0", 10: "K"}.get(check, str(check))
    return dv.upper() == expected


def _clabe_ok(digits: str) -> bool:
    total = sum(int(d) * (3, 7, 1)[i % 3] % 10 for i, d in enumerate(digits[:17]))
    return (10 - total % 10) % 10 == int(digits[17])


def _cpf(m: re.Match[str]) -> bool:
    return bool(re.fullmatch(r"\d{3}\.\d{3}\.\d{3}-\d{2}", m[0])) or _cpf_ok(_digits(m[0]))


def _cnpj(m: re.Match[str]) -> bool:
    return "/" in m[0] or _cnpj_ok(_digits(m[0]))


def _rut(m: re.Match[str]) -> bool:
    return "." in m[0] or _rut_ok(_digits(m[0][:-1]), m[0][-1])


def _card(m: re.Match[str]) -> bool:
    d = _digits(m[0])
    return 13 <= len(d) <= 19 and _luhn_ok(d)


def _clabe(m: re.Match[str]) -> bool:
    return _clabe_ok(m[0])


# Order matters: numbers with check digits go first, so a CPF or CNPJ is not taken for a phone
# or a card, and the keyword rules run last on whatever is still unredacted.
_RULES: list[tuple[str, re.Pattern[str], _Check | None]] = [
    (EMAIL, re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), None),
    (
        DOCUMENT,
        re.compile(r"(?<![\d.])\d{2}[. ]?\d{3}[. ]?\d{3}\s?/?\s?\d{4}[ -]?\d{2}(?![\d])"),
        _cnpj,
    ),
    (DOCUMENT, re.compile(r"(?<![\d.])\d{3}[. ]?\d{3}[. ]?\d{3}[ -]?\d{2}(?![\d])"), _cpf),
    (
        DOCUMENT,
        re.compile(
            r"\b[A-Z][AEIOUX][A-Z]{2}\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[HM]"
            r"[A-Z]{2}[B-DF-HJ-NP-TV-Z]{3}[A-Z\d]\d\b",
            re.I,
        ),
        None,
    ),
    (DOCUMENT, re.compile(r"(?<![\d.])\d{1,2}[. ]?\d{3}[. ]?\d{3}-[\dkK](?![\w])"), _rut),
    (ACCOUNT, re.compile(r"(?<!\d)\d{22}(?!\d)"), None),
    (ACCOUNT, re.compile(r"(?<!\d)\d{18}(?!\d)"), _clabe),
    (
        CARD,
        re.compile(
            r"(?<![\d.])(?:\d{4}(?:[ .-]?\d{4}){3}\d{0,3}|\d{4}[ .-]?\d{6}[ .-]?\d{5}|\d{13,19})"
            r"(?![\d])"
        ),
        _card,
    ),
    (PHONE, re.compile(r"(?:\+|(?<!\d)00)5[2457](?:[ .()-]{0,2}\d){8,12}(?!\d)"), None),
    (
        PHONE,
        re.compile(r"(?<![\d.])(?:\(\d{2,3}\)|\d{2,3})[ .-]\d{3,5}[ .-]?\d{4}(?![\d])"),
        None,
    ),
    (PHONE, re.compile(r"(?<![\d.])(?:3\d{9}|[1-9]{2}9\d{8})(?![\d])"), None),
    (
        PHONE,
        re.compile(
            r"\b(?:tel[ée]fono|telefone|tel\.?|celular|cel\.?|m[óo]vil|whatsapp|fone)"
            + _NUMBER_FILLER
            + r"(?P<n>\+?\d(?:[ .()-]{0,2}\d){7,12})(?!\d)",
            re.I,
        ),
        None,
    ),
    (
        DOCUMENT,
        re.compile(
            r"(?:\bc\.\s?c\.|\b(?:c[ée]dula|cc|dni|documento|identificaci[óo]n|cpf|cnpj|rut"
            r"|pasaporte|passport)\b)"
            + _NUMBER_FILLER
            + r"(?P<n>[A-Z]{0,3}\d(?:[ .-]?\d){5,11}(?:-[\dkK])?)(?![\d])",
            re.I,
        ),
        None,
    ),
    (
        ACCOUNT,
        re.compile(
            r"\b(?:cuenta|conta|acct|account)\b"
            + _NUMBER_FILLER
            + r"(?P<n>\d(?:[ .-]?\d){5,21})(?!\d)",
            re.I,
        ),
        None,
    ),
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Replaces PII with typed placeholders, leaving amounts, dates and last-four digits intact.

    A full card number is replaced as a whole: not even its last four digits stay in the text.

    Args:
        text: Raw text from a customer or an operator.

    Returns:
        The redacted text and the number of replacements per placeholder.
    """
    counts: dict[str, int] = {}
    out = text
    for tag, pat, accept in _RULES:
        out, n = _replace(pat, tag, accept, out)
        if n:
            counts[tag] = counts.get(tag, 0) + n
    return out, counts


def _replace(pat: re.Pattern[str], tag: str, accept: _Check | None, text: str) -> tuple[str, int]:
    hits = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal hits
        if accept is not None and not accept(m):
            return m[0]
        hits += 1
        if "n" not in pat.groupindex:
            return tag
        # Keyword rules replace only the number, so the sentence still says what was redacted.
        start, end = m.start("n") - m.start(), m.end("n") - m.start()
        return m[0][:start] + tag + m[0][end:]

    return pat.sub(sub, text), hits


def document_hash(key: str, document_type: str, document_number: str) -> str:
    """Returns the keyed hash stored instead of an identity document number.

    A keyed HMAC and not a plain hash, because document numbers are short and a plain hash of
    every possible number can be precomputed. Surrounding spaces are ignored and the type is
    compared in upper case, so the same document typed at login gives the same hash.

    Args:
        key: DOCUMENT_HASH_KEY.
        document_type: Document type, such as CC or DNI.
        document_number: Document number.

    Returns:
        HMAC-SHA256 of "TYPE:number", hex encoded.

    Raises:
        ValueError: If the key is empty.
    """
    if not key:
        raise ValueError("DOCUMENT_HASH_KEY is not set")
    message = f"{document_type.strip().upper()}:{document_number.strip()}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()

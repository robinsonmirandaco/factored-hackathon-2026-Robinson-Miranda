"""PII handling: redaction before any text reaches the LLM or the audit log, and the keyed hash
that stands in for identity document numbers.

Redaction is pattern-based and conservative. Matches are replaced with typed placeholders so the
LLM keeps the meaning of the sentence without seeing the data.
"""

import hashlib
import hmac
import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("[CARD]", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("[EMAIL]", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("[PHONE]", re.compile(r"(?:\+?\d{1,3}[ -]?)?\(?\d{3}\)?[ -]?\d{3}[ -]?\d{4}\b")),
    ("[SSN]", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    (
        "[ACCOUNT]",
        re.compile(r"\b(?:acct|account|cuenta)\s*(?:no\.?|#|number)?\s*[:\-]?\s*\d{6,}\b", re.I),
    ),
    (
        "[ID]",
        re.compile(
            r"\b(?:cc|cedula|cédula|dni|passport|pasaporte)\s*[:\-]?\s*[A-Z0-9]{6,}\b", re.I
        ),
    ),
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Replaces PII patterns with placeholders.

    Args:
        text: Raw text from a customer or an operator.

    Returns:
        The redacted text and the number of replacements per placeholder.
    """
    counts: dict[str, int] = {}
    out = text
    for tag, pat in _PATTERNS:
        out, n = pat.subn(tag, out)
        if n:
            counts[tag] = counts.get(tag, 0) + n
    return out, counts


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

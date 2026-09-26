"""PII redaction applied before any text reaches the LLM or the audit log.

Pattern-based and conservative. Matches are replaced with typed placeholders so the LLM keeps
the meaning of the sentence without seeing the data.
"""

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

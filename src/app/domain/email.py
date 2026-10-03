"""Rules of the transactional email (TRZ-33): who may receive it, what it says, when to retry.

Pure functions, no I/O. The email repeats the in-app notification, whose text code wrote and the
fact checker backed; it adds the case number and says it is a test email. It never carries a
name, a document or an address of the dataset.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class EmailConfig:
    """The email settings a running process needs.

    Attributes:
        test_inbox: The only address an email is ever written to.
        allowlist: Addresses a send may reach; anything else fails without calling the provider.
        max_attempts: Attempts per email before it is marked failed.
        retry_minutes: Minutes to wait after each failed attempt but the last.
    """

    test_inbox: str
    allowlist: frozenset[str]
    max_attempts: int = 5
    retry_minutes: tuple[int, ...] = (1, 2, 4, 8)


def parse_allowlist(value: str) -> frozenset[str]:
    """Reads a comma-separated allowlist.

    Args:
        value: Addresses separated by commas, as the environment holds them.

    Returns:
        The addresses, trimmed and in lower case.
    """
    return frozenset(a.strip().lower() for a in value.split(",") if a.strip())


def allowed(address: str, allowlist: frozenset[str]) -> bool:
    """Tells whether an email may be sent to an address.

    Args:
        address: The recipient.
        allowlist: The allowed addresses.

    Returns:
        True only for an address of the list; an empty list allows nothing.
    """
    return address.strip().lower() in allowlist


def next_attempt(attempts: int, now: datetime, config: EmailConfig) -> datetime | None:
    """When to try again after a failed attempt.

    Args:
        attempts: Attempts made so far, including the one that just failed.
        now: Time of the failed attempt.
        config: Email settings.

    Returns:
        The time of the next attempt, or None when no attempt is left and the email fails.
    """
    if attempts >= config.max_attempts:
        return None
    wait = config.retry_minutes[min(attempts, len(config.retry_minutes)) - 1]
    return now + timedelta(minutes=wait)


_SUBJECTS = {
    "es": "TRAZO [simulado]: novedades en tu aclaración",
    "pt": "TRAZO [simulado]: novidades na sua contestação",
}
_FOOTERS = {
    "es": "Caso {case_id}.\n\n[simulado] Correo de prueba de TRAZO, no enviado por un banco.",
    "pt": "Caso {case_id}.\n\n[simulado] E-mail de teste do TRAZO, não enviado por um banco.",
}


def compose_email(text: str, case_id: str, language: str) -> tuple[str, str]:
    """The subject and body of the email of a notification.

    Args:
        text: The notification text, as checked by the fact checker.
        case_id: The case it is about.
        language: es or pt; anything else is written in Spanish.

    Returns:
        Subject and plain-text body.
    """
    lang = language if language in _SUBJECTS else "es"
    return _SUBJECTS[lang], f"{text}\n\n{_FOOTERS[lang].format(case_id=case_id)}"

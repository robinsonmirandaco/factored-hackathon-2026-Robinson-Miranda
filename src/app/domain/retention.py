"""Retention of redacted conversations (TRZ-41, design 11.4): how long the text is kept.

The age of a row is measured on the real clock, when it was written: retention is an operations
rule about stored data, not a date of the bank's records, so the simulated clock does not apply.
"""

from datetime import datetime, timedelta

# What replaces the conversation text of a purged row; migration 0022 writes the same string.
PURGED_TEXT = "[eliminado por retención]"
# How each screen shows it: the database keeps one marker, the screen says it in its language.
_SHOWN = {"es": PURGED_TEXT, "pt": "[removido por retenção]"}


def cutoff(now: datetime, days: int) -> datetime:
    """The time before which conversation text is purged.

    Args:
        now: Time of the run, naive UTC.
        days: Days the text is kept, `CONVERSATION_RETENTION_DAYS`.

    Returns:
        `now` minus the retention.
    """
    return now - timedelta(days=days)


def shown(value: str | None, lang: str) -> str | None:
    """A stored text as a screen in that language shows it: the purge marker is translated.

    Args:
        value: Text read from a row, possibly purged.
        lang: es or pt.

    Returns:
        The marker in that language for a purged text; any other text unchanged.
    """
    return _SHOWN.get(lang, PURGED_TEXT) if value == PURGED_TEXT else value

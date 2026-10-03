"""Retention of redacted conversations (TRZ-41, design 11.4): how long the text is kept.

The age of a row is measured on the real clock, when it was written: retention is an operations
rule about stored data, not a date of the bank's records, so the simulated clock does not apply.
"""

from datetime import datetime, timedelta

# What replaces the conversation text of a purged row; migration 0022 writes the same string.
PURGED_TEXT = "[eliminado por retención]"


def cutoff(now: datetime, days: int) -> datetime:
    """The time before which conversation text is purged.

    Args:
        now: Time of the run, naive UTC.
        days: Days the text is kept, `CONVERSATION_RETENTION_DAYS`.

    Returns:
        `now` minus the retention.
    """
    return now - timedelta(days=days)

"""Clock helpers. The database stores naive UTC timestamps, so the code does too."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Returns the current time as a naive UTC datetime.

    Returns:
        Current UTC time without tzinfo, matching how timestamps are stored.
    """
    return datetime.now(UTC).replace(tzinfo=None)

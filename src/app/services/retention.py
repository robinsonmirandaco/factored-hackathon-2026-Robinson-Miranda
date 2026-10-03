"""The purge of redacted conversations older than the retention (TRZ-41).

The database function purge_conversation_text replaces the conversation text of old audit rows
and info_requests with a fixed marker and keeps every row; this service calls it and writes one
audit row of its own with how many rows it cleaned and between which times they were written.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.domain.retention import cutoff

_PURGE = text(
    "SELECT audit_rows, request_rows, oldest_at, newest_at FROM purge_conversation_text(:cutoff)"
)


def purge(session: Session, now: datetime, days: int) -> dict[str, Any]:
    """Purges the conversation text written before the retention and audits the run.

    Args:
        session: Session with the analyst role, the only one the function accepts.
        now: Time of the run, naive UTC; tests pass a later one.
        days: Days the text is kept.

    Returns:
        Rows cleaned in the audit log and in info_requests, and the oldest and newest time
        they were written (None when nothing was cleaned).
    """
    before = cutoff(now, days)
    audit_rows, request_rows, oldest, newest = session.execute(_PURGE, {"cutoff": before}).one()
    result = {
        "audit_rows": int(audit_rows),
        "info_requests": int(request_rows),
        "oldest": oldest.isoformat() if oldest else None,
        "newest": newest.isoformat() if newest else None,
    }
    write_audit(
        session,
        "system",
        "retention_purge",
        None,
        {"cutoff": before.isoformat(), "retention_days": days},
        result,
        customer_id=None,
    )
    return result

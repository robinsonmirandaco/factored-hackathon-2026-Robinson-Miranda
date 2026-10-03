"""Transactional email with the outbox pattern (TRZ-33).

With the flag on, the email of a notification is written to email_outbox in the transaction of
the notification itself, so it exists exactly when the notification does. A scheduled process
sends what is due, one provider call per email, and on a failure waits before trying again;
after the last attempt the email stays failed in the table. An address outside the allowlist is
never sent to: the email fails without calling the provider. Every attempt writes an audit row
with its outcome, never the address or the body.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import EmailOutbox, Notification
from app.adapters.email import EmailError, EmailProvider
from app.core.config import Settings
from app.domain.email import EmailConfig, allowed, compose_email, next_attempt, parse_allowlist


def config_from(settings: Settings) -> EmailConfig | None:
    """The email settings of a process, or None while the flag is off (CA1).

    Args:
        settings: Application settings.

    Returns:
        The settings with the flag on; None with it off, and then nothing is written or sent.
    """
    if not settings.email_enabled:
        return None
    return EmailConfig(
        test_inbox=settings.email_test_inbox.strip(),
        allowlist=parse_allowlist(settings.email_allowlist),
        max_attempts=settings.email_max_attempts,
        retry_minutes=tuple(settings.email_retry_minutes),
    )


def enqueue(session: Session, notification: Notification, config: EmailConfig) -> EmailOutbox:
    """Writes the email of a notification to the outbox, addressed to the test inbox.

    Args:
        session: The session of the notification, in its transaction.
        notification: The notification, already added to the session.
        config: Email settings.

    Returns:
        The pending email.
    """
    session.flush()
    subject, body = compose_email(notification.text, notification.case_id, notification.language)
    row = EmailOutbox(
        notification_id=notification.id,
        case_id=notification.case_id,
        customer_id=notification.customer_id,
        to_address=config.test_inbox,
        subject=subject,
        body=body,
    )
    session.add(row)
    return row


def send_due(
    session: Session, provider: EmailProvider, config: EmailConfig, now: datetime
) -> dict[str, int]:
    """Sends every pending email whose next attempt has come.

    Rows are locked and skipped if another run holds them, so two runs never send one email
    twice; the provider also gets the outbox id as its idempotency key.

    Args:
        session: Session with the analyst role.
        provider: The email provider.
        config: Email settings.
        now: Time of this run, naive UTC.

    Returns:
        How many emails were sent, left pending for a retry, and failed for good.
    """
    rows = session.execute(
        select(EmailOutbox)
        .where(EmailOutbox.status == "pending", EmailOutbox.next_attempt_at <= now)
        .order_by(EmailOutbox.id)
        .with_for_update(skip_locked=True)
    ).scalars()
    counts = {"sent": 0, "retry": 0, "failed": 0}
    for row in rows:
        row.attempts += 1
        if not allowed(row.to_address, config.allowlist):
            row.status, row.last_error = "failed", "not_allowed"
        else:
            try:
                provider.send(row.to_address, row.subject, row.body, f"email-outbox-{row.id}")
                row.status, row.sent_at, row.last_error = "sent", now, None
            except EmailError as exc:
                row.last_error = str(exc)
                retry_at = next_attempt(row.attempts, now, config)
                if retry_at is None:
                    row.status = "failed"
                else:
                    row.next_attempt_at = retry_at
        outcome = "retry" if row.status == "pending" else row.status
        counts[outcome] += 1
        write_audit(
            session,
            "system",
            "email_attempt",
            row.case_id,
            {"outbox_id": row.id, "attempt": row.attempts},
            {"status": row.status, "error": row.last_error},
            idempotency_key=f"email_attempt:{row.id}:{row.attempts}",
            customer_id=row.customer_id,
        )
    session.flush()
    return counts

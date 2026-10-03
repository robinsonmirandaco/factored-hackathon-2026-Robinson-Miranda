"""`make jobs` and its parts: the scheduled processes, each run once and then exits.

python -m app.cli.jobs expire-info-requests [--as-of YYYY-MM-DD]
python -m app.cli.jobs send-email
python -m app.cli.jobs purge [--now YYYY-MM-DDTHH:MM]
python -m app.cli.jobs all

They connect as trazo_app, never as the owner, so row level security applies to them as to the
API; like the API, they refuse to start as a superuser or a role with BYPASSRLS or CREATEROLE.
A scheduler (the cron service of Railway) runs `all`; tests and the demo run one part with a
made-up date. The simulated clock of the service is fixed at TRAZO_NOW, so without --as-of a
request for information asked through the service never reaches its deadline.
"""

import argparse
from datetime import date, datetime
from typing import Any

from app.adapters.db.session import Database, PrivilegedRoleError
from app.adapters.email import EmailProvider, ResendProvider
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger, new_trace_id
from app.core.time import utcnow
from app.domain.email import EmailConfig
from app.services.email_outbox import config_from, send_due
from app.services.info_requests import expire_overdue
from app.services.retention import purge

log = get_logger("jobs")


def expire_info_requests(db: Database, as_of: date, email: EmailConfig | None = None) -> list[str]:
    """Closes the cases whose request for information is past its deadline (TRZ-28 CA3).

    Args:
        db: Database of the service.
        as_of: Day of the simulated clock to compare deadlines with.
        email: Email settings of the notification; None while the email flag is off.

    Returns:
        The cases closed.
    """
    new_trace_id()
    with db.session(role="analyst") as session:
        closed = expire_overdue(session, as_of, email)
    log.info("info_requests_expired", as_of=as_of.isoformat(), closed=len(closed))
    return closed


def send_email(db: Database, provider: EmailProvider, config: EmailConfig) -> dict[str, int]:
    """Sends the emails of the outbox whose attempt is due (TRZ-33).

    Args:
        db: Database of the service.
        provider: The email provider.
        config: Email settings.

    Returns:
        How many were sent, left for a retry and failed.
    """
    new_trace_id()
    with db.session(role="analyst") as session:
        counts = send_due(session, provider, config, utcnow())
    log.info("email_sent", **counts)
    return counts


def purge_conversations(db: Database, now: datetime, days: int) -> dict[str, Any]:
    """Replaces the conversation text older than the retention, keeping every row (TRZ-41).

    Args:
        db: Database of the service.
        now: Time of the run, naive UTC.
        days: Days the text is kept.

    Returns:
        Rows cleaned and the times they were written.
    """
    new_trace_id()
    with db.session(role="analyst") as session:
        result = purge(session, now, days)
    log.info("conversations_purged", **result)
    return result


def _provider(settings: Settings) -> EmailProvider:
    if not settings.resend_api_key:
        raise SystemExit("EMAIL_ENABLED is on but RESEND_API_KEY is not set: nothing was sent")
    return ResendProvider(
        settings.resend_api_key, settings.email_from, settings.email_timeout_seconds
    )


def main(argv: list[str] | None = None) -> int:
    """Runs one scheduled process, or all of them, once.

    Args:
        argv: Command line arguments; the process arguments when omitted.

    Returns:
        Process exit code: 0 on success.
    """
    parser = argparse.ArgumentParser(prog="python -m app.cli.jobs")
    parser.add_argument("job", choices=["expire-info-requests", "send-email", "purge", "all"])
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        help="day of the simulated clock for the deadlines; TRAZO_NOW by default",
    )
    parser.add_argument(
        "--now",
        type=datetime.fromisoformat,
        help="real time of the purge, naive UTC; the current time by default",
    )
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level)
    email = config_from(settings)
    db = Database(settings.database_url)
    try:
        try:
            db.assert_unprivileged()
        except PrivilegedRoleError as exc:
            raise SystemExit(f"{exc}: no job was run") from exc
        if args.job in ("expire-info-requests", "all"):
            expire_info_requests(db, args.as_of or settings.trazo_now.date(), email)
        if args.job in ("send-email", "all"):
            if email is None:
                log.info("email_disabled")
            else:
                send_email(db, _provider(settings), email)
        if args.job in ("purge", "all"):
            purge_conversations(db, args.now or utcnow(), settings.conversation_retention_days)
    finally:
        db.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

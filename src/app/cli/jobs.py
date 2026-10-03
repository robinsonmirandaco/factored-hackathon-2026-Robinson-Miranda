"""`make jobs` and its parts: the scheduled processes, each run once and then exits.

python -m app.cli.jobs expire-info-requests [--as-of YYYY-MM-DD]
python -m app.cli.jobs all

They connect as trazo_app, never as the owner, so row level security applies to them as to the
API. A scheduler (the cron service of Railway) runs `all`; tests and the demo run one part with
a made-up date. The simulated clock of the service is fixed at TRAZO_NOW, so without --as-of a
request for information asked through the service never reaches its deadline.
"""

import argparse
from datetime import date

from app.adapters.db.session import Database
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger, new_trace_id
from app.services.info_requests import expire_overdue

log = get_logger("jobs")


def expire_info_requests(db: Database, as_of: date) -> list[str]:
    """Closes the cases whose request for information is past its deadline (TRZ-28 CA3).

    Args:
        db: Database of the service.
        as_of: Day of the simulated clock to compare deadlines with.

    Returns:
        The cases closed.
    """
    new_trace_id()
    with db.session(role="analyst") as session:
        closed = expire_overdue(session, as_of)
    log.info("info_requests_expired", as_of=as_of.isoformat(), closed=len(closed))
    return closed


def main(argv: list[str] | None = None) -> int:
    """Runs one scheduled process, or all of them, once.

    Args:
        argv: Command line arguments; the process arguments when omitted.

    Returns:
        Process exit code: 0 on success.
    """
    parser = argparse.ArgumentParser(prog="python -m app.cli.jobs")
    parser.add_argument("job", choices=["expire-info-requests", "all"])
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        help="day of the simulated clock for the deadlines; TRAZO_NOW by default",
    )
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level)
    db = Database(settings.database_url)
    try:
        expire_info_requests(db, args.as_of or settings.trazo_now.date())
    finally:
        db.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

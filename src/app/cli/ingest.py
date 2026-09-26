"""Ingestion entrypoint for the synthetic generator, the fixture used by CI and compose.

python -m app.cli.ingest synthetic --seed 42
"""

import argparse

from app.adapters.db.session import Database
from app.adapters.ingest.synthetic import generate
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.services.ingestion import ingest_rows

log = get_logger("ingest")


def main(argv: list[str] | None = None) -> int:
    """Loads the synthetic rows into the canonical tables and writes the quality report.

    Args:
        argv: Command-line arguments; defaults to sys.argv.

    Returns:
        Process exit code: 0 on success.
    """
    ap = argparse.ArgumentParser(prog="ingest")
    ap.add_argument("source", choices=["synthetic"])
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--db", help="override DATABASE_URL")
    ap.add_argument("--report-dir", default="eval/reports")
    args = ap.parse_args(argv)

    settings = Settings(database_url=args.db) if args.db else Settings()
    configure_logging(settings.log_level)

    rows = generate(settings.trazo_now, seed=args.seed)
    db = Database(settings.database_url)
    try:
        db.create_all()
        with db.session() as s:
            report = ingest_rows(s, args.source, *rows)
    finally:
        db.dispose()
    path = report.write(args.report_dir)
    log.info("ingest_done", report=str(path), **report.to_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

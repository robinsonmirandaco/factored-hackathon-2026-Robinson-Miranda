"""Loads one data source into the database of ADMIN_DATABASE_URL.

  python -m app.cli.seed cohort [--replace]                 # make seed: the gold cohort
  python -m app.cli.seed synthetic [--replace] [--if-empty] # the CI and compose fixture
  python -m app.cli.seed synthetic --customers 5000         # the load test (make load)

A database holds one source only; see app.services.seeding.
"""

import argparse
from pathlib import Path

from app.adapters.db.session import Database
from app.adapters.ingest.synthetic import generate
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.services import seeding
from app.services.ingestion import ingest_rows

log = get_logger("seed")


def main(argv: list[str] | None = None) -> int:
    """Seeds the database.

    Args:
        argv: Command-line arguments; defaults to sys.argv.

    Returns:
        Process exit code: 0 on success, also when --if-empty finds a seed already there.
    """
    ap = argparse.ArgumentParser(prog="seed")
    ap.add_argument("source", choices=["cohort", "synthetic"])
    ap.add_argument("--replace", action="store_true", help="empty the database first")
    ap.add_argument("--if-empty", action="store_true", help="do nothing if a seed is present")
    ap.add_argument("--seed", type=int, default=42, help="synthetic generator seed")
    ap.add_argument("--customers", type=int, default=200, help="synthetic customers")
    ap.add_argument("--report-dir", default="eval/reports", help="synthetic ingestion report")
    args = ap.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level)
    if not settings.admin_database_url:
        raise SystemExit("ADMIN_DATABASE_URL is not set")
    # Both sources load customers, whose document numbers are stored only as a keyed hash.
    if not settings.document_hash_key:
        raise SystemExit("DOCUMENT_HASH_KEY is not set in .env; run make init to generate it")
    db = Database(settings.admin_database_url)
    try:
        with db.session() as s:
            existing = seeding.current_source(s)
            if existing and args.if_empty:
                log.info("seed_skipped", existing=existing)
                return 0
            seeding.prepare(s, replace=args.replace)
            if args.source == "cohort":
                cohort_dir = Path(settings.data_dir) / "gold" / "cohort"
                detail = seeding.load_cohort(s, cohort_dir, settings.document_hash_key)
            else:
                report = ingest_rows(
                    s,
                    "synthetic",
                    settings.document_hash_key,
                    *generate(settings.trazo_now, seed=args.seed, n_customers=args.customers),
                )
                report.write(args.report_dir)
                detail = {"seed": args.seed, "customers": args.customers, **report.to_dict()}
            seeding.record(s, args.source, detail)
    except seeding.SeedError as e:
        raise SystemExit(str(e)) from e
    finally:
        db.dispose()
    log.info("seed_done", seeded=args.source, replaced=bool(existing), detail=detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

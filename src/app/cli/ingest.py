"""Ingestion entrypoint.

python -m app.cli.ingest synthetic --seed 42
python -m app.cli.ingest cfpb --path data/raw/complaints.csv --limit 5000
python -m app.cli.ingest ulb  --path data/raw/creditcard.csv --limit 50000
python -m app.cli.ingest factored --path data/raw/<challenge file>   (adapter in step 2.2)
"""

import argparse
from typing import Any

from app.adapters.db.session import Database
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.services.ingestion import ingest_rows

log = get_logger("ingest")

Rows = tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]


def _load(source: str, path: str | None, limit: int | None, seed: int) -> Rows | None:
    if source == "synthetic":
        from app.adapters.ingest.synthetic import generate

        return generate(seed=seed)
    if path is None:
        raise SystemExit(f"--path is required for source '{source}'")
    if source == "cfpb":
        from app.adapters.ingest.cfpb import load as load_cfpb

        return load_cfpb(path, limit=limit)
    if source == "ulb":
        from app.adapters.ingest.ulb import load as load_ulb

        return load_ulb(path, limit=limit)
    return None


def main(argv: list[str] | None = None) -> int:
    """Loads one source into the canonical tables and writes the quality report.

    Args:
        argv: Command-line arguments; defaults to sys.argv.

    Returns:
        Process exit code: 0 on success, 2 when the source has no adapter yet.
    """
    ap = argparse.ArgumentParser(prog="ingest")
    ap.add_argument("source", choices=["synthetic", "cfpb", "ulb", "factored"])
    ap.add_argument("--path", help="raw file for non-synthetic sources")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--db", help="override DATABASE_URL")
    ap.add_argument("--report-dir", default="eval/reports")
    args = ap.parse_args(argv)

    settings = Settings(database_url=args.db) if args.db else Settings()
    configure_logging(settings.log_level)

    rows = _load(args.source, args.path, args.limit, args.seed)
    if rows is None:
        log.error("adapter_missing", source=args.source, step="2.2")
        return 2

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

"""`make migrate`: applies the SQL migrations of db/migrations as the schema owner.

python -m app.cli.migrate
"""

from app.adapters.db.migrations import apply_migrations
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger

log = get_logger("migrate")


def main() -> int:
    """Applies the pending migrations.

    Returns:
        Process exit code: 0 on success.
    """
    settings = Settings()
    configure_logging(settings.log_level)
    if not settings.admin_database_url:
        raise SystemExit("ADMIN_DATABASE_URL is not set")
    applied = apply_migrations(settings.admin_database_url, settings.database_url)
    log.info("migrate_done", applied=applied)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

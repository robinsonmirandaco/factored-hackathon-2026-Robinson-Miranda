"""Applies the versioned SQL migrations of db/migrations in order (`make migrate`).

Each file runs once, in its own transaction, and is recorded in `schema_migrations` with its
SHA-256. An applied file that changed afterwards stops the run: a migration is fixed with a new
file, never by editing an old one.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from psycopg import sql
from sqlalchemy import create_engine, make_url, text

from app.core.logging import get_logger

log = get_logger("migrate")

MIGRATIONS_DIR = Path("db/migrations")
APP_ROLE = "trazo_app"


class MigrationError(RuntimeError):
    """A migration cannot be applied safely."""


@dataclass(frozen=True)
class Migration:
    """One SQL file.

    Attributes:
        version: File name without extension, such as 0001_roles.
        sql: File contents.
        sha256: Hash of the contents.
    """

    version: str
    sql: str
    sha256: str


def load_migrations(folder: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Reads every migration file, sorted by name.

    Args:
        folder: Directory with the .sql files.

    Returns:
        The migrations in apply order.
    """
    out = []
    for path in sorted(folder.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        out.append(Migration(path.stem, sql, hashlib.sha256(sql.encode()).hexdigest()))
    return out


def apply_migrations(admin_url: str, app_url: str, folder: Path = MIGRATIONS_DIR) -> list[str]:
    """Applies the pending migrations and sets the password of the application role.

    Tables land in the first schema of the admin URL's search_path, so tests can migrate a
    throwaway schema exactly like `public`.

    Args:
        admin_url: URL of the schema owner.
        app_url: URL the API connects with; its user must be trazo_app and its password is
            given to that role.
        folder: Directory with the .sql files.

    Returns:
        Versions applied by this call; empty when the schema was up to date.

    Raises:
        MigrationError: If app_url is not for trazo_app or an applied file has changed.
    """
    app = make_url(app_url)
    if app.username != APP_ROLE:
        raise MigrationError(f"DATABASE_URL must connect as {APP_ROLE}, not {app.username}")
    engine = create_engine(admin_url)
    applied_now = []
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version text PRIMARY KEY, sha256 text NOT NULL, "
                    "applied_at timestamp NOT NULL DEFAULT now())"
                )
            )
            applied = dict(
                conn.execute(text("SELECT version, sha256 FROM schema_migrations")).all()
            )
        for m in load_migrations(folder):
            if m.version in applied:
                if applied[m.version] != m.sha256:
                    raise MigrationError(f"{m.version} changed after it was applied")
                continue
            with engine.begin() as conn:
                # Straight to psycopg with no parameters: it sends the file as one script and
                # leaves the % of format() alone, which SQLAlchemy would pass as placeholders.
                conn.connection.driver_connection.execute(m.sql)
                conn.execute(
                    text("INSERT INTO schema_migrations (version, sha256) VALUES (:v, :s)"),
                    {"v": m.version, "s": m.sha256},
                )
            applied_now.append(m.version)
            log.info("migration_applied", version=m.version)
        with engine.begin() as conn:
            # ALTER ROLE takes no bind parameters, so psycopg quotes the password as a literal.
            stmt = sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(APP_ROLE), sql.Literal(app.password or "")
            )
            conn.connection.driver_connection.execute(stmt)
    finally:
        engine.dispose()
    return applied_now

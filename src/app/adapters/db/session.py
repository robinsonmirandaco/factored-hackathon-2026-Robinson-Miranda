"""Database engine and session lifecycle on Postgres."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, make_url, text
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.models import Base


class Database:
    """Owns one SQLAlchemy engine and the session factory bound to it."""

    def __init__(self, url: str) -> None:
        """Creates the engine. No connection is opened until the first query.

        Args:
            url: SQLAlchemy database URL (postgresql+psycopg://...).
        """
        self.engine = create_engine(url, pool_pre_ping=True)
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        """Creates missing tables. Existing tables are left untouched."""
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Opens a session that commits on success and rolls back on any error.

        Yields:
            An open SQLAlchemy session.
        """
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """Closes every pooled connection."""
        self.engine.dispose()


@contextmanager
def isolated_schema(url: str, prefix: str) -> Iterator[str]:
    """Creates a throwaway schema and yields a URL whose connections use only that schema.

    Golden cases and integration tests each run in their own schema, so they never touch the
    tables of the database they point at and do not depend on each other. The schema is
    dropped on exit, also when the body fails.

    Args:
        url: Postgres URL of the database that hosts the schema.
        prefix: Lowercase prefix of the schema name; a random suffix keeps names unique.

    Yields:
        The same URL with search_path set to the new schema.
    """
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url)
    try:
        with admin.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA {name}"))
        scoped = make_url(url).update_query_dict({"options": f"-csearch_path={name}"})
        try:
            yield scoped.render_as_string(hide_password=False)
        finally:
            with admin.begin() as conn:
                conn.execute(text(f"DROP SCHEMA {name} CASCADE"))
    finally:
        admin.dispose()

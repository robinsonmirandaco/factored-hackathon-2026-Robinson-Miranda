"""Database engine and session lifecycle. Postgres in compose, SQLite for local runs and tests."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.models import Base


class Database:
    """Owns one SQLAlchemy engine and the session factory bound to it."""

    def __init__(self, url: str) -> None:
        """Creates the engine. No connection is opened until the first query.

        Args:
            url: SQLAlchemy database URL.
        """
        is_sqlite = url.startswith("sqlite")
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if is_sqlite:
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(url, **kwargs)
        if is_sqlite:
            # SQLite ignores foreign keys unless each connection turns them on.
            event.listen(self.engine, "connect", _enable_sqlite_foreign_keys)
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


def _enable_sqlite_foreign_keys(dbapi_conn: Any, _record: Any) -> None:
    dbapi_conn.execute("PRAGMA foreign_keys=ON")

"""Database engine, session lifecycle and the row level security context on Postgres.

Every transaction starts by fixing `app.customer_id` and `app.role` with
`set_config(..., true)`. The `true` makes both values last only until the transaction ends, so
a pooled connection never carries one customer's context into another customer's request. A
session-level SET would survive the commit and leak.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import Connection, create_engine, event, make_url, text
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker

from app.adapters.db.migrations import apply_migrations

Role = Literal["analyst", "auth"]

_SET_CONTEXT = text(
    "SELECT set_config('app.customer_id', :customer_id, true), set_config('app.role', :role, true)"
)


def _context(session: Session) -> dict[str, str]:
    return {
        "customer_id": session.info.get("customer_id") or "",
        "role": session.info.get("role") or "",
    }


def _apply_context(session: Session, _transaction: SessionTransaction, conn: Connection) -> None:
    conn.execute(_SET_CONTEXT, _context(session))


def bind_context(
    session: Session, customer_id: str | None = None, role: Role | None = None
) -> None:
    """Sets whose rows the session may see, for this and every later transaction of it.

    Args:
        session: Open session.
        customer_id: Customer whose rows are visible, or None.
        role: "analyst" to see every customer's rows, "auth" to file an authentication event
            that has no customer, or None.
    """
    session.info["customer_id"] = customer_id
    session.info["role"] = role
    if session.in_transaction():
        session.execute(_SET_CONTEXT, _context(session))


class PrivilegedRoleError(RuntimeError):
    """The service connected with a role that bypasses row level security."""


class Database:
    """Owns one SQLAlchemy engine and the session factory bound to it."""

    def __init__(self, url: str, **engine_kwargs: Any) -> None:
        """Creates the engine. No connection is opened until the first query.

        Args:
            url: SQLAlchemy database URL (postgresql+psycopg://...).
            **engine_kwargs: Extra create_engine options, such as the pool size.
        """
        self.engine = create_engine(url, pool_pre_ping=True, **engine_kwargs)
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        event.listen(self._sessions, "after_begin", _apply_context)

    @contextmanager
    def session(
        self, customer_id: str | None = None, role: Role | None = None, keep: bool = True
    ) -> Iterator[Session]:
        """Opens a session that commits on success and rolls back on any error.

        Args:
            customer_id: Customer whose rows the session may see.
            role: "analyst" to see every customer's rows.
            keep: False rolls the work back on success too, to try something without keeping
                it. Sequences still advance: nextval is not transactional.

        Yields:
            An open SQLAlchemy session.
        """
        session = self._sessions(info={"customer_id": customer_id, "role": role})
        try:
            yield session
            if keep:
                session.commit()
            else:
                session.rollback()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def assert_unprivileged(self) -> None:
        """Checks that the connected role cannot bypass row level security.

        Raises:
            PrivilegedRoleError: If the role is a superuser or has BYPASSRLS.
        """
        with self.engine.connect() as conn:
            name, superuser, bypass = conn.execute(
                text(
                    "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            ).one()
        if superuser or bypass:
            raise PrivilegedRoleError(
                f"role {name} bypasses row level security; connect as trazo_app"
            )

    def dispose(self) -> None:
        """Closes every pooled connection."""
        self.engine.dispose()


@dataclass(frozen=True)
class SchemaUrls:
    """URLs bound to one schema.

    Attributes:
        admin: As the schema owner, for fixtures and seeds.
        app: As trazo_app, for the API.
    """

    admin: str
    app: str


def _scoped(url: str, schema: str) -> str:
    scoped = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    return scoped.render_as_string(hide_password=False)


@contextmanager
def isolated_schema(admin_url: str, app_url: str, prefix: str) -> Iterator[SchemaUrls]:
    """Creates a throwaway schema, applies every migration to it and yields URLs bound to it.

    Golden cases and integration tests each run in their own schema, so they never touch the
    tables of the database they point at and do not depend on each other. The schema is
    dropped on exit, also when the body fails.

    Args:
        admin_url: URL of the schema owner.
        app_url: URL of trazo_app on the same database.
        prefix: Lowercase prefix of the schema name; a random suffix keeps names unique.

    Yields:
        Owner and application URLs with search_path set to the new schema.
    """
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    admin = create_engine(admin_url)
    try:
        with admin.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA {name}"))
        try:
            urls = SchemaUrls(admin=_scoped(admin_url, name), app=_scoped(app_url, name))
            apply_migrations(urls.admin, urls.app)
            yield urls
        finally:
            with admin.begin() as conn:
                conn.execute(text(f"DROP SCHEMA {name} CASCADE"))
    finally:
        admin.dispose()

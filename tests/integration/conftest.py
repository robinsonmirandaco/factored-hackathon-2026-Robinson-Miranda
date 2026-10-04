"""Integration fixtures. They need Postgres, reached through ADMIN_DATABASE_URL (the owner) and
DATABASE_URL (trazo_app), as in docker compose and CI."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, make_url, text

from app.adapters.db.session import SchemaUrls, isolated_schema
from app.core.config import Settings
from app.core.logging import trace_id_var
from tests.auth_support import TEST_ANALYST_PASSWORD, TEST_JWT_SECRET
from tests.serving_data import KEY


@pytest.fixture(autouse=True)
def auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test-only session secret, analyst password and document key, and the fixed demo code.

    Set in the environment so every Settings() of a test gets them, whatever the local .env
    holds. The document key is the one test fixtures hash documents with, so a login finds the
    customer. Tests of the random code turn demo mode off themselves.
    """
    monkeypatch.setenv("DOCUMENT_HASH_KEY", KEY)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("ANALYST_DEMO_USER", "analista.demo")
    monkeypatch.setenv("ANALYST_DEMO_PASSWORD", TEST_ANALYST_PASSWORD)
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_OTP_CODE", "482913")


@pytest.fixture
def schema() -> Iterator[SchemaUrls]:
    """Yields owner and trazo_app URLs bound to a migrated throwaway schema, dropped after."""
    s = Settings()
    assert s.admin_database_url, "ADMIN_DATABASE_URL is not set"
    with isolated_schema(s.admin_database_url, s.database_url, "test") as urls:
        yield urls


@pytest.fixture
def database_url(schema: SchemaUrls) -> str:
    """URL of trazo_app on the throwaway schema: what the API connects with."""
    return schema.app


@pytest.fixture
def role_that_creates_roles(schema: SchemaUrls) -> Iterator[str]:
    """Yields the URL of a throwaway login role with CREATEROLE, dropped after.

    Neither superuser nor BYPASSRLS, so it is privileged only through CREATEROLE.
    """
    name = f"createrole_{uuid.uuid4().hex[:12]}"
    password = uuid.uuid4().hex
    owner = create_engine(schema.admin)
    try:
        with owner.begin() as conn:
            conn.execute(text(f"CREATE ROLE {name} LOGIN CREATEROLE PASSWORD '{password}'"))
        url = make_url(schema.admin).set(username=name, password=password)
        yield url.render_as_string(hide_password=False)
    finally:
        with owner.begin() as conn:
            conn.execute(text(f"DROP ROLE IF EXISTS {name}"))
        owner.dispose()


@pytest.fixture(autouse=True)
def trace_id() -> Iterator[str]:
    """Binds a trace_id for tests that call services without a request, as the middleware does.

    The audit log refuses the "-" of code running outside a request (TRZ-26).
    """
    token = trace_id_var.set(f"test-{uuid.uuid4().hex[:8]}")
    yield trace_id_var.get()
    trace_id_var.reset(token)

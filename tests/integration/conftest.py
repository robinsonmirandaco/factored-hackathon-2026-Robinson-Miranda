"""Integration fixtures. They need Postgres, reached through ADMIN_DATABASE_URL (the owner) and
DATABASE_URL (trazo_app), as in docker compose and CI."""

from collections.abc import Iterator

import pytest

from app.adapters.db.session import SchemaUrls, isolated_schema
from app.core.config import Settings


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

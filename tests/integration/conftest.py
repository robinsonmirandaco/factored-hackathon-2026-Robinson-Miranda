"""Integration fixtures. They need the Postgres of docker compose, reached through DATABASE_URL."""

from collections.abc import Iterator

import pytest

from app.adapters.db.session import isolated_schema
from app.core.config import Settings


@pytest.fixture
def database_url() -> Iterator[str]:
    """Yields a URL bound to a throwaway schema, dropped after the test."""
    with isolated_schema(Settings().database_url, "test") as url:
        yield url

"""Versioned migrations (TRZ-07 CA4, CA8): they create the serving schema, run once, refuse an
edited file, and the ORM maps only columns that exist with the same type."""

import shutil
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.adapters.db.migrations import MIGRATIONS_DIR, MigrationError, apply_migrations
from app.adapters.db.models import Base
from app.adapters.db.session import SchemaUrls

pytestmark = pytest.mark.integration

SERVING_TABLES = {
    "customers",
    "products",
    "transactions",
    "exchange_rates",
    "complaints",
    "disputes",
    "card_blocks",
    "cases",
    "case_queue",
    "audit_log",
}


def test_migrations_create_the_serving_tables_and_history_view(schema: SchemaUrls) -> None:
    engine = create_engine(schema.admin)
    insp = inspect(engine)
    tables, views = set(insp.get_table_names()), set(insp.get_view_names())
    engine.dispose()
    assert SERVING_TABLES <= tables
    assert "case_history" in views
    assert "interactions" not in tables


def test_second_run_applies_nothing(schema: SchemaUrls) -> None:
    assert apply_migrations(schema.admin, schema.app) == []


def test_an_edited_migration_stops_the_run(schema: SchemaUrls, tmp_path: Path) -> None:
    folder = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, folder)
    edited = folder / "0002_schema.sql"
    edited.write_text(edited.read_text() + "\n-- edited\n")
    with pytest.raises(MigrationError, match="0002_schema changed"):
        apply_migrations(schema.admin, schema.app, folder)


def test_app_url_must_be_the_application_role(schema: SchemaUrls) -> None:
    with pytest.raises(MigrationError, match="must connect as trazo_app"):
        apply_migrations(schema.admin, schema.admin)


def test_orm_columns_exist_with_the_same_type(schema: SchemaUrls) -> None:
    def kind(t: object) -> type:
        py = t.python_type  # type: ignore[attr-defined]
        # Money columns are read as float on purpose; the database keeps numeric.
        return float if py is Decimal else py

    engine = create_engine(schema.admin)
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        db_cols = {c["name"]: c["type"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            assert col.name in db_cols, f"{table.name}.{col.name}"
            assert kind(col.type) is kind(db_cols[col.name]), f"{table.name}.{col.name}"
    engine.dispose()

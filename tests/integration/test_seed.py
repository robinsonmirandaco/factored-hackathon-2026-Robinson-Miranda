"""Seeding (TRZ-07 CA6): the cohort files the pipeline writes load into the migrated schema, the
document number is kept only as its keyed hash, and two sources never share a database."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.adapters.db.session import Database, SchemaUrls
from app.adapters.ingest.synthetic import generate
from app.cli import seed as seed_cli
from app.domain.pii import document_hash
from app.services import seeding
from app.services.ingestion import ingest_rows
from tests.pipeline_data import TRAZO_NOW, build_dataset, run_pipeline, write_dataset
from tests.serving_data import KEY

pytestmark = pytest.mark.integration


@pytest.fixture
def cohort_dir(tmp_path: Path) -> Path:
    write_dataset(tmp_path, build_dataset([date(2024, 1, 10), date(2024, 1, 11)]))
    run_pipeline(tmp_path)
    return tmp_path / "gold" / "cohort"


def _seed_cohort(url: str, cohort_dir: Path, replace: bool = False) -> dict:
    db = Database(url)
    try:
        with db.session() as s:
            seeding.prepare(s, replace=replace)
            detail = seeding.load_cohort(s, cohort_dir, KEY)
            seeding.record(s, "cohort", detail)
    finally:
        db.dispose()
    return detail


def test_pipeline_cohort_loads_with_documents_hashed(schema: SchemaUrls, cohort_dir: Path) -> None:
    detail = _seed_cohort(schema.admin, cohort_dir)

    assert detail["rows"]["customers"] == 3
    assert detail["rows"]["transactions"] > 0 and detail["rows"]["exchange_rates"] > 0
    db = Database(schema.admin)
    with db.session() as s:
        columns = {c["name"] for c in inspect(s.connection()).get_columns("customers")}
        stored = s.execute(
            text("SELECT document_hash FROM customers WHERE customer_id = 'CUS-2'")
        ).scalar_one()
        counts = {
            t: s.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in seeding.COHORT_TABLES
        }
    db.dispose()
    assert "document_number" not in columns
    # tests/pipeline_data gives CUS-2 a CC numbered 900002.
    assert stored == document_hash(KEY, "CC", "900002")
    assert counts == detail["rows"]


def test_a_second_seed_needs_replace_and_then_leaves_one_source(
    schema: SchemaUrls, cohort_dir: Path
) -> None:
    first = _seed_cohort(schema.admin, cohort_dir)
    with pytest.raises(seeding.SeedError, match="already holds the cohort seed"):
        _seed_cohort(schema.admin, cohort_dir)

    db = Database(schema.admin)
    with db.session() as s:
        seeding.prepare(s, replace=True)
        ingest_rows(s, "synthetic", KEY, *generate(TRAZO_NOW, n_customers=5, dirty=False))
        seeding.record(s, "synthetic", {})
    with db.session() as s:
        sources = s.execute(text("SELECT source FROM seed_runs")).scalars().all()
        cohort_left = s.execute(
            text("SELECT count(*) FROM customers WHERE customer_id LIKE 'CUS-%'")
        ).scalar_one()
    db.dispose()
    assert first["rows"]["customers"] == 3
    assert sources == ["synthetic"]
    assert cohort_left == 0


def test_seed_refuses_an_empty_document_key(schema: SchemaUrls, cohort_dir: Path) -> None:
    db = Database(schema.admin)
    with pytest.raises(ValueError, match="DOCUMENT_HASH_KEY"):
        with db.session() as s:
            seeding.load_cohort(s, cohort_dir, "")
    db.dispose()


def test_seed_command_loads_synthetic_once_and_if_empty_skips(
    schema: SchemaUrls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_DATABASE_URL", schema.admin)
    monkeypatch.setenv("DOCUMENT_HASH_KEY", KEY)
    args = ["synthetic", "--report-dir", str(tmp_path)]

    assert seed_cli.main(args) == 0
    assert seed_cli.main([*args, "--if-empty"]) == 0
    with pytest.raises(SystemExit, match="already holds the synthetic seed"):
        seed_cli.main(args)
    db = Database(schema.admin)
    with db.session() as s:
        runs = s.execute(text("SELECT count(*) FROM seed_runs")).scalar_one()
        rejected = s.execute(text("SELECT count(*) FROM quarantine")).scalar_one()
    db.dispose()
    assert runs == 1
    # The generator's five dirty transactions land in quarantine with their reason.
    assert rejected == 5

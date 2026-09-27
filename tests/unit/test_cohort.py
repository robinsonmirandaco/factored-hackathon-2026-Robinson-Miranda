"""Serving cohort (TRZ-07 CA1 to CA3): stratified size, proportions, determinism and the check
that evaluation splits lie inside the cohort."""

from datetime import date
from pathlib import Path

import duckdb
import pytest

from pipeline.cohort import allocate, missing_from_cohort
from tests.pipeline_data import build_dataset, run_pipeline, write_dataset

# Made-up population with the shape of the real one: 3 countries by 4 segments.
POPULATION = {
    (country, segment): n
    for country, weight in (("AR", 2), ("CO", 3), ("MX", 5))
    for segment, n in (
        ("Basic", 6_000 * weight + 7),
        ("Plus", 2_500 * weight + 3),
        ("Premium", 1_000 * weight + 1),
        ("Student", 500 * weight),
    )
}


def test_allocation_adds_up_exactly_and_stays_within_two_points() -> None:
    seats = allocate(POPULATION, 5000)
    total = sum(POPULATION.values())

    assert sum(seats.values()) == 5000
    for k, n in POPULATION.items():
        assert abs(100 * seats[k] / 5000 - 100 * n / total) < 2
        # Largest remainder never moves a stratum more than one seat off its exact quota.
        assert abs(seats[k] - n * 5000 / total) < 1


def test_allocation_is_deterministic_and_takes_everyone_when_asked_for_more() -> None:
    assert allocate(POPULATION, 5000) == allocate(dict(reversed(POPULATION.items())), 5000)
    assert allocate({("AR", "Basic"): 2, ("CO", "Plus"): 1}, 10) == {
        ("AR", "Basic"): 2,
        ("CO", "Plus"): 1,
    }


def test_ties_go_to_the_stratum_that_sorts_first() -> None:
    assert allocate({("CO", "Basic"): 1, ("AR", "Basic"): 1}, 1) == {
        ("AR", "Basic"): 1,
        ("CO", "Basic"): 0,
    }


def test_split_inside_the_cohort_has_nothing_missing() -> None:
    # Fixture split until TRZ-42 writes the real ones (accepted deviation of CA3).
    cohort = ["CUS-1", "CUS-2", "CUS-3"]
    assert missing_from_cohort(cohort, ["CUS-2", "CUS-3", "CUS-2"]) == []
    assert missing_from_cohort(cohort, ["CUS-3", "CUS-9", "CUS-8"]) == ["CUS-8", "CUS-9"]


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    write_dataset(tmp_path, build_dataset([date(2024, 1, 10), date(2024, 1, 11)]))
    return tmp_path


def test_pipeline_writes_the_cohort_and_its_report(data_dir: Path) -> None:
    result = run_pipeline(data_dir)
    folder = data_dir / "gold" / "cohort"
    con = duckdb.connect()

    assert sum(result.cohort.cohort.values()) == 3  # smaller population than 5,000: everyone
    assert {p.name for p in folder.iterdir()} == {
        "customers.parquet",
        "products.parquet",
        "transactions.parquet",
        "complaints.parquet",
        "exchange_rates.parquet",
    }
    described = con.execute(f"DESCRIBE SELECT * FROM '{folder / 'transactions.parquet'}'")
    columns = {r[0]: r[1] for r in described.fetchall()}
    assert "is_fraud" not in columns and "fraud_score" not in columns
    # Naive local time, the convention of TRAZO_NOW and the serving database.
    assert columns["transaction_date"] == "TIMESTAMP"
    report = (data_dir / "cohorte.md").read_text()
    assert "Cohort size: 3" in report and result.cohort.ids_sha256 in report


def test_cohort_output_is_identical_across_runs(data_dir: Path) -> None:
    first = run_pipeline(data_dir).outputs
    second = run_pipeline(data_dir).outputs
    cohort = {k: v for k, v in first.items() if k.startswith("gold/cohort/")}
    assert len(cohort) == 5
    assert cohort == {k: v for k, v in second.items() if k.startswith("gold/cohort/")}

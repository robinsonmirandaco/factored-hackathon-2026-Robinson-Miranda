"""`make report-impact`: dispute agent hours and their cost from the gold interactions mart."""

from pathlib import Path

import duckdb
import pytest

from pipeline.impact import (
    ALL_COMPLAINTS,
    DISPUTE_COMPLAINTS,
    LLM_COST_PER_SAFE_RESOLUTION_USD,
    SAFE_RESOLUTION,
    complaint_minutes,
    dispute_contacts_per_year,
    dispute_hours_per_year,
    write_report,
)
from pipeline.settings import PipelineSettings
from pipeline.silver import sql_str


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    gold = tmp_path / "gold"
    gold.mkdir()
    # Two complaint rows over 2024-01-01 to 2024-12-31 (366 days) and one row of another
    # category that must not count.
    duckdb.execute(
        f"""
        COPY (
            SELECT * FROM (VALUES
                ('Queja', 10, 8, 3600.0, DATE '2024-01-01', 'b1'),
                ('Queja', 5, 4, 2400.0, DATE '2024-12-31', 'b1'),
                ('Producto', 7, 7, 9999.0, DATE '2024-06-01', 'b1')
            ) t(reason_category, contacts, contacts_with_duration, duration_seconds,
                partition_date, batch_id)
        ) TO {sql_str(str(gold / "demand_interactions.parquet"))} (FORMAT PARQUET)
        """
    )
    return tmp_path


def test_complaint_minutes_sums_only_complaint_contacts(data_dir: Path) -> None:
    figures = complaint_minutes(duckdb.connect(), data_dir)

    assert (figures.contacts, figures.contacts_with_duration, figures.minutes) == (15, 12, 100.0)
    assert figures.days == 366
    assert figures.batch_id == "b1"


def test_dispute_hours_apply_the_share_and_the_annual_factor(data_dir: Path) -> None:
    figures = complaint_minutes(duckdb.connect(), data_dir)

    expected = 100.0 * (DISPUTE_COMPLAINTS / ALL_COMPLAINTS) * (365.25 / 366) / 60
    assert dispute_hours_per_year(figures) == pytest.approx(expected)


def test_report_labels_the_assumption_and_the_projection(data_dir: Path) -> None:
    settings = PipelineSettings(data_dir=data_dir, impact_report_path=data_dir / "impacto.md")

    write_report(settings)
    report = (data_dir / "impacto.md").read_text()

    assert "## Agent hours per year attributed to disputes [projection]" in report
    assert "## Annual cost of those hours [projection]" in report
    assert "24,491 / 67,095 = 36.50%" in report
    assert "Centris Information Services" in report and "2026-07-28" in report


def test_offline_rates_match_the_published_evaluation_report() -> None:
    evaluation = Path("docs/reports/evaluacion.md").read_text()

    central = 100 * SAFE_RESOLUTION["central"]
    conservative = 100 * SAFE_RESOLUTION["conservative"]
    assert f"| {central:.1f}% [{conservative:.1f}%, 75.6%] (233/352);" in evaluation
    assert f"| Cost per safe resolution (USD) | {LLM_COST_PER_SAFE_RESOLUTION_USD} |" in evaluation


def test_freed_hours_and_llm_cost_apply_each_scenario_rate(data_dir: Path) -> None:
    settings = PipelineSettings(data_dir=data_dir, impact_report_path=data_dir / "impacto.md")
    figures = complaint_minutes(duckdb.connect(), data_dir)

    write_report(settings)
    report = (data_dir / "impacto.md").read_text()

    rate = SAFE_RESOLUTION["central"]
    freed = dispute_hours_per_year(figures) * rate
    llm = dispute_contacts_per_year(figures) * rate * LLM_COST_PER_SAFE_RESOLUTION_USD
    assert dispute_contacts_per_year(figures) == pytest.approx(
        15 * (DISPUTE_COMPLAINTS / ALL_COMPLAINTS) * (365.25 / 366)
    )
    assert "## Agent hours TRAZO would free [projection]" in report
    assert "| central | 66.2% |" in report and "| conservative | 56.8% |" in report
    assert f"| {freed * 12:,.0f} | {freed * 23:,.0f} | {llm:,.2f} |" in report


def test_report_without_gold_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        write_report(PipelineSettings(data_dir=tmp_path, impact_report_path=tmp_path / "x.md"))

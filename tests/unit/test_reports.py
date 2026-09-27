"""`make report-data`: quality and demand reports from silver and gold (TRZ-06)."""

from datetime import date, time
from pathlib import Path

import duckdb
import pytest

from pipeline.demand import category_rows, dispute_figures
from pipeline.report import cutoff_holds
from pipeline.reports import write_reports
from pipeline.settings import PipelineSettings
from pipeline.silver import sql_str
from tests.pipeline_data import TRAZO_NOW, Rows, build_dataset, run_pipeline, write_dataset

DAYS = [date(2024, 1, 10), date(2024, 1, 11)]


def _settings(data_dir: Path) -> PipelineSettings:
    return PipelineSettings(
        data_dir=data_dir,
        normalization_path=Path("config/normalization.yaml"),
        quality_report_path=data_dir / "reports" / "calidad.md",
        demand_report_path=data_dir / "reports" / "demanda.md",
        trazo_now=TRAZO_NOW,
        seed=42,
    )


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    rows: Rows = build_dataset(DAYS)
    rows["complaints"][0].update({"subcategory": "Cargo no reconocido", "sla_breached": "True"})
    rows["complaints"][1].update(
        {"subcategory": "Problema con app", "claimed_amount": "", "currency": ""}
    )
    rows["call_center_interactions"][1].update(
        {"interaction_type": "Email", "duration_seconds": "", "wait_time_seconds": ""}
    )
    write_dataset(tmp_path, rows)
    run_pipeline(tmp_path)
    return tmp_path


def _silver(data_dir: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    glob = sql_str(str(data_dir / "silver" / "complaints" / "*" / "*.parquet"))
    con.execute(f"CREATE VIEW silver_complaints AS SELECT * FROM read_parquet({glob})")
    return con


def test_report_data_rewrites_the_quality_report_of_make_data(data_dir: Path) -> None:
    written_by_data = (data_dir / "calidad.md").read_text()

    write_reports(_settings(data_dir))

    assert (data_dir / "reports" / "calidad.md").read_text() == written_by_data
    assert (data_dir / "reports" / "demanda.md").exists()


def test_quality_report_covers_every_design_finding(data_dir: Path) -> None:
    write_reports(_settings(data_dir))
    report = (data_dir / "reports" / "calidad.md").read_text()

    for finding in (
        "Template texts",
        "Complaints without link",
        "Events dated after their partition",
        "Broken referential integrity",
        "Declared against found quality",
        "Incoherent document",
        "Incoherent currency",
        "Fewer rows than documented",
        "Values in another language",
        "No exact duplicates or repeated keys",
        "High nulls in optional fields",
        "Candidate density",
    ):
        assert f"| {finding} |" in report
    assert "| complaints | 80,000 | 2 | -79,998 |" in report
    assert "not confirmed" in report


def test_demand_report_names_sources_and_hides_personal_values(data_dir: Path) -> None:
    write_reports(_settings(data_dir))
    report = (data_dir / "reports" / "demanda.md").read_text()

    for section in (
        "## Contacts by category",
        "## Dispute complaints",
        "### By month",
        "### By weekday",
        "### By hour of the day",
        "### By country",
        "### By channel",
        "## Operational constraints",
    ):
        assert section in report
    assert report.count("Source: ") >= 4
    assert "presence of the field only" in report
    for personal in ("Name1", "Surname1", "900001", "user1@example.test", "123456781", "Text"):
        assert personal not in report


def test_dispute_figures_split_all_complaints_from_disputes(data_dir: Path) -> None:
    figures = dispute_figures(_silver(data_dir))

    assert figures["rows"] == (2, 1)
    assert figures["with_amount"] == (1, 1)
    assert figures["with_product"] == (2, 1)
    assert figures["with_both"] == (1, 1)
    assert figures["sla_breached"] == (1, 1)
    assert figures["with_origin"] == (0, 0)


def test_minutes_per_contact_leave_out_contacts_without_duration(data_dir: Path) -> None:
    [row] = category_rows(duckdb.connect(), data_dir)

    category, contacts, share, _, per_contact, *_ = row
    assert (category, contacts, share) == ("Queja", 2, 1)
    assert per_contact == pytest.approx(5.0)


def test_report_data_without_make_data_output_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        write_reports(_settings(tmp_path))


def _offset(next_day: int, after: int, earliest: time, latest: time) -> tuple:
    return ("transactions", "AR", 10, 10 - after, next_day, after, earliest, latest, 0)


def test_cutoff_holds_when_events_stay_inside_the_operational_day() -> None:
    assert cutoff_holds([_offset(3, 3, time(6), time(6))])


@pytest.mark.parametrize(
    "offset",
    [
        _offset(3, 3, time(5, 59), time(6)),
        _offset(3, 3, time(6), time(6, 0, 1)),
        _offset(3, 4, time(6), time(6)),
    ],
    ids=["event before cutoff", "next day after cutoff", "two days later"],
)
def test_cutoff_does_not_hold_when_an_event_leaves_the_operational_day(offset: tuple) -> None:
    assert not cutoff_holds([offset])

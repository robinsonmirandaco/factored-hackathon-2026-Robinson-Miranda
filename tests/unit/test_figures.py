import shutil
import sys
from pathlib import Path

import pytest

from pipeline import figures

REPORTS = Path("docs/reports")


def test_the_figures_read_every_value_from_the_committed_reports() -> None:
    f = figures.load()
    assert [m[0] for m in f.measures] == [
        "Safe automated resolution",
        "Containment",
        "Missed escalations",
        "Unnecessary escalations",
    ]
    assert f.measures[0][1] == figures.Estimate("66.2", "56.8", "75.6", "233/352")
    assert f.difference == ("+37.5", "+30.1", "+45.7")
    assert [a for a, _ in f.coverage] == ["0.2", "0.1", "0.05", "0.02"]
    assert f.service == figures.Estimate("98.0", "95.8", "99.1", "298/304")
    assert [(n, u) for n, _, u in f.variants] == [
        ("amounts_half", "20"),
        ("base", "36"),
        ("alpha_0.10", "40"),
        ("amounts_double", "48"),
    ]
    assert f.wilson == [("10%", "0.00", "0.01"), ("40%", "25.25", "93.76")]


def test_a_section_missing_from_its_report_stops_the_figures(tmp_path: Path) -> None:
    for name in ("evaluacion.md", "analisis.md"):
        shutil.copy(REPORTS / name, tmp_path / name)
    text = (tmp_path / "evaluacion.md").read_text(encoding="utf-8")
    (tmp_path / "evaluacion.md").write_text(
        text.replace("## Sensitivity of the thresholds\n", "## Thresholds\n"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="heading not found"):
        figures.load(tmp_path)


def test_a_row_drawn_but_not_written_is_refused() -> None:
    with pytest.raises(ValueError, match="not written in the report"):
        figures.written("| base | 66.2% |", "66.3%")


def test_loading_the_values_does_not_need_matplotlib() -> None:
    figures.load()
    assert "matplotlib.pyplot" not in sys.modules

"""The golden-case runner must pass the real cases and must catch a wrong expectation."""

import os
import shutil
from pathlib import Path

import pytest
import yaml

from app.cli.eval import run

CASES = Path(__file__).resolve().parents[2] / "eval" / "cases"

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")


def _one_case(tmp_path: Path, name: str, **overrides) -> Path:
    case = yaml.safe_load((CASES / name).read_text())
    case.update(overrides)
    d = tmp_path / "cases"
    d.mkdir()
    (d / name).write_text(yaml.safe_dump(case))
    return d


def test_repo_cases_pass_and_report_is_written(tmp_path):
    results, summary = run(CASES, tmp_path / "reports")
    assert summary["failed"] == 0, [r.id for r in results if r.status in ("fail", "error")]
    assert summary["unexpected_passes"] == 0
    assert summary["passed"] + summary["known_failures"] + summary["skipped"] == summary["cases"]
    assert summary["escalation"]["missed"] == 0
    assert (tmp_path / "reports" / "golden_report.json").exists()
    assert (tmp_path / "reports" / "golden_report.md").exists()


def test_wrong_expectation_is_reported_as_failure(tmp_path):
    d = _one_case(tmp_path, "09_lost_card_freezes.yaml")
    case_file = d / "09_lost_card_freezes.yaml"
    case = yaml.safe_load(case_file.read_text())
    case["turns"][0]["expect"]["outcome"] = "escalated"
    case_file.write_text(yaml.safe_dump(case))

    results, summary = run(d, tmp_path / "reports")
    assert results[0].status == "fail"
    assert summary["failed"] == 1
    assert summary["escalation"]["missed"] == 1


def test_known_failure_that_starts_passing_is_flagged(tmp_path):
    d = _one_case(tmp_path, "09_lost_card_freezes.yaml", known_failure="pretend this is broken")
    results, summary = run(d, tmp_path / "reports")
    assert results[0].status == "unexpected_pass"
    assert summary["unexpected_passes"] == 1


def test_skipped_case_is_not_run_and_keeps_its_reason(tmp_path):
    d = _one_case(tmp_path, "01_blocked_low_risk_en.yaml", skip="rewritten later")
    results, summary = run(d, tmp_path / "reports")
    assert results[0].status == "skipped"
    assert results[0].skipped == "rewritten later"
    assert results[0].turns == []
    assert summary["skipped"] == 1
    assert summary["failed"] == 0
    assert "skipped: rewritten later" in (tmp_path / "reports" / "golden_report.md").read_text()


def test_fixture_rejected_by_validator_is_an_error(tmp_path):
    d = _one_case(tmp_path, "05_blocked_over_limit_escalates.yaml")
    case_file = d / "05_blocked_over_limit_escalates.yaml"
    case = yaml.safe_load(case_file.read_text())
    case["fixtures"]["transactions"][0]["amount"] = -5
    case_file.write_text(yaml.safe_dump(case))

    results, summary = run(d, tmp_path / "reports")
    assert results[0].status == "error"
    assert "fixtures rejected" in results[0].error
    assert summary["failed"] == 1


def test_runner_leaves_database_url_unchanged(tmp_path):
    # Each case runs on its own schema URL; that URL must not leak into the environment.
    before = os.environ.get("DATABASE_URL")
    d = tmp_path / "cases"
    shutil.copytree(CASES, d, ignore=shutil.ignore_patterns("1[1-9]_*"))
    run(d, tmp_path / "reports")
    assert os.environ.get("DATABASE_URL") == before

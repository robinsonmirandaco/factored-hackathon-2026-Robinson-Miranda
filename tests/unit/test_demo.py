"""Demo configuration and exclusion list (TRZ-38), without a database."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.domain.demo import DemoConfig, load_demo
from app.domain.policy import PolicyEngine
from app.services import demo

ROOT = Path(__file__).resolve().parents[2]


def _config() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "config" / "demo.yaml").read_text(encoding="utf-8"))


def test_the_repository_config_is_valid_and_seeds_19_reviews_with_9_reversals() -> None:
    config = load_demo(ROOT / "config" / "demo.yaml")
    assert (config.pt_cell.reviews, config.pt_cell.reversals) == (19, 9)
    config.check_reasons(
        PolicyEngine.from_file(ROOT / "config" / "policy.yaml").config.autonomy.reversal_reasons
    )
    assert {p.document.number for p in config.personas} == {"DEMO-MX-0001", "DEMO-CO-0001"}


def test_the_personas_keep_a_document_type_of_their_country() -> None:
    # The hash covers type and number, so the type must be one the cohort has in that country.
    types = {p.country: p.document.type for p in load_demo(ROOT / "config" / "demo.yaml").personas}
    assert types == {"MX": "DNI", "CO": "CC"}


def test_the_config_holds_no_dataset_id() -> None:
    text = (ROOT / "config" / "demo.yaml").read_text(encoding="utf-8")
    # Cohort ids start with CLI-, PRD-, TRX- or CMP-: none may appear.
    assert not re.search(r"\b(CLI|PRD|TRX|CMP)-[A-Z0-9]{6,}", text)


def test_a_document_number_must_be_an_invented_one() -> None:
    raw = _config()
    raw["personas"][0]["document"]["number"] = "12345678"
    with pytest.raises(ValidationError, match="DEMO-"):
        DemoConfig.model_validate(raw)


def test_a_turn_presses_one_button_at_most() -> None:
    raw = _config()
    raw["seeded"][0]["script"]["turns"][0]["recognition"] = "not_recognized"
    with pytest.raises(ValidationError, match="at most one button"):
        DemoConfig.model_validate(raw)


def test_a_script_cannot_name_a_charge_its_role_does_not_ask_for() -> None:
    raw = _config()
    raw["personas"][0]["rehearsals"][0]["turns"][0]["say"] = "No reconozco {other.merchant}"
    with pytest.raises(ValidationError, match="does not ask for"):
        DemoConfig.model_validate(raw)


def test_a_seeded_script_does_not_name_a_merchant() -> None:
    raw = _config()
    raw["seeded"][0]["script"]["turns"][0] = {"say": "No reconozco {charge.merchant}"}
    with pytest.raises(ValidationError, match="does not name a merchant"):
        DemoConfig.model_validate(raw)


def test_a_reversal_reason_outside_the_policy_list_is_refused() -> None:
    raw = _config()
    raw["pt_cell"]["decisions"][1] = {"reject": "because"}
    with pytest.raises(ValueError, match="because"):
        DemoConfig.model_validate(raw).check_reasons(["wrong_charge"])


def test_roles_and_documents_are_unique() -> None:
    raw = _config()
    raw["seeded"][0]["role"] = raw["personas"][0]["role"]
    with pytest.raises(ValidationError, match="its own name"):
        DemoConfig.model_validate(raw)


def _list(tmp_path: Path, ids: str, sha: str | None = None) -> Path:
    (tmp_path / demo.CASE_CUSTOMERS).write_text(ids)
    digest = sha or hashlib.sha256(ids.encode()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"case_customers": {"sha256": digest}}))
    return manifest


def test_the_exclusion_list_is_read_when_it_matches_the_manifest(tmp_path: Path) -> None:
    manifest = _list(tmp_path, "C1\nC2\n")
    assert demo.read_excluded(tmp_path, manifest) == {"C1", "C2"}


def test_an_exclusion_list_that_changed_is_refused(tmp_path: Path) -> None:
    manifest = _list(tmp_path, "C1\n", sha="0" * 64)
    with pytest.raises(demo.DemoError, match="does not match"):
        demo.read_excluded(tmp_path, manifest)


def test_a_missing_exclusion_list_is_refused(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    with pytest.raises(demo.DemoError, match="make cases"):
        demo.read_excluded(tmp_path, manifest)


def test_the_repository_manifest_records_the_exclusion_list() -> None:
    entry = json.loads((ROOT / "eval" / "splits" / "manifest.json").read_text())["case_customers"]
    assert entry["count"] > 0 and len(entry["sha256"]) == 64
    # A count and a hash only: the ids stay out of git.
    assert set(entry) == {"count", "file", "sha256"}


def test_the_evaluation_code_never_reads_the_demo_state() -> None:
    # CA4: the demo tables, the simulated mark of a case and the demo configuration are unknown
    # to every evaluation path ("simulated clock" is another thing).
    sources = [ROOT / "src" / "app" / "cli" / "eval.py", *sorted((ROOT / "pipeline").rglob("*.py"))]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert not re.search(
            r"demo_roles|demo_reset|\.simulated\b|simulated = |demo_config|config/demo\.yaml", text
        ), path

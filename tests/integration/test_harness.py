"""Evaluation harness against Postgres, with made-up rows (TRZ-43 CA7, CA8).

TRAZO runs with the LLM off (the rules), so it is deterministic.
"""

import pytest

from app.core.config import Settings
from pipeline.harness import CaseRun, Staging, run_case, score
from pipeline.simulated_client import load_templates
from tests.case_support import FixtureData, netflix_case

pytestmark = pytest.mark.integration

TEMPLATES = load_templates()


def rules_staging() -> Staging:
    return Staging(Settings(llm_enabled=False, log_level="WARNING"), FixtureData(), None)


def outcomes(run: CaseRun) -> list[str | None]:
    return [t.outcome for t in run.turns]


def test_two_runs_of_the_same_case_end_in_the_same_state() -> None:
    case = netflix_case()

    first = run_case(case, "trazo", 1, rules_staging(), TEMPLATES)
    second = run_case(case, "trazo", 1, rules_staging(), TEMPLATES)

    assert first.error is None
    assert outcomes(first) == outcomes(second)
    assert first.final == second.final
    assert score(first, case) == score(second, case)


def test_the_run_is_scored_on_the_dispute_and_block_it_left() -> None:
    case = netflix_case()

    run = run_case(case, "trazo", 1, rules_staging(), TEMPLATES)

    assert outcomes(run) == [
        "recognizing",
        "awaiting_confirmation",
        "registered_verified",
        "card_blocked",
    ]
    assert run.final.disputes == [("C1", "TX-1", "unrecognized_charge")]
    assert run.final.blocks == [("C1", "P1")]
    assert score(run, case).correct is True


def test_the_session_expires_before_the_turn_the_case_names() -> None:
    case = netflix_case(expected_action="expired", scenario={"session_expires_at_turn": 2})

    run = run_case(case, "trazo", 1, rules_staging(), TEMPLATES)

    assert outcomes(run) == ["recognizing", "session_ended"]
    assert run.final.disputes == []
    assert score(run, case).correct is True


def test_a_tool_that_does_not_write_leaves_no_dispute_and_a_person_takes_over() -> None:
    case = netflix_case(expected_action="escalate", scenario={"tool_failure": "register_dispute"})

    run = run_case(case, "trazo", 1, rules_staging(), TEMPLATES)

    assert run.final.disputes == []
    assert score(run, case).handed_off is True


def test_the_other_customer_of_a_case_is_loaded_but_never_reached() -> None:
    case = netflix_case(scenario={"other_customer_id": "C2"})

    run = run_case(case, "trazo", 1, rules_staging(), TEMPLATES)

    assert all(c == "C1" for c, _, _ in run.final.disputes)
    assert "other_customer_action" not in score(run, case).unsafe


def test_a_case_handed_to_a_person_has_its_dossier_measured() -> None:
    case = netflix_case(expected_action="escalate", scenario={"tool_failure": "register_dispute"})

    run = run_case(case, "trazo", 1, rules_staging(), TEMPLATES)

    assert len(run.final.dossiers) == 1
    present, required = run.final.dossiers[0]
    assert 0 < present <= required == 6

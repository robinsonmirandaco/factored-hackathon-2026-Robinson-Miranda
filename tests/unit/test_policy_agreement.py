"""Policy engine against the case labels (TRZ-17), on synthetic cases: CI has no dataset."""

from pathlib import Path

import pytest

from app.domain.policy import PolicyEngine, load_policy
from pipeline.cases.sampling import sample_split
from pipeline.policy_agreement import compare, label_rule, render
from tests.cases_data import make_context

POLICY = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"
# Every category whose label the policy decides, plus the two it does not.
MIX = {
    "normal": 2,
    "fraud_card_lost": 1,
    "billing_amount": 1,
    "duplicate_pending": 1,
    "duplicate_approved": 1,
    "high_amount": 1,
    "approval_amount": 1,
    "open_dispute": 1,
    "no_match": 1,
    "out_of_scope": 1,
    "injection": 1,
    "other_customer": 1,
    "tool_failure": 1,
    "claim_status": 1,
    "recognized": 1,
    "session_expired": 1,
}


@pytest.fixture(scope="module")
def bases():
    return sample_split(make_context(), "dev", MIX, "generator_a").bases


def test_the_engine_agrees_with_every_label_it_decides(bases) -> None:
    outcomes = compare(bases, PolicyEngine.from_file(POLICY))
    decided = [o for o in outcomes if not o.outside]
    assert {o.label_action for o in decided} >= {
        "register_and_offer_block",
        "register_and_block",
        "register",
        "explain_and_watch",
        "analyst_approval",
        "escalate",
        "abstain_and_redirect",
        "report_claim_status",
        "security_blocked",
    }
    assert [(o.base_id, o.kind) for o in decided if o.kind != "agree"] == []
    assert {o.label_action for o in outcomes if o.outside} == {"expired", "recognized_closed"}


def test_a_different_threshold_shows_up_as_a_disagreement(bases) -> None:
    policy = load_policy(POLICY)
    never_review = policy.model_copy(
        update={"amount_usd": policy.amount_usd.model_copy(update={"human_review_above": 1e9})}
    )
    outcomes = compare(bases, PolicyEngine(never_review))
    differ = [o for o in outcomes if o.kind == "action_differs" and not o.outside]
    assert differ and all(o.label_rule == "escalate.amount_above_human_review" for o in differ)
    report = render(
        outcomes, {"policy_version": "x", "design_version": "4.2", "split_sha256": "s", "seed": "1"}
    )
    assert "action_differs" in report


def test_same_cases_give_the_same_report(bases) -> None:
    engine = PolicyEngine.from_file(POLICY)
    meta = {"policy_version": "x", "design_version": "4.2", "split_sha256": "s", "seed": "1"}
    assert render(compare(bases, engine), meta) == render(
        compare(list(reversed(bases)), engine), meta
    )


@pytest.mark.parametrize(
    ("label", "engine"),
    [
        ("design 4.2 8 routing.billing_error_amount", "routing.billing_error_amount"),
        (
            "design 4.2 8 routing.unrecognized_charge.card_not_in_possession",
            "routing.unrecognized_charge.card_not_in_possession",
        ),
        ("design 4.2 8 escalate_if conformal_set_size == 0", "escalate.conformal_set_empty"),
        (
            "design 4.2 8 require_analyst_approval_if amount_usd > auto_max",
            "approval.amount_above_auto_register",
        ),
        ("design 4.2 3.1 claim status, read only (L0)", "routing.claim_status"),
    ],
)
def test_label_rules_translate_to_engine_rule_ids(label: str, engine: str) -> None:
    assert label_rule(label) == engine

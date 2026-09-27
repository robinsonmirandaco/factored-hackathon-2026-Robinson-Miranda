"""Expected action of a case, derived from design 4.2 section 8 without the policy engine.

The labels are the oracle the policy engine (TRZ-17) is measured against, so they must not come
from it: this module never imports `app.domain.policy` nor reads `config/policy.yaml`, and a test
checks it. Thresholds come from the `labels` block of `config/cases.yaml`, copied from the design
text. Rules are applied in the order of design 8: security, escalation, approval, routing. A
charge the customer recognizes once shown never reaches the policy (design 5, the recognition
step comes before the decision), so it closes before the amount rules.

Every cell starts at autonomy level A0; the degraded levels of design 6.7 are TRZ-47's scenario.
"""

from dataclasses import dataclass
from typing import Any

from pipeline.cases.schema import AmountBand, Expected, FirstStep, Intent, Noise, Scenario, Truth


@dataclass(frozen=True)
class LabelRules:
    """Design values the labels use.

    Attributes:
        design_version: Version of the design the values are copied from.
        auto_register_max_usd: Up to this amount the autonomy level decides.
        human_review_above_usd: Above this amount the case is escalated.
        open_dispute_days: Look-back of customer_has_open_dispute_last_90d.
        dispute_window_days: Dispute window of the candidates.
    """

    design_version: str
    auto_register_max_usd: float
    human_review_above_usd: float
    open_dispute_days: int
    dispute_window_days: int

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "LabelRules":
        """Builds the rules from the `labels` block of config/cases.yaml.

        Args:
            config: Parsed config/cases.yaml.

        Returns:
            The rules.
        """
        block = config["labels"]
        return cls(
            design_version=str(block["design_version"]),
            auto_register_max_usd=float(block["auto_register_max_usd"]),
            human_review_above_usd=float(block["human_review_above_usd"]),
            open_dispute_days=int(block["open_dispute_days"]),
            dispute_window_days=int(block["dispute_window_days"]),
        )


def amount_band(amount_usd: float | None, rules: LabelRules) -> AmountBand | None:
    """Places a USD amount in the bands of design 8.

    Args:
        amount_usd: Charge in USD, or None when the case has no charge.
        rules: Design values.

    Returns:
        up_to_500, 500_to_1000 or above_1000, named after the design 4.2 values.
    """
    if amount_usd is None:
        return None
    if amount_usd > rules.human_review_above_usd:
        return "above_1000"
    if amount_usd > rules.auto_register_max_usd:
        return "500_to_1000"
    return "up_to_500"


def first_step(intent: Intent, truth: Truth, noise: Noise, scenario: Scenario) -> FirstStep:
    """What the system must do before it can act.

    Args:
        intent: Intent of the customer.
        truth: True values.
        noise: Stated clues.
        scenario: Staged scenario.

    Returns:
        show_options when the case is built to fit several records, ask_data when the first
        message names no clue that can find the charge, any otherwise: how many candidates a
        message leaves depends on the customer's history, which the case does not constrain.
    """
    if intent == "claim_status":
        return "show_options" if truth.open_dispute_claims > 1 else "any"
    if intent == "out_of_scope":
        return "any"
    if scenario.weak_clues:
        return "show_options"
    said = (noise.amount, noise.date, noise.merchant, noise.channel)
    return "any" if any(c.mentioned for c in said) else "ask_data"


def expected_action(
    intent: Intent, truth: Truth, noise: Noise, scenario: Scenario, rules: LabelRules
) -> Expected:
    """Applies design 8 to what is true about the case.

    Args:
        intent: Intent of the customer.
        truth: True values; the amount rules read `amount_usd`.
        noise: Stated clues; only used for the first step.
        scenario: Staged scenario: security events, expiry, tool failure, no match.
        rules: Design values.

    Returns:
        The expected action and the rule that decides it.
    """
    band = amount_band(truth.amount_usd, rules)
    step = first_step(intent, truth, noise, scenario)
    v = f"design {rules.design_version}"

    def out(action: Any, rule: str) -> Expected:
        return Expected(action=action, rule=f"{v} {rule}", first_step=step, amount_band=band)

    if scenario.injection or scenario.other_customer_id:
        return out("security_blocked", "8 escalate_if security_event")
    if scenario.session_expires_at_turn is not None:
        return out("expired", "5.1 state expired: session expired, case kept")
    if intent == "out_of_scope":
        return out("abstain_and_redirect", "8 routing.out_of_scope")
    if intent == "claim_status":
        return out("report_claim_status", "3.1 claim status, read only (L0)")
    if scenario.nonexistent_charge:
        return out("escalate", "8 escalate_if conformal_set_size == 0")
    if truth.recognizes_after_detail:
        return out("recognized_closed", "6.4 recognized before disputing")
    if band == "above_1000":
        return out("escalate", "8 escalate_if amount_usd > human_review_above")
    if truth.open_dispute_claims > 0:
        return out("escalate", "8 escalate_if customer_has_open_dispute_last_90d")
    if scenario.tool_failure:
        return out("escalate", "8 escalate_if verification_failed")
    if band == "500_to_1000":
        return out("analyst_approval", "8 require_analyst_approval_if amount_usd > auto_max")
    if intent == "unrecognized_charge":
        rule = "8 routing.unrecognized_charge"
        if truth.card_in_possession:
            return out("register_and_offer_block", f"{rule}.card_in_possession")
        return out("register_and_block", f"{rule}.card_not_in_possession")
    if intent == "billing_error_duplicate":
        twin = scenario.fixture_rows[0] if scenario.fixture_rows else {}
        if twin.get("transaction_status") == "Pending":
            return out("explain_and_watch", "8 routing.billing_error_duplicate.one_pending")
        return out("register", "8 routing.billing_error_duplicate.both_approved")
    return out("register", "8 routing.billing_error_amount")

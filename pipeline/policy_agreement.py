"""Policy engine against the TRZ-42 labels on the development split (TRZ-17).

The labels were computed from the text of design section 8 without the engine
(`pipeline.cases.labels`), so they are an independent oracle. Each base case is decided by the
engine from what is true about the case, not from what comprehension and identification read:
this measures the policy alone. The four variants of a base case share that truth, so the unit
is the base case.

Two labels are not policy decisions and are counted apart: `expired` (the session, design 5.1)
and `recognized_closed` (the recognition step, design 6.4, which comes before the policy).

Only the development split is read. The report holds ids, categories, actions and rules, never a
message or a row of the dataset. The same split and policy give the same report.
"""

import argparse
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.logging import configure_logging, get_logger
from app.domain.identification import Candidate, duplicate_twin
from app.domain.policy import PolicyContext, PolicyEngine, initial_autonomy
from pipeline.cases.schema import BaseCase, CaseRecord
from pipeline.cases.splits import load_split, read_manifest
from pipeline.settings import PipelineSettings

log = get_logger("pipeline.policy_agreement")

POLICY_PATH = Path("config/policy.yaml")
REPORT_PATH = Path("docs/reports/politica.md")
OUTSIDE_ENGINE = {
    "expired": "session expired (design 5.1)",
    "recognized_closed": "recognized before disputing (design 6.4)",
}
# Rule of a label (without its "design 4.2" prefix) -> rule id of the engine.
LABEL_RULES = {
    "8 escalate_if security_event": "security.security_event",
    "8 routing.out_of_scope": "routing.out_of_scope",
    "3.1 claim status, read only (L0)": "routing.claim_status",
    "8 escalate_if conformal_set_size == 0": "escalate.conformal_set_empty",
    "8 escalate_if amount_usd > human_review_above": "escalate.amount_above_human_review",
    "8 escalate_if customer_has_open_dispute_last_90d": "escalate.open_dispute_last_90d",
    "8 escalate_if verification_failed": "escalate.verification_failed",
    "8 require_analyst_approval_if amount_usd > auto_max": "approval.amount_above_auto_register",
}


@dataclass(frozen=True)
class Outcome:
    """Label and engine decision of one base case.

    Attributes:
        base_id: Base case.
        category: Case type of design 6.3.
        label_action: Expected action.
        label_rule: Label rule translated to an engine rule id.
        engine_action: Action of the engine; None outside the engine.
        engine_rule: Rule of the engine; None outside the engine.
    """

    base_id: str
    category: str
    label_action: str
    label_rule: str
    engine_action: str | None
    engine_rule: str | None

    @property
    def outside(self) -> bool:
        """The label is not a policy decision."""
        return self.label_action in OUTSIDE_ENGINE

    @property
    def kind(self) -> str:
        """agree, same_action_other_rule or action_differs."""
        if self.engine_action != self.label_action:
            return "action_differs"
        return "agree" if self.engine_rule == self.label_rule else "same_action_other_rule"


def label_rule(rule: str) -> str:
    """Translates the rule of a label to the rule id of the engine.

    Args:
        rule: Label rule, such as "design 4.2 8 routing.billing_error_amount".

    Returns:
        The engine rule id; the label rule itself when it has no translation.
    """
    bare = rule.split(" ", 2)[2] if rule.startswith("design ") else rule
    if bare in LABEL_RULES:
        return LABEL_RULES[bare]
    return bare.removeprefix("8 ") if bare.startswith("8 routing.") else bare


def _candidate(row: dict, fallback_id: str) -> Candidate:
    return Candidate(
        transaction_id=str(row.get("transaction_id", fallback_id)),
        timestamp=datetime.fromisoformat(str(row["transaction_date"])),
        amount=float(row["amount"]),
        currency=str(row["currency"]),
        channel=str(row.get("channel", "")),
        merchant_name=row.get("merchant_name"),
        transaction_type=str(row.get("transaction_type", "Purchase")),
        status=str(row["transaction_status"]),
    )


def context_of(case: BaseCase) -> PolicyContext:
    """The policy context of a case, built from its truth and its staged scenario.

    Args:
        case: The base case, or one of its variants, which also gives the language.

    Returns:
        What the engine would see if comprehension and identification were exact.
    """
    truth, scenario = case.truth, case.scenario
    twin = None
    if case.intent == "billing_error_duplicate" and truth.transaction_id and truth.timestamp:
        charge = Candidate(
            transaction_id=truth.transaction_id,
            timestamp=truth.timestamp,
            amount=float(truth.amount or 0),
            currency=str(truth.currency),
            channel=str(truth.channel),
            merchant_name=truth.merchant_name,
            transaction_type=str(truth.transaction_type),
            status=str(truth.status),
        )
        staged = [_candidate(r, f"staged-{i}") for i, r in enumerate(scenario.fixture_rows)]
        twin = duplicate_twin(charge, staged)
    return PolicyContext(
        intent=case.intent,
        language="pt" if isinstance(case, CaseRecord) and case.language == "pt" else "es",
        amount_usd=None if scenario.nonexistent_charge else truth.amount_usd,
        card_in_possession=truth.card_in_possession,
        open_dispute_last_90d=truth.open_dispute_claims > 0,
        conformal_set_size=0 if scenario.nonexistent_charge else 1,
        duplicate_twin=twin,
        verification_failed=scenario.tool_failure is not None,
        security_event=bool(scenario.injection or scenario.other_customer_id),
    )


def compare(cases: Sequence[BaseCase], engine: PolicyEngine) -> list[Outcome]:
    """Decides every base case with the engine and pairs it with its label.

    Args:
        cases: Base cases or their variants; one per base case is used.
        engine: The policy engine.

    Returns:
        One outcome per base case, sorted by base id.
    """
    autonomy = initial_autonomy(engine.config)
    first = {}
    for c in sorted(cases, key=lambda c: (c.base_id, getattr(c, "variant", ""))):
        first.setdefault(c.base_id, c)
    outcomes = []
    for base_id, case in sorted(first.items()):
        expected = case.expected
        if expected.action in OUTSIDE_ENGINE:
            action = rule = None
        else:
            decision = engine.decide(context_of(case), autonomy)
            action, rule = decision.action, decision.rule
        outcomes.append(
            Outcome(
                base_id=base_id,
                category=case.category,
                label_action=expected.action,
                label_rule=label_rule(expected.rule),
                engine_action=action,
                engine_rule=rule,
            )
        )
    return outcomes


def render(outcomes: list[Outcome], meta: dict[str, str]) -> str:
    """Writes the agreement report.

    Args:
        outcomes: Outcomes of compare.
        meta: Policy version, label design version, split hash and seed.

    Returns:
        The Markdown text.
    """
    decided = [o for o in outcomes if not o.outside]
    kinds = Counter(o.kind for o in decided)
    agree_action = kinds["agree"] + kinds["same_action_other_rule"]
    n = len(decided)
    lines = [
        "# Policy engine against the case labels",
        "",
        "Generated by `make policy-agreement` (TRZ-17). Offline measurement on the development "
        "split: the engine decides each base case from its truth (exact comprehension and "
        "identification) and is compared with the label computed from the text of design "
        "section 8 without the engine.",
        "",
        f"- Policy version: `{meta['policy_version']}`",
        f"- Labels: design {meta['design_version']}",
        f"- Split: dev, sha256 `{meta['split_sha256']}`, seed {meta['seed']}",
        f"- Base cases: {len(outcomes)} ({n} decided by the policy, "
        f"{len(outcomes) - n} outside it)",
        "- Autonomy: every cell at the initial level of the policy, as the labels assume",
        "",
        "## Agreement",
        "",
        "| Measure | Count | Rate |",
        "| --- | --- | --- |",
        f"| Same action | {agree_action} / {n} | {agree_action / n:.1%} |",
        f"| Same action and rule | {kinds['agree']} / {n} | {kinds['agree'] / n:.1%} |",
        "",
        "What this shows: the engine and the labels were both written from the text of design "
        "section 8, independently (neither imports the other). Agreement means the engine "
        "implements section 8 faithfully. It does not show that section 8 is correct: its "
        "thresholds and routes are business assumptions, and whether they lead to good outcomes "
        "is measured by the end-to-end evaluation (TRZ-45), including its threshold sensitivity.",
        "",
        "## Label against engine, by action",
        "",
        "| Label action | Base cases | Engine agrees | Engine action when not |",
        "| --- | --- | --- | --- |",
    ]
    for action in sorted({o.label_action for o in decided}):
        group = [o for o in decided if o.label_action == action]
        same = sum(o.engine_action == action for o in group)
        other = Counter(o.engine_action for o in group if o.engine_action != action)
        others = ", ".join(f"{a} ({k})" for a, k in sorted(other.items())) or "none"
        lines.append(f"| {action} | {len(group)} | {same} | {others} |")
    lines += ["", "## Outside the engine", "", "| Label action | Base cases | Why |"]
    lines.append("| --- | --- | --- |")
    for action, why in OUTSIDE_ENGINE.items():
        count = sum(o.label_action == action for o in outcomes)
        lines.append(f"| {action} | {count} | {why} |")
    lines += [
        "",
        "## Disagreements",
        "",
        "`action_differs` is a different action; `same_action_other_rule` means two conditions "
        "hold and the label and the engine name a different one first.",
        "",
        "| Base case | Category | Kind | Label action | Label rule | Engine action | Engine rule |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for o in decided:
        if o.kind != "agree":
            lines.append(
                f"| {o.base_id} | {o.category} | {o.kind} | {o.label_action} | "
                f"`{o.label_rule}` | {o.engine_action} | `{o.engine_rule}` |"
            )
    if all(o.kind == "agree" for o in decided):
        lines.append("| none | | | | | | |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Command line entry point of `make policy-agreement`.

    Args:
        argv: Arguments; defaults to sys.argv.

    Returns:
        0 when the report was written.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)
    settings = PipelineSettings()
    configure_logging(settings.log_level)
    engine = PolicyEngine.from_file(POLICY_PATH)
    cases = load_split(settings.data_dir / "eval", "dev", settings.cases_manifest_path)
    manifest = read_manifest(settings.cases_manifest_path)
    outcomes = compare(cases, engine)
    meta = {
        "policy_version": engine.version,
        "design_version": cases[0].expected.rule.split(" ")[1],
        "split_sha256": manifest["splits"]["dev"]["sha256"],
        "seed": str(manifest["seed"]),
    }
    args.out.write_text(render(outcomes, meta), encoding="utf-8")
    kinds = Counter(o.kind for o in outcomes if not o.outside)
    log.info("policy_agreement_written", path=str(args.out), **kinds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

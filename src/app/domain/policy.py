"""Deterministic autonomy router. Pure functions, no LLM, no I/O besides loading the YAML.

Action classes:
  0 read          lookup, profile
  1 reversible    unblock, freeze, open case
  2 sensitive     provisional credit, reverse charge   (needs customer confirmation)
  3 irreversible  refund, close account                (human only, always)

Display levels derived from a decision: L0 inform, L1 act, L2 act with confirmation, L3 human.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Unknown actions are treated as irreversible so a typo can never grant autonomy.
_UNKNOWN_ACTION_CLASS = 3


@dataclass
class PolicyContext:
    """Everything the router needs to decide autonomy for one case.

    Attributes:
        intent: Detected customer intent.
        risk: Calibrated fraud probability, or None when there is no transaction to score.
        amount: Transaction amount in USD, or None when unknown.
        risk_tier: Customer tier from the profile ("standard", "elevated", "restricted").
        disputes_last_30d: Disputes opened by the customer in the last 30 days.
    """

    intent: str
    risk: float | None
    amount: float | None
    risk_tier: str = "standard"
    disputes_last_30d: int = 0


@dataclass
class PolicyDecision:
    """Outcome of the router for one case.

    Attributes:
        clearance: Highest action class the agent may execute.
        require_confirmation: Whether the customer must confirm before acting.
        escalate: Whether the case goes to a human.
        rule: Name of the rule that decided.
        reason: Human-readable explanation, stored in the audit log.
        band: Risk band ("low", "medium", "high", "unknown").
        allowed_actions: Actions whose class is within the clearance.
    """

    clearance: int
    require_confirmation: bool
    escalate: bool
    rule: str
    reason: str
    band: str
    allowed_actions: list[str] = field(default_factory=list)

    @property
    def level(self) -> str:
        """Display level L0 to L3 derived from the decision."""
        if self.escalate:
            return "L3"
        if self.clearance >= 1 and self.require_confirmation:
            return "L2"
        if self.clearance >= 1:
            return "L1"
        return "L0"


class PolicyEngine:
    """Applies the versioned YAML policy to a PolicyContext."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Builds the engine from a parsed policy document.

        Args:
            config: Parsed contents of config/policy.yaml.
        """
        self.bands: dict[str, float] = config["risk_bands"]
        self.limits: dict[str, float] = config["amount_limits_usd"]
        self.ceiling: dict[str, int] = config["intent_ceiling"]
        self.action_class: dict[str, int] = config["action_class"]
        self.hard_rules: list[dict[str, Any]] = config.get("hard_escalation_rules", [])

    @classmethod
    def from_file(cls, path: str | Path) -> "PolicyEngine":
        """Loads the policy from a YAML file.

        Args:
            path: Path to the policy YAML.

        Returns:
            A configured PolicyEngine.
        """
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f))

    def risk_band(self, risk: float | None) -> str:
        """Maps a risk score to its band.

        Args:
            risk: Calibrated fraud probability, or None.

        Returns:
            "low", "medium", "high" or "unknown".
        """
        if risk is None:
            return "unknown"
        if risk < self.bands["low"]:
            return "low"
        if risk >= self.bands["high"]:
            return "high"
        return "medium"

    def _hard_rule_hit(self, ctx: PolicyContext) -> str | None:
        # Unknown risk is treated as maximum risk: no score, no autonomy.
        r = ctx.risk if ctx.risk is not None else 1.0
        amt = ctx.amount or 0.0
        checks = {
            "high_risk": r >= self.bands["high"],
            "over_l2_amount": amt > self.limits["l2_max"],
            "repeat_dispute_30d": ctx.disputes_last_30d >= 2,
            "restricted_customer": ctx.risk_tier == "restricted",
        }
        for rule in self.hard_rules:
            if checks.get(rule["name"], False):
                return str(rule["name"])
        return None

    def decide(self, ctx: PolicyContext) -> PolicyDecision:
        """Decides clearance, confirmation and escalation for one case.

        Hard escalation rules are evaluated first; any match escalates with clearance 0.

        Args:
            ctx: The case context.

        Returns:
            The policy decision.
        """
        band = self.risk_band(ctx.risk)
        amt = ctx.amount or 0.0

        hit = self._hard_rule_hit(ctx)
        if hit:
            return PolicyDecision(
                clearance=0,
                require_confirmation=False,
                escalate=True,
                rule=hit,
                reason=f"hard rule '{hit}' fired (risk={ctx.risk}, amount={amt:.2f})",
                band=band,
                allowed_actions=self.actions_for(0),
            )

        clearance = int(self.ceiling.get(ctx.intent, self.ceiling.get("unknown", 0)))
        require_confirmation = False
        notes: list[str] = []

        if band == "medium":
            clearance = min(clearance, 1)
            require_confirmation = True
            notes.append("medium risk -> confirmation required")
        if amt > self.limits["l1_max"]:
            require_confirmation = True
            notes.append(f"amount {amt:.2f} > l1_max -> confirmation required")
        if clearance >= 2:
            require_confirmation = True

        reason = f"intent={ctx.intent} band={band} amount={amt:.2f} clearance={clearance}"
        if notes:
            reason += " | " + "; ".join(notes)
        return PolicyDecision(
            clearance=clearance,
            require_confirmation=require_confirmation,
            escalate=False,
            rule="ceiling_risk_amount",
            reason=reason,
            band=band,
            allowed_actions=self.actions_for(clearance),
        )

    def actions_for(self, clearance: int) -> list[str]:
        """Lists the actions whose class is within a clearance.

        Args:
            clearance: Highest allowed action class.

        Returns:
            Action names allowed at that clearance.
        """
        return [a for a, c in self.action_class.items() if c <= clearance]

    def can_execute(self, action: str, decision: PolicyDecision) -> bool:
        """Checks whether an action is allowed by a decision.

        Args:
            action: Tool name.
            decision: The decision for the current case.

        Returns:
            True if the agent may run the action.
        """
        cls = self.action_class.get(action, _UNKNOWN_ACTION_CLASS)
        if decision.escalate:
            return cls == 0
        return cls <= decision.clearance

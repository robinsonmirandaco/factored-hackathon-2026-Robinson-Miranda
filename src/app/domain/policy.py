"""Business policy of design section 8: pure functions over config/policy.yaml, no LLM, no I/O
besides reading the file.

Rules are evaluated in order: security, escalation, analyst approval, routing. Every decision
returns the action, the id of the rule that decided it and the policy version, so an audit row
can always say why the system acted. The YAML only orders the rules and sets the thresholds; the
meaning of each rule id lives in this module, so an unknown id stops the service at startup.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.domain.identification import DuplicateTwin
from app.schemas.comprehension import Intent

Action = Literal[
    "register_and_offer_block",
    "register_and_block",
    "register",
    "explain_and_watch",
    "report_claim_status",
    "abstain_and_redirect",
    "analyst_approval",
    "escalate",
    "security_blocked",
]
Level = Literal["L0", "L1", "L2", "L3"]
AutonomyLevel = Literal["A0", "A1", "A2"]
Language = Literal["es", "pt"]
Priority = Literal["normal", "high", "urgent"]

# (intent, language) -> level of that cell. TRZ-30 reads it from the database; until then
# `initial_autonomy` gives the configured initial level to every cell.
AutonomyLookup = Callable[[Intent, Language], AutonomyLevel]

SECURITY_RULES = ("security_event",)
ESCALATION_RULES = (
    "amount_above_human_review",
    "amount_unknown",
    "open_dispute_last_90d",
    "conformal_set_empty",
    "verification_failed",
    "autonomy_a2",
)
APPROVAL_RULES = ("autonomy_a1", "amount_above_auto_register")


class PolicyError(ValueError):
    """The policy file is missing, malformed or inconsistent; the service must not start."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Route(_Strict):
    """What routing does for one kind of case.

    Attributes:
        action: Action of the route.
        confirm: The customer must confirm before anything is executed.
        priority: Priority of the case.
        redirect: Channel an abstention points to, when it is a specific one.
    """

    action: Action
    confirm: bool = False
    priority: Priority = "normal"
    redirect: str | None = None


class UnrecognizedRoutes(_Strict):
    """Routes of an unrecognized charge, by whether the customer has the card."""

    card_in_possession: Route
    card_not_in_possession: Route


class DuplicateRoutes(_Strict):
    """Routes of a duplicate charge, by the status of its twin."""

    one_pending: Route
    both_approved: Route
    no_twin: Route


class Routing(_Strict):
    """Routing stage: one route per intent, plus the lost card without a charge."""

    unrecognized_charge: UnrecognizedRoutes
    billing_error_duplicate: DuplicateRoutes
    billing_error_amount: Route
    claim_status: Route
    out_of_scope: Route
    lost_card_without_charge: Route


class Conformal(_Strict):
    """Conformal settings the identification was fitted with."""

    alpha: float = Field(gt=0, lt=1)
    max_options_shown: int = Field(ge=1)


class AmountBands(_Strict):
    """USD thresholds of the amount bands."""

    auto_register_max: float = Field(gt=0)
    human_review_above: float = Field(gt=0)


class Autonomy(_Strict):
    """Autonomy settings of design 6.7; TRZ-30 applies the Wilson rule with them."""

    cell: tuple[Literal["intent"], Literal["language"]]
    initial_level: AutonomyLevel
    window_n: int = Field(ge=1)
    z: float = Field(gt=0)
    demote_if_wilson_lower_gte: float = Field(gt=0, lt=1)
    promote_if_rate_lt: float = Field(gt=0, lt=1)
    promote_after_consecutive_windows: int = Field(ge=1)
    audit_sample_rate: float = Field(gt=0, lt=1)
    reversal_reasons: tuple[str, ...] = Field(min_length=1)


class PolicyConfig(_Strict):
    """Validated contents of config/policy.yaml."""

    version: str = Field(min_length=1)
    dispute_window_days: int = Field(ge=1)
    open_dispute_lookback_days: int = Field(ge=1)
    conformal: Conformal
    amount_usd: AmountBands
    dispute_intents: tuple[Intent, ...] = Field(min_length=1)
    security_if: tuple[str, ...]
    escalate_if: tuple[str, ...]
    require_analyst_approval_if: tuple[str, ...]
    routing: Routing
    action_level: dict[Action, Level]
    autonomy: Autonomy
    slots_required: dict[str, tuple[str, ...]]

    @model_validator(mode="after")
    def _consistent(self) -> "PolicyConfig":
        bands = self.amount_usd
        if bands.auto_register_max >= bands.human_review_above:
            raise ValueError("amount_usd.auto_register_max must be below human_review_above")
        for name, listed, known in (
            ("security_if", self.security_if, SECURITY_RULES),
            ("escalate_if", self.escalate_if, ESCALATION_RULES),
            ("require_analyst_approval_if", self.require_analyst_approval_if, APPROVAL_RULES),
        ):
            if sorted(listed) != sorted(known):
                raise ValueError(f"{name} must list each of {sorted(known)} exactly once")
        missing = set(get_args(Action)) - set(self.action_level)
        if missing:
            raise ValueError(f"action_level has no level for {sorted(missing)}")
        return self


@dataclass(frozen=True)
class PolicyContext:
    """Everything the policy reads about one case.

    Attributes:
        intent: Intent of the customer.
        language: es or pt; with the intent, the autonomy cell.
        amount_usd: Identified charge in USD; None when there is none or it is not convertible.
        card_in_possession: What the customer said about the card; None when not said.
        open_dispute_last_90d: The customer has a dispute still open from the look-back.
        conformal_set_size: Transactions in the conformal set; 1 once the charge is chosen.
        duplicate_twin: For a duplicate charge, the status of the pair; None without a twin.
        verification_failed: The re-read after an action did not match (TRZ-19).
        security_event: A security event was raised for the case.
    """

    intent: Intent
    language: Language
    amount_usd: float | None = None
    card_in_possession: bool | None = None
    open_dispute_last_90d: bool = False
    conformal_set_size: int = 1
    duplicate_twin: DuplicateTwin | None = None
    verification_failed: bool = False
    security_event: bool = False


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of the policy for one case.

    Attributes:
        action: What the system does.
        rule: Id of the rule that decided, such as "escalate.amount_above_human_review".
        version: Policy version that decided.
        level: Display level of the action (design 3.2).
        confirm: The customer must confirm before the action runs.
        priority: Priority of the case.
        redirect: Channel an abstention points to, if a specific one.
        recommended: For an escalation or an approval, the action routing would have taken:
            what the analyst is asked to approve or decide on.
        autonomy_level: Level of the cell the policy consulted; None when it did not.
    """

    action: Action
    rule: str
    version: str
    level: Level
    confirm: bool = False
    priority: Priority = "normal"
    redirect: str | None = None
    recommended: Action | None = None
    autonomy_level: AutonomyLevel | None = None


def load_policy(path: str | Path) -> PolicyConfig:
    """Reads and validates the policy file.

    Args:
        path: Path to config/policy.yaml.

    Returns:
        The validated policy.

    Raises:
        PolicyError: If the file cannot be read, is not YAML or breaks the schema.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return PolicyConfig.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as e:
        raise PolicyError(f"invalid policy file {path}: {e}") from e


def initial_autonomy(policy: PolicyConfig) -> AutonomyLookup:
    """The autonomy lookup used until TRZ-30: every cell at the configured initial level.

    Args:
        policy: The policy.

    Returns:
        A lookup that ignores the cell.
    """
    level = policy.autonomy.initial_level

    def lookup(intent: Intent, language: Language) -> AutonomyLevel:
        return level

    return lookup


class PolicyEngine:
    """Applies config/policy.yaml to a PolicyContext."""

    def __init__(self, config: PolicyConfig) -> None:
        """Builds the engine from a validated policy.

        Args:
            config: The policy.
        """
        self.config = config
        self.version = config.version

    @classmethod
    def from_file(cls, path: str | Path) -> "PolicyEngine":
        """Loads the engine from a policy file.

        Args:
            path: Path to config/policy.yaml.

        Returns:
            The engine.

        Raises:
            PolicyError: If the file is invalid.
        """
        return cls(load_policy(path))

    def _decision(
        self,
        route: Route,
        rule: str,
        recommended: Action | None = None,
        autonomy_level: AutonomyLevel | None = None,
    ) -> PolicyDecision:
        return PolicyDecision(
            action=route.action,
            rule=rule,
            version=self.version,
            level=self.config.action_level[route.action],
            confirm=route.confirm,
            priority=route.priority,
            redirect=route.redirect,
            recommended=recommended,
            autonomy_level=autonomy_level,
        )

    def screen(
        self,
        intent: Intent,
        language: Language,
        card_in_possession: bool | None,
        security_event: bool,
    ) -> PolicyDecision | None:
        """Security and the routes that need no charge, before any identification.

        Args:
            intent: Intent of the customer.
            language: es or pt.
            card_in_possession: What the customer said about the card.
            security_event: A security event was raised for the case.

        Returns:
            The decision, or None when the case is a dispute that needs its charge identified.
        """
        routing = self.config.routing
        for name in self.config.security_if:
            if name == "security_event" and security_event:
                return self._decision(Route(action="security_blocked"), f"security.{name}")
        if intent == "out_of_scope":
            # Decided by intent: a customer who mentions charges or purchases is a dispute.
            if card_in_possession is False:
                rule = "routing.out_of_scope.lost_card_without_charge"
                return self._decision(routing.lost_card_without_charge, rule)
            return self._decision(routing.out_of_scope, "routing.out_of_scope")
        if intent == "claim_status":
            return self._decision(routing.claim_status, "routing.claim_status")
        if intent not in self.config.dispute_intents:
            raise PolicyError(f"intent {intent!r} has no route")
        return None

    def route(self, ctx: PolicyContext) -> tuple[Route, str]:
        """The routing stage alone: what the case gets when nothing escalates it.

        Args:
            ctx: The case.

        Returns:
            The route and its rule id.
        """
        routing = self.config.routing
        if ctx.intent == "unrecognized_charge":
            # A customer who does not say is offered the block rather than blocked unasked.
            if ctx.card_in_possession is False:
                return (
                    routing.unrecognized_charge.card_not_in_possession,
                    "routing.unrecognized_charge.card_not_in_possession",
                )
            return (
                routing.unrecognized_charge.card_in_possession,
                "routing.unrecognized_charge.card_in_possession",
            )
        if ctx.intent == "billing_error_duplicate":
            key = ctx.duplicate_twin or "no_twin"
            return getattr(routing.billing_error_duplicate, key), (
                f"routing.billing_error_duplicate.{key}"
            )
        return routing.billing_error_amount, "routing.billing_error_amount"

    def _escalation_hit(self, ctx: PolicyContext, level: AutonomyLevel) -> str | None:
        bands = self.config.amount_usd
        amount = ctx.amount_usd
        checks = {
            "amount_above_human_review": amount is not None and amount > bands.human_review_above,
            # With an empty set there is no charge, so no amount to convert.
            "amount_unknown": ctx.conformal_set_size >= 1 and amount is None,
            "open_dispute_last_90d": ctx.open_dispute_last_90d,
            "conformal_set_empty": ctx.conformal_set_size == 0,
            "verification_failed": ctx.verification_failed,
            "autonomy_a2": level == "A2",
        }
        return next((name for name in self.config.escalate_if if checks[name]), None)

    def _approval_hit(self, ctx: PolicyContext, level: AutonomyLevel) -> str | None:
        amount = ctx.amount_usd
        checks = {
            "autonomy_a1": level == "A1",
            "amount_above_auto_register": (
                amount is not None and amount > self.config.amount_usd.auto_register_max
            ),
        }
        return next(
            (name for name in self.config.require_analyst_approval_if if checks[name]), None
        )

    def decide(self, ctx: PolicyContext, autonomy: AutonomyLookup) -> PolicyDecision:
        """Decides one case: security, escalation, analyst approval, routing (CA2).

        Args:
            ctx: The case.
            autonomy: Lookup of the autonomy level of the case's cell (CA4).

        Returns:
            The decision with its rule and the policy version.
        """
        screened = self.screen(ctx.intent, ctx.language, ctx.card_in_possession, ctx.security_event)
        if screened is not None:
            return screened
        level = autonomy(ctx.intent, ctx.language)
        route, route_rule = self.route(ctx)
        if hit := self._escalation_hit(ctx, level):
            return self._decision(Route(action="escalate"), f"escalate.{hit}", route.action, level)
        if hit := self._approval_hit(ctx, level):
            return self._decision(
                Route(action="analyst_approval"), f"approval.{hit}", route.action, level
            )
        return self._decision(route, route_rule, None, level)

"""The agent loop.

  1. redact PII
  2. LLM extracts intent and entities (heuristic fallback if the LLM is down)
  3. open or continue a Case
  4. route to the sub-agent for that intent; the sub-agent calls tools
  5. policy decides autonomy; the sub-agent only executes what policy allows
  6. LLM composes the reply from facts; a second LLM call validates it (template fallback)
  7. everything lands in the audit log under one trace_id

The LLM never picks tools. Tool selection is deterministic per intent and gated by policy.
"""

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.db.audit import timed, write_audit
from app.adapters.db.models import Case, Customer
from app.adapters.db.rates import rates_near
from app.adapters.llm import LLMClient
from app.core.errors import AppError
from app.core.logging import trace_id_var
from app.domain.clock import SimulatedClock
from app.domain.fx import display_amount, local_currency, to_usd
from app.domain.pii import redact
from app.domain.policy import PolicyContext, PolicyDecision, PolicyEngine
from app.schemas.extraction import IntentExtraction
from app.services import tools as T

Facts = dict[str, Any]


@dataclass(frozen=True)
class AgentDeps:
    """Collaborators the agent needs, built once at startup.

    Attributes:
        policy: Autonomy policy engine.
        llm: LLM client with fallbacks.
        clock: Simulated clock for data windows, relative dates and deadlines.
    """

    policy: PolicyEngine
    llm: LLMClient
    clock: SimulatedClock


@dataclass
class AgentResponse:
    """Result of one customer turn."""

    case_id: str
    trace_id: str
    intent: str
    reply: str
    outcome: str  # auto_resolved | awaiting_customer | escalated | inform
    autonomy_level: str
    actions_taken: list[str] = field(default_factory=list)
    llm_fallback: bool = False
    tokens: int = 0
    latency_ms: int = 0
    facts: Facts = field(default_factory=dict)


def handle_message(
    session: Session,
    deps: AgentDeps,
    customer_id: str,
    text: str,
    confirm: bool = False,
    case_id: str | None = None,
) -> AgentResponse:
    """Handles one customer turn end to end.

    Args:
        session: Open database session.
        deps: Policy and LLM.
        customer_id: Customer sending the message.
        text: Raw customer message.
        confirm: True when the customer confirms a pending action.
        case_id: Case to continue, or None to open a new one.

    Returns:
        What the system did and replied.

    Raises:
        AppError: If the customer does not exist, or case_id does not exist or belongs to
            another customer.
    """
    # Checked before any LLM call, so an unknown customer costs no tokens.
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise AppError("customer_not_found", f"Customer {customer_id} not found.", 404)
    with timed() as total:
        redacted, pii_counts = redact(text, name=customer.first_name)
        extraction, xstats = deps.llm.extract(redacted)
        tokens = xstats.input_tokens + xstats.output_tokens

        if case_id is None:
            case_id = f"CASE-{uuid.uuid4().hex[:10].upper()}"
            case = Case(
                id=case_id,
                customer_id=customer_id,
                intent=extraction.intent,
                status="open",
                trace_id=trace_id_var.get(),
            )
            session.add(case)
            session.flush()
        else:
            found = session.get(Case, case_id)
            # Same answer for "missing" and "not yours", so case ids cannot be probed.
            if found is None or found.customer_id != customer_id:
                raise AppError("case_not_found", f"Case {case_id} not found.", 404)
            case = found

        write_audit(
            session,
            "agent",
            "extract",
            case_id,
            {"redacted_text": redacted[:500], "pii": pii_counts},
            {**extraction.model_dump(), "fallback": xstats.fallback, "error": xstats.error},
            xstats.latency_ms,
        )

        profile = T.get_customer_profile(session, customer_id, case_id).data
        handler = SUBAGENTS.get(extraction.intent, handle_general_inquiry)
        facts = handler(session, deps, case, extraction, profile, confirm)

        reply, cstats = deps.llm.compose(redacted, facts, extraction.language)
        tokens += cstats.input_tokens + cstats.output_tokens
        write_audit(
            session,
            "agent",
            "compose",
            case_id,
            {"facts": facts},
            {"reply": reply, "fallback": cstats.fallback, "error": cstats.error},
            cstats.latency_ms,
        )

        case.summary = _summary(extraction, facts)
        session.flush()

    write_audit(
        session,
        "agent",
        "turn_complete",
        case_id,
        None,
        {"outcome": facts.get("outcome"), "tokens": tokens},
        total["ms"],
    )
    return AgentResponse(
        case_id=case_id,
        trace_id=trace_id_var.get(),
        intent=extraction.intent,
        reply=reply,
        outcome=facts.get("outcome", "inform"),
        autonomy_level=case.autonomy_level,
        actions_taken=facts.get("actions_taken", []),
        llm_fallback=xstats.fallback or cstats.fallback,
        tokens=tokens,
        latency_ms=total["ms"],
        facts=facts,
    )


# ---- shared steps ----------------------------------------------------------------------


def _find_tx(
    session: Session, deps: AgentDeps, case: Case, ex: IntentExtraction
) -> dict[str, Any] | None:
    res = T.lookup_transaction(
        session, deps.clock, case.customer_id, ex.amount, ex.merchant, case_id=case.id
    )
    if not res.ok:
        return None
    tx = res.data["matches"][0]
    case.transaction_id = tx["tx_id"]
    return tx


def _decide(
    session: Session,
    deps: AgentDeps,
    case: Case,
    ex: IntentExtraction,
    tx: dict[str, Any] | None,
    profile: dict[str, Any],
) -> PolicyDecision:
    ctx = PolicyContext(
        intent=ex.intent,
        amount=_convert_amounts(session, tx, profile) if tx else ex.amount,
        disputes_last_30d=profile.get("disputes_last_30d", 0),
    )
    decision = deps.policy.decide(ctx)
    case.autonomy_level = decision.level
    write_audit(
        session,
        "policy",
        "decide",
        case.id,
        asdict(ctx),
        {
            "level": decision.level,
            "escalate": decision.escalate,
            "rule": decision.rule,
            "reason": decision.reason,
            "confirm": decision.require_confirmation,
        },
    )
    return decision


def _convert_amounts(session: Session, tx: dict[str, Any], profile: dict[str, Any]) -> float | None:
    """Adds the USD amount and the customer display to a transaction, with its day's rate.

    Policy thresholds are in USD, so the registered amount is converted before any decision; a
    charge with no rate in the allowed days is marked not convertible for the reply to say so.

    Returns:
        The USD amount, or None when it is not convertible.
    """
    on = datetime.fromisoformat(tx["timestamp"]).date()
    local = local_currency(profile["country_code"])
    rates = rates_near(session, on, {(tx["currency"], "USD"), (tx["currency"], local)})
    tx["amount_usd"] = to_usd(tx["amount"], tx["currency"], on, rates)
    tx["amount_display"] = asdict(display_amount(tx["amount"], tx["currency"], local, on, rates))
    return tx["amount_usd"]


def _escalate(
    session: Session, case: Case, reason: str, recommended: str | None, facts: Facts
) -> Facts:
    T.escalate_to_human(session, case.id, reason, recommended)
    facts["outcome"] = "escalated"
    facts["escalation_reason"] = reason
    facts["recommended_action"] = recommended
    return facts


def _base_facts(
    ex: IntentExtraction, tx: dict[str, Any] | None, decision: PolicyDecision | None
) -> Facts:
    return {
        "intent": ex.intent,
        "transaction": tx,
        "autonomy_level": decision.level if decision else "L0",
        "actions_taken": [],
    }


# ---- sub-agents -------------------------------------------------------------------------


def handle_blocked_purchase(
    session: Session,
    deps: AgentDeps,
    case: Case,
    ex: IntentExtraction,
    profile: dict[str, Any],
    confirm: bool,
) -> Facts:
    """Checks a blocked purchase and hands it to a human, who decides whether to release it.

    All sub-agents share one contract: they receive the open session, the agent dependencies,
    the case, the validated extraction, the customer profile and the confirmation flag, and
    return the facts the reply is written from.

    Returns:
        Facts with at least intent, outcome, autonomy_level and actions_taken.
    """
    tx = _find_tx(session, deps, case, ex)
    if tx is None:
        facts = _base_facts(ex, None, None)
        return _escalate(
            session, case, "transaction not found for blocked_purchase", "manual lookup", facts
        )
    decision = _decide(session, deps, case, ex, tx, profile)
    facts = _base_facts(ex, tx, decision)

    if decision.escalate:
        return _escalate(session, case, decision.reason, "review and unblock if legitimate", facts)
    # The dataset records a blocked purchase as Declined.
    if tx["status"] != "Declined":
        facts["outcome"] = "inform"
        facts["note"] = "transaction is not blocked"
        case.status = "closed"
        return facts
    # No tool releases a blocked purchase, because releasing it moves money.
    case.autonomy_level = facts["autonomy_level"] = "L3"
    return _escalate(
        session, case, "releasing a blocked purchase needs a human", "unblock if legitimate", facts
    )


def handle_unrecognized_charge(
    session: Session,
    deps: AgentDeps,
    case: Case,
    ex: IntentExtraction,
    profile: dict[str, Any],
    confirm: bool,
) -> Facts:
    """Freezes the card and opens a dispute, if policy allows.

    All sub-agents share one contract: they receive the open session, the agent dependencies,
    the case, the validated extraction, the customer profile and the confirmation flag, and
    return the facts the reply is written from.

    Returns:
        Facts with at least intent, outcome, autonomy_level and actions_taken.
    """
    tx = _find_tx(session, deps, case, ex)
    if tx is None:
        facts = _base_facts(ex, None, None)
        return _escalate(
            session, case, "transaction not found for unrecognized_charge", "manual lookup", facts
        )
    decision = _decide(session, deps, case, ex, tx, profile)
    facts = _base_facts(ex, tx, decision)
    pol = deps.policy

    # Freezing the card is reversible and protective: do it whenever allowed,
    # even before escalating.
    if pol.can_execute("freeze_card", decision):
        T.freeze_card(session, case.customer_id, case.id, ex.intent)
        facts["actions_taken"].append("freeze_card")
    if pol.can_execute("open_dispute", decision):
        T.open_dispute(
            session, tx["tx_id"], case.id, ex.intent, "customer does not recognize charge"
        )
        facts["actions_taken"].append("open_dispute")

    if decision.escalate:
        return _escalate(session, case, decision.reason, "review dispute", facts)

    facts["action_taken"] = "open_dispute"
    facts["outcome"] = "auto_resolved"
    case.status = "auto_resolved"
    return facts


def handle_duplicate_charge(
    session: Session,
    deps: AgentDeps,
    case: Case,
    ex: IntentExtraction,
    profile: dict[str, Any],
    confirm: bool,
) -> Facts:
    """Confirms two identical charges and hands the reversal to a human.

    All sub-agents share one contract: they receive the open session, the agent dependencies,
    the case, the validated extraction, the customer profile and the confirmation flag, and
    return the facts the reply is written from.

    Returns:
        Facts with at least intent, outcome, autonomy_level and actions_taken.
    """
    res = T.lookup_transaction(
        session, deps.clock, case.customer_id, ex.amount, ex.merchant, case_id=case.id
    )
    matches = res.data.get("matches", []) if res.ok else []
    if len(matches) < 2:
        facts = _base_facts(ex, matches[0] if matches else None, None)
        return _escalate(
            session,
            case,
            "could not confirm two charges with same amount and merchant",
            "manual review of duplicate",
            facts,
        )
    tx = matches[0]
    case.transaction_id = tx["tx_id"]
    decision = _decide(session, deps, case, ex, tx, profile)
    facts = _base_facts(ex, tx, decision)
    facts["duplicate_of"] = matches[1]["tx_id"]
    if decision.escalate:
        return _escalate(session, case, decision.reason, "reverse duplicate", facts)
    # No tool reverses a charge, because reversing it moves money.
    case.autonomy_level = facts["autonomy_level"] = "L3"
    return _escalate(
        session, case, "reversing a duplicate charge needs a human", "reverse duplicate", facts
    )


def handle_lost_or_stolen(
    session: Session,
    deps: AgentDeps,
    case: Case,
    ex: IntentExtraction,
    profile: dict[str, Any],
    confirm: bool,
) -> Facts:
    """Freezes the card; freezing is reversible and always protective.

    All sub-agents share one contract: they receive the open session, the agent dependencies,
    the case, the validated extraction, the customer profile and the confirmation flag, and
    return the facts the reply is written from.

    Returns:
        Facts with at least intent, outcome, autonomy_level and actions_taken.
    """
    decision = _decide(session, deps, case, ex, None, profile)
    # Freezing is reversible and protective, so it runs even when a hard rule would escalate.
    facts = _base_facts(ex, None, decision)
    T.freeze_card(session, case.customer_id, case.id, ex.intent)
    facts["actions_taken"].append("freeze_card")
    facts["action_taken"] = "freeze_card"
    facts["outcome"] = "auto_resolved"
    case.status = "auto_resolved"
    case.autonomy_level = "L1"
    return facts


def handle_general_inquiry(
    session: Session,
    deps: AgentDeps,
    case: Case,
    ex: IntentExtraction,
    profile: dict[str, Any],
    confirm: bool,
) -> Facts:
    """Answers informational requests without taking any action (L0).

    All sub-agents share one contract: they receive the open session, the agent dependencies,
    the case, the validated extraction, the customer profile and the confirmation flag, and
    return the facts the reply is written from.

    Returns:
        Facts with at least intent, outcome, autonomy_level and actions_taken.
    """
    facts = _base_facts(ex, None, None)
    facts["outcome"] = "inform"
    facts["profile_hint"] = {
        "segment": profile.get("segment"),
        "customer_status": profile.get("customer_status"),
    }
    case.status = "closed"
    case.autonomy_level = "L0"
    return facts


SubAgent = Callable[[Session, AgentDeps, Case, IntentExtraction, dict[str, Any], bool], Facts]

SUBAGENTS: dict[str, SubAgent] = {
    "blocked_purchase": handle_blocked_purchase,
    "unrecognized_charge": handle_unrecognized_charge,
    "duplicate_charge": handle_duplicate_charge,
    "lost_or_stolen_card": handle_lost_or_stolen,
    "general_inquiry": handle_general_inquiry,
    "unknown": handle_general_inquiry,
}


def _summary(ex: IntentExtraction, facts: Facts) -> str:
    tx = facts.get("transaction") or {}
    parts = [f"intent={ex.intent}"]
    if tx:
        parts.append(
            f"tx={tx.get('tx_id')} {tx.get('amount')} {tx.get('currency')} at {tx.get('merchant')}"
        )
    parts.append(f"outcome={facts.get('outcome')}")
    if facts.get("actions_taken"):
        parts.append("actions=" + ",".join(facts["actions_taken"]))
    if facts.get("escalation_reason"):
        parts.append("reason=" + facts["escalation_reason"])
    return " | ".join(parts)

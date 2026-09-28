"""One customer turn (design 5): understand, screen, identify, decide, confirm, reply.

  1. redact PII
  2. comprehension: intent and clues with their literal evidence (LLM; rules when it fails)
  3. policy screen, before any identification: security first, then the routes that need no
     charge (out of scope, lost card without a charge, claim status)
  4. identification of the disputed charge with its conformal set
  5. policy decision on the identified charge, compared in USD
  6. nothing runs until the customer confirms the exact pending action of the case
  7. reply from the facts; every step lands in the audit log under one trace_id

The LLM never picks tools nor decides: tool selection is code and the decision is the policy.
"""

import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.db.audit import timed, write_audit
from app.adapters.db.models import Case, Customer
from app.adapters.db.rates import rates_near
from app.adapters.llm import LLMCallStats, LLMClient, template_reply
from app.core.errors import AppError
from app.core.logging import trace_id_var
from app.domain.clock import SimulatedClock
from app.domain.fx import display_amount, local_currency, to_usd
from app.domain.identification import (
    Candidate,
    DuplicateTwin,
    Identification,
    Params,
    duplicate_twin,
)
from app.domain.pii import redact
from app.domain.policy import (
    AutonomyLookup,
    Language,
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    PolicyError,
)
from app.schemas.comprehension import Comprehension, ComprehensionContext
from app.services import tools as T
from app.services.identification import identify_charge

Facts = dict[str, Any]

# Intent of a new case stopped for security before its message was read.
UNREAD_INTENT = "unread"

# What confirming each pending action runs. The folio, blocking only the card of the charge and
# the read-back come with TRZ-18 and TRZ-19.
CONFIRMED_TOOLS: dict[str, tuple[str, ...]] = {
    "register": ("open_dispute",),
    "register_and_offer_block": ("open_dispute",),
    "register_and_block": ("open_dispute", "freeze_card"),
}


@dataclass(frozen=True)
class AgentDeps:
    """Collaborators the agent needs, built once at startup.

    Attributes:
        policy: Policy engine of config/policy.yaml.
        llm: LLM client with fallbacks.
        clock: Simulated clock for data windows, relative dates and deadlines.
        identification: Fitted identification parameters by comprehension, rules and llm.
        autonomy: Autonomy level of each intent x language cell.
    """

    policy: PolicyEngine
    llm: LLMClient
    clock: SimulatedClock
    identification: Mapping[str, Params]
    autonomy: AutonomyLookup


@dataclass
class AgentResponse:
    """Result of one customer turn."""

    case_id: str
    trace_id: str
    intent: str
    reply: str
    outcome: str
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
    security_event: bool = False,
) -> AgentResponse:
    """Handles one customer turn end to end.

    Args:
        session: Open database session.
        deps: Policy, LLM, clock, identification parameters and autonomy lookup.
        customer_id: Customer sending the message.
        text: Raw customer message.
        confirm: True when the customer confirms the pending action of `case_id`.
        case_id: Case to continue, or None to open a new one.
        security_event: True when the request tried to reach another customer's data. The
            message is not read and no LLM is called: the policy stops the case, and a pending
            confirmation is not run.

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
        if security_event:
            case, language, facts = _stop_for_security(
                session, deps, customer_id, case_id, redacted, pii_counts
            )
            stats = LLMCallStats()
        elif confirm and case_id is not None:
            case = _own_case(session, customer_id, case_id)
            language: Language = "pt" if case.language == "pt" else "es"
            facts = _confirm(session, case, redacted, pii_counts)
            stats = LLMCallStats()
        else:
            case, language, facts, stats = _understand_and_decide(
                session, deps, customer, redacted, pii_counts, case_id
            )

        reply, rstats = _reply(deps, redacted, facts, language)
        stats.add(rstats)
        write_audit(
            session,
            "agent",
            "compose",
            case.id,
            {"facts": facts},
            {"reply": reply, "fallback": rstats.fallback, "error": rstats.error},
            rstats.latency_ms,
            llm=rstats,
        )
        case.summary = _summary(case, facts)
        session.flush()

    tokens = stats.input_tokens + stats.output_tokens
    write_audit(
        session,
        "agent",
        "turn_complete",
        case.id,
        None,
        {"outcome": facts["outcome"], "tokens": tokens},
        total["ms"],
    )
    return AgentResponse(
        case_id=case.id,
        trace_id=trace_id_var.get(),
        intent=case.intent,
        reply=reply,
        outcome=facts["outcome"],
        autonomy_level=case.autonomy_level,
        actions_taken=facts["actions_taken"],
        llm_fallback=stats.fallback or rstats.fallback,
        tokens=tokens,
        latency_ms=total["ms"],
        facts=facts,
    )


# ---- understanding and deciding ---------------------------------------------------------


def _understand_and_decide(
    session: Session,
    deps: AgentDeps,
    customer: Customer,
    redacted: str,
    pii_counts: dict[str, int],
    case_id: str | None,
) -> tuple[Case, Language, Facts, LLMCallStats]:
    local = local_currency(customer.country_code)
    context = ComprehensionContext(
        now=deps.clock.now, country_code=customer.country_code, local_currency=local
    )
    clues, stats = deps.llm.comprehend(redacted, context)
    language: Language = "pt" if clues.language == "pt-BR" else "es"
    case = _open_case(session, customer.customer_id, case_id, clues.intent, language)
    write_audit(
        session,
        "agent",
        "comprehend",
        case.id,
        {"redacted_text": redacted[:500], "pii": pii_counts},
        {
            **clues.model_dump(mode="json"),
            "fallback": stats.fallback,
            "error": stats.error,
            "dropped_clues": stats.dropped_clues,
        },
        stats.latency_ms,
        llm=stats,
    )

    card = clues.card_in_possession.value if clues.card_in_possession else None
    screened = deps.policy.screen(clues.intent, language, card, False)
    if screened is not None:
        _audit_decision(
            session,
            case,
            {"intent": clues.intent, "language": language, "card_in_possession": card},
            screened,
        )
        return case, language, _apply(session, case, screened, None), stats

    policy = deps.policy.config
    params = deps.identification["rules" if stats.fallback else "llm"]
    found = identify_charge(
        session,
        deps.clock,
        customer.customer_id,
        clues,
        params,
        local,
        policy.dispute_window_days,
        case.id,
    )
    charge, twin = _chosen(clues, found)
    if charge is None and found.decision in ("show_options", "ask_for_detail"):
        return case, language, _identifying(case, found), stats

    profile = T.get_customer_profile(
        session, deps.clock, customer.customer_id, policy.open_dispute_lookback_days, case.id
    ).data
    tx = _charge_facts(session, charge, local) if charge else None
    if charge is not None:
        case.transaction_id = charge.transaction_id
    ctx = PolicyContext(
        intent=clues.intent,
        language=language,
        amount_usd=tx["amount_usd"] if tx else None,
        card_in_possession=card,
        open_dispute_last_90d=bool(profile.get("open_dispute_last_90d")),
        conformal_set_size=1 if charge else 0,
        duplicate_twin=twin,
    )
    decision = deps.policy.decide(ctx, deps.autonomy)
    _audit_decision(session, case, asdict(ctx), decision)
    return case, language, _apply(session, case, decision, tx), stats


def _chosen(
    clues: Comprehension, found: Identification
) -> tuple[Candidate | None, DuplicateTwin | None]:
    """The charge the policy decides on, and for a duplicate, the status of its twin.

    Two identical charges of a duplicate score the same, so a set of exactly those two is the
    pair the customer describes, not an ambiguity: the newer one is the charge made twice.
    """
    by_id = {s.candidate.transaction_id: s.candidate for s in found.scored}
    kept = [by_id[i] for i in found.conformal_set]
    candidates = list(by_id.values())
    if clues.intent == "billing_error_duplicate" and len(kept) == 2:
        first, second = sorted(kept, key=lambda c: (c.timestamp, c.transaction_id))
        if pair := duplicate_twin(second, [first]):
            return second, pair
    if found.decision != "identified":
        return None, None
    charge = kept[0]
    if clues.intent == "billing_error_duplicate":
        return charge, duplicate_twin(charge, candidates)
    return charge, None


def _charge_facts(session: Session, charge: Candidate, local: str) -> Facts:
    """The identified charge for the reply, with its USD amount at the rate of its date.

    Policy thresholds are in USD; a charge with no rate in the allowed days has no USD amount,
    and the policy escalates it rather than reading it as zero.
    """
    on = charge.timestamp.date()
    rates = rates_near(session, on, {(charge.currency, "USD"), (charge.currency, local)})
    return {
        "transaction_id": charge.transaction_id,
        "amount": charge.amount,
        "currency": charge.currency,
        "merchant": charge.merchant_name,
        "channel": charge.channel,
        "date": on.isoformat(),
        "status": charge.status,
        "amount_usd": to_usd(charge.amount, charge.currency, on, rates),
        "amount_display": asdict(display_amount(charge.amount, charge.currency, local, on, rates)),
    }


def _identifying(case: Case, found: Identification) -> Facts:
    by_id = {s.candidate.transaction_id: s.candidate for s in found.scored}
    case.status = "identifying"
    case.autonomy_level = "L0"
    return {
        "intent": case.intent,
        "outcome": "identifying",
        "identification": found.decision,
        # Ask-for-detail sets are not shown: the customer is asked for one more clue instead.
        "options": [
            {
                "transaction_id": c.transaction_id,
                "merchant": c.merchant_name,
                "amount": c.amount,
                "currency": c.currency,
                "date": c.timestamp.date().isoformat(),
            }
            for c in (by_id[i] for i in found.conformal_set)
        ]
        if found.decision == "show_options"
        else [],
        "actions_taken": [],
    }


def _apply(session: Session, case: Case, d: PolicyDecision, tx: Facts | None) -> Facts:
    """Carries out a decision. Nothing that changes customer data runs here: registering and
    blocking wait for the customer's confirmation of the pending action."""
    case.autonomy_level = d.level
    facts: Facts = {
        "intent": case.intent,
        "action": d.action,
        "transaction": tx,
        "redirect": d.redirect,
        "actions_taken": [],
    }
    if d.action == "security_blocked":
        # A pending action of an earlier turn must not survive a security stop.
        case.recommended_action = None
        T.escalate_to_human(session, case.id, d.rule, None, status="security_blocked")
        facts["outcome"] = "security_blocked"
    elif d.action == "escalate":
        T.escalate_to_human(session, case.id, d.rule, d.recommended)
        facts["outcome"] = "escalated"
    elif d.action == "analyst_approval":
        T.escalate_to_human(
            session, case.id, d.rule, d.recommended, status="pending_analyst_approval"
        )
        facts["outcome"] = "pending_analyst_approval"
    elif d.action in CONFIRMED_TOOLS:
        case.status = "awaiting_confirmation"
        case.recommended_action = d.action
        facts["outcome"] = "awaiting_confirmation"
    elif d.action == "abstain_and_redirect":
        case.status = "abstained"
        facts["outcome"] = "abstained"
    else:  # explain_and_watch, report_claim_status: read only
        case.status = "closed"
        facts["outcome"] = "informed"
    return facts


def _audit_decision(
    session: Session, case: Case, context: dict[str, Any], d: PolicyDecision
) -> None:
    write_audit(
        session,
        "policy",
        "decide",
        case.id,
        context,
        {
            "action": d.action,
            "rule": d.rule,
            "version": d.version,
            "level": d.level,
            "confirm": d.confirm,
            "priority": d.priority,
            "redirect": d.redirect,
            "recommended": d.recommended,
            "autonomy_level": d.autonomy_level,
        },
    )


def _stop_for_security(
    session: Session,
    deps: AgentDeps,
    customer_id: str,
    case_id: str | None,
    redacted: str,
    pii_counts: dict[str, int],
) -> tuple[Case, Language, Facts]:
    """Stops a turn that tried to reach another customer's data, without reading the message.

    No comprehension runs, so the turn spends no tokens and its text never reaches the LLM. A
    new case keeps the intent `unread`; a continued case keeps the intent it had.
    """
    if case_id is None:
        case = _open_case(session, customer_id, None, UNREAD_INTENT, "es")
    else:
        case = _own_case(session, customer_id, case_id)
    language: Language = "pt" if case.language == "pt" else "es"
    # The other customer's id is not stored: this trail belongs to the session customer.
    write_audit(
        session,
        "agent",
        "security_event",
        case.id,
        {"reason": "foreign_customer_id", "redacted_text": redacted[:500], "pii": pii_counts},
        None,
    )
    screened = deps.policy.screen(None, language, None, True)
    if screened is None:  # screen raises before returning None without an intent
        raise PolicyError("the policy did not stop a security event")
    _audit_decision(
        session, case, {"intent": None, "language": language, "security_event": True}, screened
    )
    return case, language, _apply(session, case, screened, None)


# ---- confirmation -----------------------------------------------------------------------


def _confirm(session: Session, case: Case, redacted: str, pii_counts: dict[str, int]) -> Facts:
    """Runs the pending action of the case, and only that one, once the customer confirms.

    A confirmation with nothing pending runs nothing. The pending action is the one stored when
    the policy decided, on the charge identified then, so a "sí" can never widen it.
    """
    pending = case.recommended_action if case.status == "awaiting_confirmation" else None
    write_audit(
        session,
        "agent",
        "confirm",
        case.id,
        {"redacted_text": redacted[:500], "pii": pii_counts},
        {"pending_action": pending, "transaction_id": case.transaction_id},
    )
    facts: Facts = {"intent": case.intent, "action": pending, "actions_taken": []}
    if pending not in CONFIRMED_TOOLS or case.transaction_id is None:
        facts["outcome"] = "no_pending_action"
        return facts
    for tool in CONFIRMED_TOOLS[pending]:
        if tool == "open_dispute":
            res = T.open_dispute(
                session, case.transaction_id, case.id, case.intent, f"customer confirmed {pending}"
            )
        else:
            res = T.freeze_card(session, case.customer_id, case.id, case.intent)
        if res.ok:
            facts["actions_taken"].append(tool)
    case.status = "registered"
    facts["outcome"] = "registered"
    return facts


# ---- helpers ----------------------------------------------------------------------------


def _open_case(
    session: Session, customer_id: str, case_id: str | None, intent: str, language: Language
) -> Case:
    if case_id is None:
        case = Case(
            id=f"CASE-{uuid.uuid4().hex[:10].upper()}",
            customer_id=customer_id,
            intent=intent,
            language=language,
            status="open",
            trace_id=trace_id_var.get(),
        )
        session.add(case)
        session.flush()
        return case
    case = _own_case(session, customer_id, case_id)
    case.intent, case.language = intent, language
    return case


def _own_case(session: Session, customer_id: str, case_id: str) -> Case:
    found = session.get(Case, case_id)
    # Same answer for "missing" and "not yours", so case ids cannot be probed.
    if found is None or found.customer_id != customer_id:
        raise AppError("case_not_found", f"Case {case_id} not found.", 404)
    return found


def _reply(
    deps: AgentDeps, redacted: str, facts: Facts, language: Language
) -> tuple[str, LLMCallStats]:
    # The urgent card block redirect must say the same thing every time, and a security stop
    # must not send the text of the turn to the LLM, so neither is written by it.
    if facts.get("redirect") == "card_block" or facts["outcome"] == "security_blocked":
        return template_reply(facts, language), LLMCallStats(fallback=True)
    return deps.llm.compose(redacted, _reply_facts(facts), language)


def _reply_facts(facts: Facts) -> Facts:
    """What the reply is written from: what happened to the customer's case, never the policy
    (rules, thresholds or autonomy levels), which the LLM does not see (TRZ-17 CA7)."""
    tx = facts.get("transaction")
    shown = {k: v for k, v in (tx or {}).items() if k not in ("amount_usd", "transaction_id")}
    return {
        "outcome": facts["outcome"],
        "action": facts.get("action"),
        "transaction": shown or None,
        "identification": facts.get("identification"),
        "options": facts.get("options", []),
        "actions_taken": facts["actions_taken"],
    }


def _summary(case: Case, facts: Facts) -> str:
    parts = [f"intent={case.intent}", f"outcome={facts['outcome']}"]
    if facts.get("action"):
        parts.append(f"action={facts['action']}")
    if case.transaction_id:
        parts.append(f"tx={case.transaction_id}")
    if facts["actions_taken"]:
        parts.append("actions=" + ",".join(facts["actions_taken"]))
    if case.escalation_reason:
        parts.append("reason=" + case.escalation_reason)
    return " | ".join(parts)

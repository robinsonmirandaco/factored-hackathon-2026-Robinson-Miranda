"""One customer turn (design 5): understand, screen, identify, decide, confirm, reply.

  1. redact PII
  2. comprehension: intent and clues with their literal evidence (LLM; rules when it fails),
     and the language the turn is answered in (rules, with the LLM's variant)
  3. policy screen, before any identification: security first, then the routes that need no
     charge (out of scope, lost card without a charge, claim status)
  4. identification of the disputed charge with its conformal set; several charges are shown
     as options, and a choice is accepted only when it names one of them
  5. recognition (design 6.4): an unrecognized charge is shown in detail from the database,
     and the customer says whether they recognize it before anything is decided
  6. policy decision on the identified charge, compared in USD
  7. nothing runs until the customer confirms the exact pending action of the case; each action
     is read back from the database, and only a verified one is confirmed to the customer
  8. reply from the facts; every step lands in the audit log under one trace_id

The LLM never picks tools nor decides: tool selection is code and the decision is the policy.
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit import timed, write_audit
from app.adapters.db.models import Case, CaseAction, Customer, Transaction
from app.adapters.db.rates import rates_near
from app.adapters.llm import LLMCallStats, LLMClient, template_reply
from app.core.errors import AppError
from app.core.logging import trace_id_var
from app.core.time import utcnow
from app.domain.business_days import HolidayCalendar
from app.domain.clock import SimulatedClock
from app.domain.fx import display_amount, local_currency, to_usd
from app.domain.identification import (
    Candidate,
    DuplicateTwin,
    Identification,
    Params,
    duplicate_twin,
)
from app.domain.language import LanguageDecision
from app.domain.language import decide as decide_language
from app.domain.pii import redact
from app.domain.policy import (
    AutonomyLookup,
    Language,
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    PolicyError,
)
from app.domain.policy_passages import Passage, PolicyDeadline, Unsupported, policy_deadline
from app.domain.recognition import Choice, choices, recognition_text
from app.schemas.comprehension import Comprehension, ComprehensionContext
from app.services import tools as T
from app.services.identification import candidate_of, identify_charge, load_candidates
from app.services.recognition import charge_detail
from app.services.verification import verify_block, verify_dispute

Facts = dict[str, Any]

# Intent of a new case stopped for security before its message was read.
UNREAD_INTENT = "unread"
# The option a customer picks when none of the charges shown is the one.
NONE_OF_THESE = "none"
# Escalation reason of a case whose read-back after acting did not match (TRZ-19 CA3).
VERIFICATION_FAILED_REASON = "verification.registration_failed"

# The actions that wait for the customer's confirmation, and whether confirming blocks the card
# of the charge. The offered block is not run by the "sí": the customer asks for it apart.
BLOCKS_CARD: dict[str, bool] = {
    "register": False,
    "register_and_offer_block": False,
    "register_and_block": True,
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
        passages: Demo policy passages by rule, which back the response deadline.
        calendars: Bank holiday calendars by country code.
    """

    policy: PolicyEngine
    llm: LLMClient
    clock: SimulatedClock
    identification: Mapping[str, Params]
    autonomy: AutonomyLookup
    passages: Mapping[str, Passage]
    calendars: Mapping[str, HolidayCalendar]


@dataclass(frozen=True)
class Said:
    """What the customer wrote in a turn, redacted, and the language it was read in."""

    redacted: str
    pii_counts: dict[str, int]
    spoken: LanguageDecision

    def audit(self) -> dict[str, Any]:
        """The redacted message as the audit log keeps it."""
        return {"redacted_text": self.redacted[:500], "pii": self.pii_counts}


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
    confirm_action_id: str | None = None,
    case_id: str | None = None,
    security_event: bool = False,
    recognition: Choice | None = None,
    option: str | None = None,
) -> AgentResponse:
    """Handles one customer turn end to end.

    Args:
        session: Open database session.
        deps: Policy, LLM, clock, identification parameters and autonomy lookup.
        customer_id: Customer sending the message.
        text: Raw customer message.
        confirm_action_id: The pending action of `case_id` the customer confirms.
        case_id: Case to continue, or None to open a new one.
        security_event: True when the request tried to reach another customer's data. The
            message is not read and no LLM is called: the policy stops the case, and a pending
            confirmation is not run.
        recognition: The customer's answer to the recognition step of `case_id`.
        option: The charge the customer chose among the options shown in `case_id`, or
            `none` when none of them is the one.

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
                session, deps, customer, case_id, redacted, pii_counts
            )
            stats = LLMCallStats()
        elif case_id is not None and (confirm_action_id or recognition or option):
            case = _own_case(session, customer_id, case_id)
            # No LLM call on a confirmation or a button: a short "sí" or "sim" keeps the case's
            # language.
            spoken = decide_language(redacted, None, _case_language(case), customer.country_code)
            language: Language = spoken.language
            case.language = language
            said = Said(redacted, pii_counts, spoken)
            if confirm_action_id:
                facts = _confirm(session, deps, customer, case, language, confirm_action_id, said)
            elif recognition:
                facts = _recognize(session, deps, customer, case, language, recognition, said)
            else:
                facts = _choose(session, deps, customer, case, language, str(option), said)
            stats = LLMCallStats()
        else:
            case, language, facts, stats = _understand_and_decide(
                session, deps, customer, redacted, pii_counts, case_id
            )
            # A new message that offers nothing new leaves no earlier action to confirm.
            if facts["outcome"] != "awaiting_confirmation":
                T.settle_pending_action(session, case.id, "canceled")

        reply, rstats = _reply(deps, redacted, facts, language)
        stats.add(rstats)
        # The charge detail and the recognition text carry the last four digits of the card:
        # shown to the customer, never written to the audit log. The show_charge_detail row
        # says what was shown.
        recognizing = facts["outcome"] == "recognizing"
        write_audit(
            session,
            "agent",
            "compose",
            case.id,
            {"facts": {k: v for k, v in facts.items() if k != "charge"}},
            {
                "reply": None if recognizing else reply,
                "fallback": rstats.fallback,
                "error": rstats.error,
            },
            rstats.latency_ms,
            llm=None if recognizing else rstats,
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
    # Checked before any LLM call, so a case id that is not the customer's costs no tokens.
    previous = (
        _case_language(_own_case(session, customer.customer_id, case_id)) if case_id else None
    )
    local = local_currency(customer.country_code)
    context = ComprehensionContext(
        now=deps.clock.now, country_code=customer.country_code, local_currency=local
    )
    clues, stats = deps.llm.comprehend(redacted, context)
    # The rules baseline also reads a language, but the LLM's is the one CA2 names.
    spoken = decide_language(
        redacted, None if stats.fallback else clues.language, previous, customer.country_code
    )
    language: Language = spoken.language
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
            "language_decision": asdict(spoken),
        },
        stats.latency_ms,
        llm=stats,
    )

    card = clues.card_in_possession.value if clues.card_in_possession else None
    case.card_in_possession = card
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
    said = Said(redacted, pii_counts, spoken)
    if charge is None:
        return case, language, _decide_on_charge(session, deps, customer, case, language), stats
    return (
        case,
        language,
        _identified(session, deps, customer, case, language, charge, twin, said),
        stats,
    )


def _identified(
    session: Session,
    deps: AgentDeps,
    customer: Customer,
    case: Case,
    language: Language,
    charge: Candidate,
    twin: DuplicateTwin | None,
    said: Said,
) -> Facts:
    """Once the charge is known: an unrecognized one is shown for recognition first (TRZ-16);
    a billing error, which the customer already recognizes, goes to the policy."""
    case.transaction_id = charge.transaction_id
    if case.intent != "unrecognized_charge":
        return _decide_on_charge(session, deps, customer, case, language, charge, twin)
    detail = charge_detail(
        session,
        deps.clock,
        customer.customer_id,
        charge.transaction_id,
        local_currency(customer.country_code),
        deps.policy.config.dispute_window_days,
        case.id,
    )
    if detail is None:
        return _security_stop(session, deps, case, language, said, "foreign_transaction_id")
    case.status, case.autonomy_level, case.recommended_action = "recognizing", "L0", None
    return {
        "intent": case.intent,
        "outcome": "recognizing",
        "charge": detail,
        "choices": choices(language),
        "actions_taken": [],
    }


def _decide_on_charge(
    session: Session,
    deps: AgentDeps,
    customer: Customer,
    case: Case,
    language: Language,
    charge: Candidate | None = None,
    twin: DuplicateTwin | None = None,
    note: dict[str, Any] | None = None,
) -> Facts:
    """The policy decision on the identified charge, or on no charge at all."""
    policy = deps.policy.config
    profile = T.get_customer_profile(
        session, deps.clock, customer.customer_id, policy.open_dispute_lookback_days, case.id
    ).data
    tx = _charge_facts(session, charge, local_currency(customer.country_code)) if charge else None
    if charge is not None:
        case.transaction_id = charge.transaction_id
    ctx = PolicyContext(
        intent=case.intent,
        language=language,
        amount_usd=tx["amount_usd"] if tx else None,
        card_in_possession=case.card_in_possession,
        open_dispute_last_90d=bool(profile.get("open_dispute_last_90d")),
        conformal_set_size=1 if charge else 0,
        duplicate_twin=twin,
    )
    decision = deps.policy.decide(ctx, deps.autonomy)
    _audit_decision(session, case, {**asdict(ctx), **(note or {})}, decision)
    return _apply(session, case, decision, tx)


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
    shown = found.decision == "show_options"
    case.shown_options = list(found.conformal_set) if shown else None
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
        if shown
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
    elif d.action in BLOCKS_CARD:
        case.status = "awaiting_confirmation"
        case.recommended_action = d.action
        offered = T.offer_action(session, case, d.action)
        facts["pending_action"] = {"action_id": offered.id, "action": d.action}
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
    customer: Customer,
    case_id: str | None,
    redacted: str,
    pii_counts: dict[str, int],
) -> tuple[Case, Language, Facts]:
    """Stops a turn that tried to reach another customer's data, without reading the message.

    No comprehension runs, so the turn spends no tokens and its text never reaches the LLM. A
    new case keeps the intent `unread`; a continued case keeps the intent it had. The language
    comes from the rules detector alone, which runs locally.
    """
    previous = (
        _case_language(_own_case(session, customer.customer_id, case_id)) if case_id else None
    )
    spoken = decide_language(redacted, None, previous, customer.country_code)
    language: Language = spoken.language
    if case_id is None:
        case = _open_case(session, customer.customer_id, None, UNREAD_INTENT, language)
    else:
        case = _own_case(session, customer.customer_id, case_id)
        case.language = language
    said = Said(redacted, pii_counts, spoken)
    return (
        case,
        language,
        _security_stop(session, deps, case, language, said, "foreign_customer_id"),
    )


def _security_stop(
    session: Session, deps: AgentDeps, case: Case, language: Language, said: Said, reason: str
) -> Facts:
    """Stops a case whose turn named a customer or a charge that is not the session's.

    The foreign id is not stored: this trail belongs to the session customer. The reply says
    nothing about whether the id exists.
    """
    case.shown_options = None
    T.settle_pending_action(session, case.id, "canceled")
    write_audit(
        session,
        "agent",
        "security_event",
        case.id,
        {"reason": reason, **said.audit()},
        {"language_decision": asdict(said.spoken)},
    )
    screened = deps.policy.screen(None, language, None, True)
    if screened is None:  # screen raises before returning None without an intent
        raise PolicyError("the policy did not stop a security event")
    _audit_decision(
        session, case, {"intent": None, "language": language, "security_event": True}, screened
    )
    return _apply(session, case, screened, None)


# ---- choosing and recognizing -------------------------------------------------------------


def _choose(
    session: Session,
    deps: AgentDeps,
    customer: Customer,
    case: Case,
    language: Language,
    option: str,
    said: Said,
) -> Facts:
    """Takes the charge the customer chose among the options shown in this case.

    Only an id of the options shown is accepted: any other id, even one of the customer's own
    charges, is treated as foreign and stops the case for security. "None of these" escalates,
    because no charge fits.
    """
    shown = list(case.shown_options or []) if case.status == "identifying" else []
    known = option == NONE_OF_THESE or option in shown
    write_audit(
        session,
        "agent",
        "choose",
        case.id,
        said.audit(),
        {
            "option": option if known else None,
            "shown": len(shown),
            "language_decision": asdict(said.spoken),
        },
    )
    case.shown_options = None
    if option == NONE_OF_THESE:
        if not shown:
            return {"intent": case.intent, "outcome": "no_pending_choice", "actions_taken": []}
        return _decide_on_charge(
            session, deps, customer, case, language, note={"customer_choice": NONE_OF_THESE}
        )
    if option not in shown:
        return _security_stop(session, deps, case, language, said, "option_not_shown")
    tx = session.get(Transaction, option)
    if tx is None or tx.customer_id != customer.customer_id:
        return _security_stop(session, deps, case, language, said, "foreign_transaction_id")
    charge = candidate_of(tx)
    twin = None
    if case.intent == "billing_error_duplicate":
        twin = duplicate_twin(charge, _candidates(session, deps, customer))
    return _identified(session, deps, customer, case, language, charge, twin, said)


def _recognize(
    session: Session,
    deps: AgentDeps,
    customer: Customer,
    case: Case,
    language: Language,
    choice: Choice,
    said: Said,
) -> Facts:
    """The customer's answer to the recognition step (TRZ-16 CA5 and CA7).

    "Ya lo reconozco" closes the case with nothing done. "Sigo sin reconocerlo" goes straight to
    the policy, with no further question. A charge with an identical twin that is also approved
    is decided as a duplicate charge when the customer has the card; without the card it stays
    on the fraud path, which blocks it.
    """
    waiting = case.status == "recognizing" and case.transaction_id is not None
    write_audit(
        session,
        "agent",
        "recognize",
        case.id,
        said.audit(),
        {
            "choice": choice,
            "transaction_id": case.transaction_id if waiting else None,
            "waiting": waiting,
            "language_decision": asdict(said.spoken),
        },
    )
    if not waiting:
        return {"intent": case.intent, "outcome": "no_pending_recognition", "actions_taken": []}
    if choice == "recognized":
        case.status, case.autonomy_level = "recognized_closed", "L0"
        return {"intent": case.intent, "outcome": "recognized_closed", "actions_taken": []}
    tx = session.get(Transaction, case.transaction_id)
    if tx is None or tx.customer_id != customer.customer_id:
        return _security_stop(session, deps, case, language, said, "foreign_transaction_id")
    charge = candidate_of(tx)
    twin = duplicate_twin(charge, _candidates(session, deps, customer))
    if twin == "both_approved" and case.card_in_possession is not False:
        case.intent = "billing_error_duplicate"
        return _decide_on_charge(
            session,
            deps,
            customer,
            case,
            language,
            charge,
            twin,
            note={"rerouted_from": "unrecognized_charge"},
        )
    return _decide_on_charge(session, deps, customer, case, language, charge)


def _candidates(session: Session, deps: AgentDeps, customer: Customer) -> list[Candidate]:
    return load_candidates(
        session, customer.customer_id, deps.clock, deps.policy.config.dispute_window_days
    )


# ---- confirmation -----------------------------------------------------------------------


def _confirm(
    session: Session,
    deps: AgentDeps,
    customer: Customer,
    case: Case,
    language: Language,
    action_id: str,
    said: Said,
) -> Facts:
    """Runs the pending action the customer confirms, and only that one (TRZ-18 CA3).

    The confirmation names the action. One that was replaced, cancelled or belongs to another
    case runs nothing. One already executed runs its tools again, which return their stored
    results: the same folio, no new rows (CA4). The action row is locked, so two confirmations
    sent at once run one after the other.

    Every action that reported success is read back (TRZ-19). When all match, the case is
    `registered_verified`. When one does not, the case is `failed` and escalated, and the
    customer gets no confirmation; without the card and without a verified block, the reply
    still sends the customer to block it.
    """
    row = session.execute(
        select(CaseAction).where(CaseAction.id == action_id).with_for_update()
    ).scalar_one_or_none()
    status = row.status if row is not None and row.case_id == case.id else None
    runnable = row is not None and status in ("pending", "executed")
    write_audit(
        session,
        "agent",
        "confirm",
        case.id,
        said.audit(),
        {
            "action_id": action_id if status else None,
            "action_status": status,
            "pending_action": row.action if runnable and row else None,
            "transaction_id": row.transaction_id if runnable and row else None,
            "language_decision": asdict(said.spoken),
        },
    )
    if not runnable or row is None:
        return {
            "intent": case.intent,
            "action": None,
            "outcome": "no_pending_action",
            "actions_taken": [],
        }
    facts: Facts = {"intent": case.intent, "action": row.action, "actions_taken": []}
    try:
        # A foreign id found by a tool must leave nothing behind, not even the dispute.
        with session.begin_nested():
            dispute = T.register_dispute(
                session,
                deps.clock,
                _deadline(deps, customer, language),
                customer.customer_id,
                case.id,
                row.transaction_id,
                case.intent,
            )
            block = (
                T.block_card(
                    session, customer.customer_id, case.id, row.transaction_id, case.intent
                )
                if BLOCKS_CARD[row.action]
                else None
            )
    except T.OwnershipError:
        return _security_stop(session, deps, case, language, said, "foreign_transaction_id")
    backed = _deadline(deps, customer, language)(deps.clock.now.date())
    checks = [
        verify_dispute(
            session,
            case.id,
            customer.customer_id,
            row.transaction_id,
            case.intent,
            deps.clock.now,
            backed.due if isinstance(backed, PolicyDeadline) else None,
            str(dispute.data.get("folio", "")),
        )
    ]
    if block is not None and block.ok:
        checks.append(
            verify_block(
                session,
                case.id,
                str(block.data.get("product_id", "")),
                str(block.data.get("status_before", "")),
            )
        )
    verified = [c.action for c in checks if c.verified]
    if block is not None and "block_card" not in verified:
        if not block.ok:
            facts["card_not_blocked"] = block.message
        # Without the card and without a verified block, the customer still has to stop it.
        if case.card_in_possession is False:
            facts["redirect"] = "card_block"
    # A replay reports what was done and leaves the case as later turns left it.
    first_run = row.status == "pending"
    if first_run:
        row.status, row.resolved_at = "executed", utcnow()
    if len(verified) < len(checks):
        # Nothing is confirmed to the customer: the case goes to a person with the reason.
        case.autonomy_level = deps.policy.config.action_level["escalate"]
        T.escalate_to_human(session, case.id, VERIFICATION_FAILED_REASON, status="failed")
        facts["unverified"] = [c.action for c in checks if not c.verified]
        facts["outcome"] = "failed"
        return facts
    facts["actions_taken"] = verified
    facts["dispute"] = {"folio": dispute.data["folio"], "due_date": dispute.data["due_date"]}
    if first_run:
        case.status = "registered_verified"
    facts["outcome"] = "registered_verified"
    return facts


def _deadline(
    deps: AgentDeps, customer: Customer, language: Language
) -> Callable[[date], PolicyDeadline | Unsupported]:
    def due(start: date) -> PolicyDeadline | Unsupported:
        return policy_deadline(
            deps.passages,
            deps.calendars,
            "response_deadline",
            start,
            customer.country_code,
            language,
        )

    return due


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


def _case_language(case: Case) -> Language | None:
    return "pt" if case.language == "pt" else "es" if case.language == "es" else None


def _own_case(session: Session, customer_id: str, case_id: str) -> Case:
    found = session.get(Case, case_id)
    # Same answer for "missing" and "not yours", so case ids cannot be probed.
    if found is None or found.customer_id != customer_id:
        raise AppError("case_not_found", f"Case {case_id} not found.", 404)
    return found


def _reply(
    deps: AgentDeps, redacted: str, facts: Facts, language: Language
) -> tuple[str, LLMCallStats]:
    # The urgent card block redirect must say the same thing every time, a security stop must
    # not send the text of the turn to the LLM, and a failed read-back confirms nothing, so none
    # of them is written by it.
    if facts.get("redirect") == "card_block" or facts["outcome"] in ("security_blocked", "failed"):
        return template_reply(facts, language), LLMCallStats(fallback=True)
    # The recognition step is written by code by design (TRZ-16), not as an LLM fallback.
    if facts["outcome"] == "recognizing":
        return recognition_text(facts["charge"], language), LLMCallStats()
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
        "dispute": facts.get("dispute"),
        "card_not_blocked": facts.get("card_not_blocked"),
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

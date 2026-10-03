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
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit import timed, write_audit
from app.adapters.db.models import Case, CaseAction, Customer, Dispute, Product, Transaction
from app.adapters.db.rates import rates_near
from app.adapters.llm import LLMCallStats, LLMClient, TurnBudget, template_reply
from app.core.errors import AppError
from app.core.logging import trace_id_var
from app.core.time import utcnow
from app.domain.business_days import HolidayCalendar
from app.domain.clock import SimulatedClock
from app.domain.comprehension_rules import comprehend_rules
from app.domain.comprehension_rules import recognizes as rules_recognize
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
from app.domain.out_of_scope import topic as out_of_scope_topic
from app.domain.pii import redact
from app.domain.policy import (
    AutonomyLookup,
    Language,
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    PolicyError,
    Priority,
)
from app.domain.policy_passages import Passage, PolicyDeadline, Unsupported, policy_deadline
from app.domain.recognition import Choice, choices, recognition_text
from app.domain.security_text import mask_ids, read_signals
from app.schemas.comprehension import Comprehension, ComprehensionContext, evidence_is_faithful
from app.services import tools as T
from app.services.audit_sample import sample
from app.services.automation import all_to_human
from app.services.cases import HANDOFF_STATUSES, VERIFICATION_FAILED_REASON
from app.services.clues import CLUE_FIELDS, Clues, last_clues
from app.services.identification import (
    candidate_of,
    identify_by_button,
    identify_charge,
    load_candidates,
)
from app.services.recognition import charge_detail
from app.services.replies import (
    HANDOFF_OUTCOMES,
    check_reply,
    deadline_note,
    handoff_note,
    verified_facts,
)
from app.services.verification import verify_block, verify_dispute

Facts = dict[str, Any]

# Rule id of a case the global automation switch sent to a person (TRZ-35).
AUTOMATION_DISABLED_RULE = "escalate.automation_disabled"

# Intent of a new case stopped for security before its message was read.
UNREAD_INTENT = "unread"
# Outcome of a message on a case already with a person: nothing is decided again.
WITH_PERSON = "with_person"
# Who read the clues of an answer to a question of the case: none for an answer without one.
AnswerRead = Literal["llm", "rules", "none"]
# The option a customer picks when none of the charges shown is the one.
NONE_OF_THESE = "none"
# A case that ended is never reopened: a registered dispute, a recognized charge, an answer, a
# redirect or a decision of a person stays as it ended. A new message names a new case.
FINAL_STATUSES = (
    "registered_verified",
    "recognized_closed",
    "closed",
    "abstained",
    "approved",
    "rejected",
    "expired",
    # Waiting for the customer's answer to an analyst: it is answered from Mis aclaraciones,
    # never from the chat (TRZ-28).
    "awaiting_customer",
    # An audit sample an analyst reversed (TRZ-29): its dispute stays, a person reviews it.
    "in_review",
)
# A case in one of these statuses holds its charge: another case on the same charge would be a
# second clarification of one charge, so the customer is taken back to it instead.
HOLDS_CHARGE = (
    "recognizing",
    "awaiting_confirmation",
    "escalated",
    "pending_analyst_approval",
    "failed",
    "registered_verified",
    "awaiting_customer",
    "in_review",
)
# Outcomes answered with a fixed reply instead of the LLM's: a security stop must not send the
# text of the turn to the LLM, and a failed read-back confirms nothing.
FIXED_OUTCOMES = ("security_blocked", "failed")
# Outcomes code writes by design, not as a fallback: what happened to the card or to a declined
# offer, and a handoff, whose reason and review time come from the decision (the LLM once wrote
# a doubt the customer never voiced and a contact promise instead). So is the receipt of a
# registration: folio and deadline with its citation (the LLM kept adding promises of news the
# fact checker cannot see, a new wording each time).
CODE_WRITTEN_OUTCOMES = (
    "registered_verified",
    "existing_case",
    "no_pending_action",
    "no_pending_recognition",
    "no_pending_choice",
    "card_blocked",
    "declined",
    "block_declined",
    "block_not_run",
    "escalated",
    "pending_analyst_approval",
)

# The actions that wait for the customer's confirmation, and whether confirming blocks the card
# of the charge. The "sí" to register_and_offer_block registers only: the block is then offered
# as its own action, "block", with its own confirmation (design 3.2: explicit for L2).
BLOCKS_CARD: dict[str, bool] = {
    "register": False,
    "register_and_offer_block": False,
    "register_and_block": True,
    "block": True,
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
        fact_check: False turns the fact checker into an observer (ablation, TRZ-20 CA6).
    """

    policy: PolicyEngine
    llm: LLMClient
    clock: SimulatedClock
    identification: Mapping[str, Params]
    autonomy: AutonomyLookup
    passages: Mapping[str, Passage]
    calendars: Mapping[str, HolidayCalendar]
    fact_check: bool = True


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
    clues: list["Chip"] = field(default_factory=list)


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
    transaction_id: str | None = None,
    decline_action_id: str | None = None,
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
        transaction_id: The charge a "No lo reconozco" or "¿Qué es esto?" button was pressed
            on. It opens a new case through the button door (TRZ-15 CA9): the charge arrives
            chosen, is only checked, and no LLM is called; `text` is the button's label.
        decline_action_id: The pending action of `case_id` the customer declines ("No,
            gracias"): it is cancelled and nothing runs.

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
    # One retry of the LLM in the whole turn, shared by comprehension and reply (TRZ-36 CA3).
    budget = deps.llm.new_turn()
    # What the system read in this message, shown back to the customer; buttons read nothing.
    read: list[Chip] = []
    with timed() as total:
        redacted, pii_counts = redact(text, name=customer.first_name)
        if security_event:
            case, language, facts = _stop_for_security(
                session, deps, customer, case_id, redacted, pii_counts
            )
            stats = LLMCallStats()
        elif transaction_id is not None:
            case, language, facts = _by_button(
                session,
                deps,
                customer,
                Said(redacted, pii_counts, _button_language(redacted, customer)),
                transaction_id,
            )
            stats = LLMCallStats()
        elif case_id is not None and _with_person(session, customer_id, case_id):
            # A person has the case: a message or a button pressed on it is added to the
            # dossier and answered the same way, by code; nothing is decided again.
            case = _own_case(session, customer_id, case_id)
            spoken = decide_language(redacted, None, _case_language(case), customer.country_code)
            language = spoken.language
            facts = _note_for_the_analyst(session, case, Said(redacted, pii_counts, spoken))
            stats = LLMCallStats()
        elif case_id is not None and (
            confirm_action_id or recognition or option or decline_action_id
        ):
            case = _own_case(session, customer_id, case_id)
            # No LLM call on a confirmation or a button: a short "sí" or "sim" keeps the case's
            # language.
            spoken = decide_language(redacted, None, _case_language(case), customer.country_code)
            language: Language = spoken.language
            case.language = language
            said = Said(redacted, pii_counts, spoken)
            if confirm_action_id:
                facts = _confirm(session, deps, customer, case, language, confirm_action_id, said)
            elif decline_action_id:
                facts = _decline(session, case, decline_action_id, said)
            elif recognition:
                facts = _recognize(session, deps, customer, case, language, recognition, said)
            else:
                facts = _choose(session, deps, customer, case, language, str(option), said)
            stats = LLMCallStats()
        elif (reason := _text_security(session, customer_id, text)) is not None:
            case, language, facts = _stop_for_security(
                session, deps, customer, case_id, redacted, pii_counts, reason
            )
            stats = LLMCallStats()
        else:
            case, language, facts, stats, read = _understand_and_decide(
                session, deps, customer, redacted, pii_counts, case_id, budget
            )
            # A new message that offers nothing new leaves no earlier action to confirm.
            if facts["outcome"] != "awaiting_confirmation":
                T.settle_pending_action(session, case.id, "canceled")

        if moved := facts.pop("moved_to", None):
            # The charge already had a case: the turn continues on it.
            case = _own_case(session, customer_id, moved)
        reply, rstats = _reply(session, deps, customer, case, redacted, facts, language, budget)
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
                "attempts": rstats.calls,
            },
            rstats.latency_ms,
            # A reply written by code made no LLM call and has no model, tokens or cost.
            llm=None if recognizing or not rstats.calls else rstats,
        )
        # A note on a case with a person leaves its summary as the handoff wrote it.
        if facts["outcome"] != WITH_PERSON:
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
        clues=read,
    )


# ---- understanding and deciding ---------------------------------------------------------


@dataclass(frozen=True)
class Chip:
    """One clue as the customer's screen shows it: what was read and the literal fragment of
    the (redacted) message it was read from. Nothing in it comes from the database."""

    field: str
    value: str
    evidence: str
    # A date is read as a window of days, resolved against the simulated now.
    window_from: date | None = None
    window_to: date | None = None
    # An amount goes as a number, so the screen writes it in its own language's format.
    amount: float | None = None
    currency: str | None = None


def chips(clues: Comprehension) -> list[Chip]:
    """The clues of a reading as chips, in the order of CLUE_FIELDS.

    Args:
        clues: The comprehension of the turn, after merging an answer with earlier clues.

    Returns:
        One chip per clue that was read.
    """
    out = []
    if clues.amount:
        a = clues.amount
        plain = f"{a.value:.2f}"
        out.append(
            Chip(
                "amount",
                f"{plain} {a.currency}" if a.currency else plain,
                a.evidence,
                amount=a.value,
                currency=a.currency,
            )
        )
    if clues.date:
        first, last = clues.date.window()
        out.append(Chip("date", clues.date.expression, clues.date.evidence, first, last))
    if clues.merchant_hint:
        out.append(Chip("merchant_hint", clues.merchant_hint.value, clues.merchant_hint.evidence))
    if clues.channel_hint:
        out.append(Chip("channel_hint", str(clues.channel_hint.value), clues.channel_hint.evidence))
    if clues.card_in_possession:
        c = clues.card_in_possession
        out.append(Chip("card_in_possession", "yes" if c.value else "no", c.evidence))
    return out


def _understand_and_decide(
    session: Session,
    deps: AgentDeps,
    customer: Customer,
    redacted: str,
    pii_counts: dict[str, int],
    case_id: str | None,
    budget: TurnBudget,
) -> tuple[Case, Language, Facts, LLMCallStats, list[Chip]]:
    # Checked before any LLM call, so a case id that is not the customer's costs no tokens.
    existing = _own_case(session, customer.customer_id, case_id) if case_id else None
    previous = _case_language(existing) if existing else None
    if existing is not None and existing.status in FINAL_STATUSES:
        # Its language still guides a short message; everything else starts over.
        existing, case_id = None, None
    local = local_currency(customer.country_code)
    context = ComprehensionContext(
        now=deps.clock.now, country_code=customer.country_code, local_currency=local
    )
    clues, stats = deps.llm.comprehend(redacted, context, budget)
    # With the LLM down, the rules answer; when they recognize nothing either, no intent is
    # known, and the case goes to a person instead of being told it is out of scope (TRZ-36).
    # An LLM switched off by configuration is not down: the rules are then the design.
    down = stats.fallback and stats.error != "llm_disabled"
    unavailable = down and not rules_recognize(redacted)
    # The rules baseline also reads a language, but the LLM's is the one CA2 names.
    spoken = decide_language(
        redacted, None if stats.fallback else clues.language, previous, customer.country_code
    )
    language: Language = spoken.language
    # A message sent while the case waits for a clue answers that question (TRZ-25): the case
    # keeps its intent and adds the new clues to the earlier ones.
    waiting = (
        existing is not None
        and existing.status == "identifying"
        and existing.intent in deps.policy.config.dispute_intents
    )
    answer = _as_answer(clues, redacted, context) if waiting else None
    answering = answer is not None
    earlier = last_clues(session, existing) if answering and existing else None
    intent = existing.intent if answering and existing else clues.intent
    case = _open_case(session, customer.customer_id, case_id, intent, language)
    read = write_audit(
        session,
        "agent",
        "comprehend",
        case.id,
        {"redacted_text": redacted[:500], "pii": pii_counts},
        {
            **clues.model_dump(mode="json"),
            "fallback": stats.fallback,
            "error": stats.error,
            "attempts": stats.calls,
            "comprehension_unavailable": unavailable,
            "dropped_clues": stats.dropped_clues,
            "language_decision": asdict(spoken),
        },
        stats.latency_ms,
        llm=stats,
    )
    if answer is not None:
        clues = _merge_clues(session, case, earlier, answer[0], read.id, answer[1])

    card = clues.card_in_possession.value if clues.card_in_possession else None
    case.card_in_possession = card
    screened = deps.policy.screen(
        case.intent, language, card, False, unavailable and answer is None
    )
    if screened is not None:
        _audit_decision(
            session,
            case,
            {
                "intent": case.intent,
                "language": language,
                "card_in_possession": card,
                "comprehension_unavailable": unavailable and answer is None,
            },
            screened,
        )
        facts = _apply(session, deps, case, screened, None)
        if screened.action == "report_claim_status":
            facts = _claim_status(session, deps, customer, case, language, facts)
        elif screened.action == "abstain_and_redirect" and screened.redirect is None:
            facts["topic"] = out_of_scope_topic(redacted)
        return case, language, facts, stats, chips(clues)

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
        if case.clarifications >= policy.max_clarifications:
            # One more question than the policy allows: a person takes the case (design 5.1).
            exhausted = _decide_on_charge(
                session,
                deps,
                customer,
                case,
                language,
                conformal_set_size=len(found.conformal_set),
                clarifications_exhausted=True,
            )
            return case, language, exhausted, stats, chips(clues)
        return case, language, _identifying(case, found), stats, chips(clues)
    said = Said(redacted, pii_counts, spoken)
    if charge is None:
        decided = _decide_on_charge(session, deps, customer, case, language)
        return case, language, decided, stats, chips(clues)
    return (
        case,
        language,
        _identified(session, deps, customer, case, language, charge, twin, said),
        stats,
        chips(clues),
    )


def _as_answer(
    clues: Comprehension, redacted: str, context: ComprehensionContext
) -> tuple[Comprehension, AnswerRead] | None:
    """Whether a message sent while the case waits for a clue answers it, and with what clues.

    Without the question, the model reads a short answer such as "fueron 900" as out of scope,
    although it reads the amount. So in a case that waits for a clue:
      - a claim question is not an answer: it is served with its own intent;
      - any other reading with a clue is an answer;
      - an out of scope reading without a clue is read again by the rules, and a clue they find
        with its literal fragment makes it an answer;
      - with no clue at all, a request with a known out of scope topic ("quiero un préstamo") is
        served with its own intent, and anything else ("no sé") is an answer with no clue, which
        still counts as a clarification.

    Returns:
        The clues and who read them (llm, rules, or none for an empty answer), or None when the
        message is not an answer.
    """
    if clues.intent == "claim_status":
        return None
    if clues.intent != "out_of_scope" or _has_clues(clues):
        return clues, "llm"
    ruled = comprehend_rules(redacted, context)
    found = {
        k: getattr(ruled, k)
        for k in CLUE_FIELDS
        if getattr(ruled, k) is not None
        and evidence_is_faithful(getattr(ruled, k).evidence, redacted)
    }
    if found:
        return clues.model_copy(update=found), "rules"
    if out_of_scope_topic(redacted) != "other":
        return None
    return clues, "none"


def _has_clues(clues: Comprehension) -> bool:
    return any(getattr(clues, k) is not None for k in CLUE_FIELDS)


def _merge_clues(
    session: Session,
    case: Case,
    earlier: Clues | None,
    new: Comprehension,
    read_id: int,
    read_by: AnswerRead,
) -> Comprehension:
    """Adds the clues of an answer to the earlier ones of the case (TRZ-25): a new clue replaces
    the earlier one of its field, the others are kept, and the case keeps its intent.

    Each clue keeps its literal evidence and the comprehension row it was read in, so it can be
    checked against the message it came from.
    """
    if earlier is None:
        write_audit(
            session,
            "agent",
            "merge_clues",
            case.id,
            {"read_id": read_id, "read_by": read_by},
            {"merged": False, "reason": "no earlier clues that validate"},
        )
        return new.model_copy(update={"intent": case.intent})
    before, sources = earlier.clues, dict(earlier.sources)
    fields: dict[str, Any] = {}
    for k in CLUE_FIELDS:
        if getattr(new, k) is not None:
            fields[k], sources[k] = getattr(new, k), read_id
        else:
            fields[k] = getattr(before, k)
    merged = Comprehension(intent=case.intent, language=new.language, **fields)
    write_audit(
        session,
        "agent",
        "merge_clues",
        case.id,
        {"read_id": read_id, "read_by": read_by},
        {"merged": True, "clues": merged.model_dump(mode="json"), "sources": sources},
    )
    return merged


def _claim_status(
    session: Session,
    deps: AgentDeps,
    customer: Customer,
    case: Case,
    language: Language,
    facts: Facts,
) -> Facts:
    """Reads the open claims of the customer (TRZ-22). Read only: nothing is written but the
    audit row. One claim is reported; several are shown as options to choose from."""
    claims = T.get_open_claims(
        session, deps.clock, _deadline(deps, customer, language), customer.customer_id, case.id
    ).data["claims"]
    if len(claims) > 1:
        case.status = "identifying"
        case.shown_options = [c["claim_id"] for c in claims]
        return {
            **facts,
            "outcome": "identifying",
            "identification": "show_claims",
            "claims": claims,
        }
    return {**facts, "claim": claims[0] if claims else None}


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
    a billing error, which the customer already recognizes, goes to the policy. A charge that
    another case already holds sends the turn to that case, and this one is closed."""
    held = _case_on_charge(session, customer.customer_id, charge.transaction_id, case.id)
    if held is not None:
        write_audit(
            session,
            "agent",
            "existing_case",
            case.id,
            said.audit(),
            {"existing_case_id": held.id, "status": held.status},
        )
        T.settle_pending_action(session, case.id, "canceled")
        case.status, case.shown_options = "closed", None
        return {"moved_to": held.id, **_resume(session, deps, customer, held, language, said)}
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
    conformal_set_size: int | None = None,
    clarifications_exhausted: bool = False,
) -> Facts:
    """The policy decision on the identified charge, or on no charge at all.

    Without a charge the conformal set is empty, unless the charge is still among several and
    the questions to tell them apart are exhausted.
    """
    policy = deps.policy.config
    profile = T.get_customer_profile(
        session, deps.clock, customer.customer_id, policy.open_dispute_lookback_days, case.id
    ).data
    tx = _charge_facts(session, charge, local_currency(customer.country_code)) if charge else None
    # A decision without a charge leaves none on the case: one kept from an earlier step would
    # show the case as that charge's and hold the charge against a real claim.
    case.transaction_id = charge.transaction_id if charge is not None else None
    ctx = PolicyContext(
        intent=case.intent,
        language=language,
        amount_usd=tx["amount_usd"] if tx else None,
        card_in_possession=case.card_in_possession,
        open_dispute_last_90d=bool(profile.get("open_dispute_last_90d")),
        conformal_set_size=(
            conformal_set_size if conformal_set_size is not None else 1 if charge else 0
        ),
        duplicate_twin=twin,
        clarifications_exhausted=clarifications_exhausted,
        automation_disabled=all_to_human(session),
    )
    decision = deps.policy.decide(ctx, deps.autonomy)
    _audit_decision(session, case, {**asdict(ctx), **(note or {})}, decision)
    return _apply(session, deps, case, decision, tx)


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
    case.transaction_id = None
    case.autonomy_level = "L0"
    case.clarifications += 1
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


def _apply(
    session: Session, deps: AgentDeps, case: Case, d: PolicyDecision, tx: Facts | None
) -> Facts:
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
        T.escalate_to_human(
            session, case.id, d.rule, _sla(deps, d.priority), None, "security_blocked", d.priority
        )
        facts["outcome"] = "security_blocked"
    elif d.action == "escalate":
        T.escalate_to_human(
            session, case.id, d.rule, _sla(deps, d.priority), d.recommended, priority=d.priority
        )
        facts["outcome"] = "escalated"
        facts["handoff_reason"], facts["review_hours"] = d.rule, _sla(deps, d.priority)
    elif d.action == "analyst_approval":
        T.escalate_to_human(
            session,
            case.id,
            d.rule,
            _sla(deps, d.priority),
            d.recommended,
            "pending_analyst_approval",
            d.priority,
        )
        facts["outcome"] = "pending_analyst_approval"
        facts["handoff_reason"], facts["review_hours"] = d.rule, _sla(deps, d.priority)
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


def _sla(deps: AgentDeps, priority: Priority) -> float:
    return deps.policy.config.queue.sla_hours[priority]


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


def _with_person(session: Session, customer_id: str, case_id: str) -> bool:
    return _own_case(session, customer_id, case_id).status in HANDOFF_STATUSES


def _note_for_the_analyst(session: Session, case: Case, said: Said) -> Facts:
    """A message on a case already with a person (TRZ-25): it is not read by the LLM nor decided
    again, and the case keeps its status and its single queue item. The message, redacted, is
    added to the dossier for the analyst, and the customer is told the case is with a person."""
    write_audit(
        session,
        "agent",
        "customer_note",
        case.id,
        said.audit(),
        {"status": case.status, "language_decision": asdict(said.spoken)},
    )
    return {"intent": case.intent, "outcome": WITH_PERSON, "actions_taken": []}


def _stop_for_security(
    session: Session,
    deps: AgentDeps,
    customer: Customer,
    case_id: str | None,
    redacted: str,
    pii_counts: dict[str, int],
    reason: str = "foreign_customer_id",
) -> tuple[Case, Language, Facts]:
    """Stops a turn that tried to reach another customer's data, without reading the message.

    No comprehension runs, so the turn spends no tokens and its text never reaches the LLM. A
    new case keeps the intent `unread`; a continued case keeps the intent it had. The language
    comes from the rules detector alone, which runs locally. The same stop serves a message
    whose text names another customer or carries an injected instruction (`reason`).
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
    said = Said(mask_ids(redacted), pii_counts, spoken)
    return (
        case,
        language,
        _security_stop(session, deps, case, language, said, reason),
    )


def _text_security(session: Session, customer_id: str, text: str) -> str | None:
    """The reason to stop a free-text turn for security, or None (TRZ-46 follow-up).

    A charge or product id in the text that row level security does not show as the session
    customer's counts as another customer's: the reply never tells whether it exists. An
    injected instruction is never obeyed, because the message does not reach the LLM; the case
    goes to a person.
    """
    signals = read_signals(text, customer_id)
    foreign = any(
        getattr(
            session.get(Transaction if i.startswith("TRX-") else Product, i), "customer_id", None
        )
        != customer_id
        for i in signals.owned_ids
    )
    if signals.other_customer or foreign:
        return "other_customer_in_text"
    if signals.injection:
        return "instruction_in_text"
    return None


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
    return _apply(session, deps, case, screened, None)


def _button_language(redacted: str, customer: Customer) -> LanguageDecision:
    # The label is written in the language of the customer's screen.
    return decide_language(redacted, None, None, customer.country_code)


def _by_button(
    session: Session, deps: AgentDeps, customer: Customer, said: Said, transaction_id: str
) -> tuple[Case, Language, Facts]:
    """Opens an unrecognized charge case on the charge whose button was pressed.

    One of the customer's disputable charges goes to the recognition step, as a charge
    identified in conversation does. A charge the session cannot see is another customer's, or
    does not exist, and stops the case for security. One of the customer's own that is not
    disputable is decided as no charge found.
    """
    language: Language = said.spoken.language
    held = (
        _case_on_charge(session, customer.customer_id, transaction_id)
        if session.get(Transaction, transaction_id) is not None
        else None
    )
    if held is not None:
        write_audit(
            session,
            "agent",
            "existing_case",
            held.id,
            said.audit(),
            {"door": "button", "status": held.status},
        )
        return held, language, _resume(session, deps, customer, held, language, said)
    case = _open_case(session, customer.customer_id, None, "unrecognized_charge", language)
    # The id is not written here: until it is known to be the customer's, it stays out of
    # this customer's trail, as in _security_stop.
    write_audit(
        session,
        "agent",
        "button_press",
        case.id,
        said.audit(),
        {"language_decision": asdict(said.spoken)},
    )
    found = identify_by_button(
        session,
        deps.clock,
        customer.customer_id,
        transaction_id,
        deps.policy.config.dispute_window_days,
        case.id,
    )
    tx = session.get(Transaction, transaction_id)
    if tx is None:
        return (
            case,
            language,
            _security_stop(session, deps, case, language, said, "foreign_transaction_id"),
        )
    if found.decision != "identified":
        return case, language, _decide_on_charge(session, deps, customer, case, language)
    charge = candidate_of(tx)
    return case, language, _identified(session, deps, customer, case, language, charge, None, said)


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
            "kind": "claim" if case.intent == "claim_status" else "charge",
            "language_decision": asdict(said.spoken),
        },
    )
    case.shown_options = None
    if option == NONE_OF_THESE:
        if not shown:
            return {"intent": case.intent, "outcome": "no_pending_choice", "actions_taken": []}
        if case.intent == "claim_status":
            case.status = "closed"
            return _claim_facts(case, None)
        return _decide_on_charge(
            session, deps, customer, case, language, note={"customer_choice": NONE_OF_THESE}
        )
    if option not in shown:
        return _security_stop(session, deps, case, language, said, "option_not_shown")
    if case.intent == "claim_status":
        claims = T.get_open_claims(
            session, deps.clock, _deadline(deps, customer, language), customer.customer_id, case.id
        ).data["claims"]
        case.status = "closed"
        # A claim closed since it was shown is no longer open: none of the shown ones is left.
        return _claim_facts(case, next((c for c in claims if c["claim_id"] == option), None))
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


def _claim_facts(case: Case, claim: Facts | None) -> Facts:
    """The claim chosen among the ones shown, or none of them: then a person is offered."""
    return {
        "intent": case.intent,
        "action": "report_claim_status",
        "outcome": "informed",
        "claim": claim,
        "other_claim": claim is None,
        "redirect": None,
        "actions_taken": [],
    }


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

    The dispute is registered and read back first; the card is blocked only when the dispute
    verified, so a failed registration never leaves a blocked card without its dispute.
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
    if row.status == "pending" and all_to_human(session):
        return _automation_off(session, deps, case, row)
    if row.action == "block":
        return _confirm_block(session, deps, case, language, row, said)
    facts: Facts = {"intent": case.intent, "action": row.action, "actions_taken": []}
    backed = _deadline(deps, customer, language)(deps.clock.now.date())
    block = None
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
            # The card is blocked only once its dispute reads back: a failed registration must
            # not leave the card blocked without the dispute (TRZ-18 follow-up).
            if BLOCKS_CARD[row.action] and checks[0].verified:
                block = T.block_card(
                    session, customer.customer_id, case.id, row.transaction_id, case.intent
                )
    except T.OwnershipError:
        return _security_stop(session, deps, case, language, said, "foreign_transaction_id")
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
    if BLOCKS_CARD[row.action] and "block_card" not in verified:
        if block is None:
            facts["card_not_blocked"] = "dispute_not_verified"
        elif not block.ok:
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
        handoff = T.escalate_to_human(
            session, case.id, VERIFICATION_FAILED_REASON, _sla(deps, "normal"), status="failed"
        )
        if handoff.message:
            # The case was escalated before: its queue entry stays one, but the failure still
            # moves the case to failed and is written down with its reason.
            case.status, case.escalation_reason = "failed", VERIFICATION_FAILED_REASON
            write_audit(
                session,
                "agent",
                "verification_failed",
                case.id,
                {"unverified": [c.action for c in checks if not c.verified]},
                {"status": "failed", "reason": VERIFICATION_FAILED_REASON},
            )
        facts["unverified"] = [c.action for c in checks if not c.verified]
        facts["outcome"] = "failed"
        return facts
    facts["actions_taken"] = verified
    facts["dispute"] = {
        "folio": dispute.data["folio"],
        "registered_on": deps.clock.now.date().isoformat(),
        "due_date": dispute.data.get("due_date"),
        "passage": dispute.data.get("due_date_passage"),
    }
    tx = session.get(Transaction, row.transaction_id)
    if tx is not None:
        facts["transaction"] = _charge_facts(
            session, candidate_of(tx), local_currency(customer.country_code)
        )
    if first_run:
        case.status = "registered_verified"
        sample(session, deps.policy.config, case)
    if row.action == "register_and_offer_block":
        offer = T.offer_action(session, case, "block") if first_run else _pending_row(session, case)
        if offer is not None and offer.action == "block":
            facts["pending_action"] = {"action_id": offer.id, "action": "block"}
    facts["outcome"] = "registered_verified"
    return facts


def _automation_off(session: Session, deps: AgentDeps, case: Case, row: CaseAction) -> Facts:
    """A confirmation that arrives with the automation switch on runs nothing (TRZ-35).

    The pending action is cancelled. A registration goes to a person with the action it would
    have run as the recommendation. The block offered after a registration is not run either:
    the dispute stays registered and the customer is sent to the bank's card block channel.
    """
    row.status, row.resolved_at = "canceled", utcnow()
    facts: Facts = {"intent": case.intent, "action": row.action, "actions_taken": []}
    if row.action == "block":
        write_audit(
            session,
            "agent",
            "automation_disabled",
            case.id,
            {"action_id": row.id, "pending_action": row.action},
            {"executed": False, "redirect": "card_block"},
        )
        facts["redirect"] = "card_block"
        facts["outcome"] = "block_not_run"
        return facts
    case.autonomy_level = deps.policy.config.action_level["escalate"]
    T.escalate_to_human(
        session, case.id, AUTOMATION_DISABLED_RULE, _sla(deps, "normal"), row.action
    )
    facts["outcome"] = "escalated"
    facts["handoff_reason"], facts["review_hours"] = AUTOMATION_DISABLED_RULE, _sla(deps, "normal")
    return facts


def _confirm_block(
    session: Session, deps: AgentDeps, case: Case, language: Language, row: CaseAction, said: Said
) -> Facts:
    """Blocks the card of a registered charge, the block offered after its registration.

    The dispute is already registered and verified: a block that fails or does not read back
    leaves it as it is, and the customer is told to block the card through the bank.
    """
    try:
        with session.begin_nested():
            block = T.block_card(
                session, case.customer_id, case.id, row.transaction_id, case.intent
            )
    except T.OwnershipError:
        return _security_stop(session, deps, case, language, said, "foreign_transaction_id")
    check = (
        verify_block(
            session,
            case.id,
            str(block.data.get("product_id", "")),
            str(block.data.get("status_before", "")),
        )
        if block.ok
        else None
    )
    if row.status == "pending":
        row.status, row.resolved_at = "executed", utcnow()
    facts: Facts = {"intent": case.intent, "action": "block", "actions_taken": []}
    if check is not None and check.verified:
        facts["actions_taken"] = ["block_card"]
        facts["outcome"] = "card_blocked"
        return facts
    facts["card_not_blocked"] = block.message or "block_not_verified"
    facts["redirect"] = "card_block"
    facts["outcome"] = "card_not_blocked"
    return facts


def _decline(session: Session, case: Case, action_id: str, said: Said) -> Facts:
    """Cancels the pending action the customer declines ("No, gracias"); nothing runs.

    Declining the registration closes the case with nothing registered. Declining the offered
    block leaves the registered dispute as it is and the card active.
    """
    row = session.execute(
        select(CaseAction).where(CaseAction.id == action_id).with_for_update()
    ).scalar_one_or_none()
    mine = row is not None and row.case_id == case.id
    pending = mine and row is not None and row.status == "pending"
    write_audit(
        session,
        "agent",
        "decline",
        case.id,
        said.audit(),
        {
            "action_id": action_id if mine else None,
            "pending_action": row.action if pending and row else None,
        },
    )
    if not pending or row is None:
        return {
            "intent": case.intent,
            "action": None,
            "outcome": "no_pending_action",
            "actions_taken": [],
        }
    row.status, row.resolved_at = "canceled", utcnow()
    if row.action == "block":
        return {
            "intent": case.intent,
            "action": "block",
            "outcome": "block_declined",
            "actions_taken": [],
        }
    case.status, case.recommended_action = "closed", None
    return {"intent": case.intent, "action": row.action, "outcome": "declined", "actions_taken": []}


def _pending_row(session: Session, case: Case) -> CaseAction | None:
    return session.execute(
        select(CaseAction).where(CaseAction.case_id == case.id, CaseAction.status == "pending")
    ).scalar_one_or_none()


def _case_on_charge(
    session: Session, customer_id: str, transaction_id: str, other_than: str | None = None
) -> Case | None:
    """The case that already holds a charge: one in progress, with a person or registered, or
    the case of an opened dispute on it."""
    held = session.execute(
        select(Case)
        .where(
            Case.customer_id == customer_id,
            Case.transaction_id == transaction_id,
            Case.status.in_(HOLDS_CHARGE),
            Case.id != (other_than or ""),
        )
        .order_by(Case.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if held is not None:
        return held
    dispute = session.execute(
        select(Dispute).where(
            Dispute.customer_id == customer_id,
            Dispute.transaction_id == transaction_id,
            Dispute.status == "opened",
            Dispute.case_id != (other_than or ""),
        )
    ).scalar_one_or_none()
    return session.get(Case, dispute.case_id) if dispute is not None else None


def _resume(
    session: Session,
    deps: AgentDeps,
    customer: Customer,
    case: Case,
    language: Language,
    said: Said,
) -> Facts:
    """Where the customer is taken back to on a case that already holds the charge: its
    recognition step, its pending question, or what became of it (registered, with a person)."""
    tx = session.get(Transaction, case.transaction_id) if case.transaction_id else None
    if case.status == "recognizing" and tx is not None:
        return _identified(session, deps, customer, case, language, candidate_of(tx), None, said)
    pending = _pending_row(session, case)
    if case.status == "awaiting_confirmation" and pending is not None and tx is not None:
        return {
            "intent": case.intent,
            "action": pending.action,
            "outcome": "awaiting_confirmation",
            "transaction": _charge_facts(
                session, candidate_of(tx), local_currency(customer.country_code)
            ),
            "pending_action": {"action_id": pending.id, "action": pending.action},
            "actions_taken": [],
        }
    dispute = session.execute(
        select(Dispute).where(Dispute.case_id == case.id, Dispute.status == "opened").limit(1)
    ).scalar_one_or_none()
    return {
        "intent": case.intent,
        "outcome": "existing_case",
        "existing_status": case.status,
        "dispute": {"folio": dispute.folio} if dispute is not None else None,
        "actions_taken": [],
    }


def pending_detail(session: Session, action_id: str) -> dict[str, Any]:
    """What the customer's screen needs to ask for a pending action, read from the database.

    It goes to the customer only, never to the LLM: it carries the card's last four digits.

    Args:
        session: Session bound to the customer of the JWT.
        action_id: The pending action.

    Returns:
        Merchant, amount, currency and last four digits of the card of its charge; empty when
        the action or its charge is not visible to the session.
    """
    row = session.get(CaseAction, action_id)
    tx = session.get(Transaction, row.transaction_id) if row is not None else None
    if tx is None:
        return {}
    product = session.get(Product, tx.product_id)
    return {
        "merchant": tx.merchant_name,
        "amount": tx.amount,
        "currency": tx.currency,
        "last4": product.product_number_last4 if product else None,
    }


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
    session: Session,
    deps: AgentDeps,
    customer: Customer,
    case: Case,
    redacted: str,
    facts: Facts,
    language: Language,
    budget: TurnBudget,
) -> tuple[str, LLMCallStats]:
    """The reply of the turn. The LLM writes it from the facts; code adds the deadline note and,
    for a case handed to a person, its number (TRZ-25 CA5); the fact checker decides whether the
    LLM text is sent or the fixed reply instead."""
    if facts["outcome"] in HANDOFF_OUTCOMES:
        facts["case_number"] = case.id
    notes = (deadline_note(facts, dict(deps.passages), language), handoff_note(facts, language))
    note = " ".join(n for n in notes if n)
    # The urgent card block redirect must say the same thing every time, a security stop must
    # not send the text of the turn to the LLM, and a failed read-back confirms nothing, so none
    # of them is written by it.
    if facts.get("redirect") == "card_block" or facts["outcome"] in FIXED_OUTCOMES:
        return _joined(template_reply(facts, language), note), LLMCallStats(fallback=True)
    if facts["outcome"] in CODE_WRITTEN_OUTCOMES:
        return _joined(template_reply(facts, language), note), LLMCallStats()
    # The screen asks for the confirmation with the amount, the merchant and its buttons: the
    # turn needs no text of its own.
    if facts["outcome"] == "awaiting_confirmation":
        return "", LLMCallStats()
    # A case with a person is answered the same way every time, with no LLM call.
    if facts["outcome"] == WITH_PERSON:
        return _joined(template_reply(facts, language), note), LLMCallStats()
    # The recognition step is written by code by design (TRZ-16), not as an LLM fallback. So is
    # the redirect of a request out of scope (TRZ-23): where to go is never left to the LLM.
    if facts["outcome"] == "recognizing":
        return recognition_text(facts["charge"], language), LLMCallStats()
    if facts["outcome"] == "abstained":
        return template_reply(facts, language), LLMCallStats()
    # So is the status of a claim (TRZ-22): with the LLM, replies described a status other than
    # the recorded one and promised news, which the fact checker cannot see.
    if facts.get("action") == "report_claim_status":
        return _joined(template_reply(facts, language), note), LLMCallStats()
    body, stats = deps.llm.compose(redacted, _reply_facts(facts), language, budget)
    text = _joined(body, note)
    if stats.fallback:
        return text, stats
    verified = verified_facts(
        session, customer.customer_id, facts, dict(deps.passages), deps.clock.today()
    )
    if check_reply(session, case.id, text, verified, deps.fact_check):
        return text, stats
    stats.fallback = True
    stats.error = "fact_check_blocked"
    return _joined(template_reply(facts, language), note), stats


def _joined(body: str, note: str) -> str:
    return f"{body} {note}" if note else body


def _reply_facts(facts: Facts) -> Facts:
    """What the reply is written from: what happened to the customer's case, never the policy
    (rules, thresholds or autonomy levels), which the LLM does not see (TRZ-17 CA7). Nor the
    deadline: code writes it with its citation after the LLM text."""
    tx = facts.get("transaction")
    shown = {k: v for k, v in (tx or {}).items() if k not in ("amount_usd", "transaction_id")}
    dispute = facts.get("dispute")
    return {
        "outcome": facts["outcome"],
        "action": facts.get("action"),
        "transaction": shown or None,
        "identification": facts.get("identification"),
        "options": facts.get("options", []),
        "actions_taken": facts["actions_taken"],
        "dispute": {"folio": dispute["folio"]} if dispute else None,
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

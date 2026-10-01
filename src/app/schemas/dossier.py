"""The dossier of a case handed to a person (design 12, TRZ-25).

Strict: no field outside design 12 and no transcript. Every fact and every piece of evidence
names the table and the id it was read from; clues carry their literal fragment and the
comprehension row of the message they were read in. Nothing in it is written by the LLM except
the translation of a Portuguese message, which is labeled automatic.
"""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(_Strict):
    """Where a fact was read.

    Attributes:
        table: Table of the record, such as transactions or complaints.
        id: Id of the record in that table.
    """

    table: str = Field(min_length=1)
    id: str = Field(min_length=1)


class Fact(_Strict):
    """A verified fact or a piece of evidence, with its source."""

    name: str
    value: Any
    source: Source


class Translation(_Strict):
    """The automatic translation of the original message into Spanish.

    Attributes:
        text: The translation, or None when the LLM failed.
        automatic: Always true: a model wrote it, nobody checked it.
        model: Model that translated.
        prompt_version: Version of the translation prompt.
        error: Why there is no translation, if there is none.
    """

    text: str | None
    automatic: Literal[True] = True
    model: str | None = None
    prompt_version: str | None = None
    error: str | None = None


class Clue(_Strict):
    """A clue of the customer with its literal fragment.

    Attributes:
        field: amount, date, merchant_hint, channel_hint or card_in_possession.
        value: What was read.
        evidence: Literal fragment of the message.
        read_in: Id of the comprehension row of the message it was read in.
    """

    field: str
    value: Any
    evidence: str
    read_in: int


class ScoredCandidate(_Strict):
    """A candidate charge with its probability and its score by component."""

    transaction_id: str
    probability: float
    components: dict[str, float]


class Identification(_Strict):
    """The identification of the charge: candidates, conformal set and scores."""

    decision: str
    candidates: int
    conformal_set: list[str]
    top: list[ScoredCandidate]


class ActionTaken(_Strict):
    """An action of the case and what became of it.

    Attributes:
        action: register, register_and_offer_block, register_and_block or block.
        state: verified (read back and matched), not_executed (never confirmed, replaced or
            canceled, or a block an analyst's approval leaves out), failed (read back and did
            not match), or not_verified (executed with no read-back row, only possible for rows
            older than TRZ-19).
        source: The case_actions row, or the audit row of the analyst's approval.
    """

    action: str
    state: Literal["verified", "not_executed", "failed", "not_verified"]
    source: Source


class OpenQuestion(_Strict):
    """Something the system could not confirm, from a closed list."""

    code: str
    text: str


class LaterMessage(_Strict):
    """A message the customer wrote once the case was with a person, or an answer to the
    analyst's question, redacted (TRZ-25, TRZ-28).

    Attributes:
        text: The message with PII replaced.
        source: The audit row that keeps it.
    """

    text: str
    source: Source


class InfoExchange(_Strict):
    """A question of the analyst to the customer and its answer, both PII-redacted (TRZ-28).

    Attributes:
        question: The question as the analyst wrote it.
        asked_by: User name of the analyst.
        asked_on: Simulated day it was asked.
        due_on: Last business day to answer.
        status: open, answered or expired.
        answer: The customer's answer; None while there is none.
        source: The info_requests row.
    """

    question: str
    asked_by: str
    asked_on: date
    due_on: date
    status: str
    answer: str | None
    source: Source


class RuleTriggered(_Strict):
    """The policy rule that handed the case over.

    Attributes:
        rule: Rule id, such as escalate.conformal_set_empty.
        version: Policy version.
        level: Display level of the action.
        autonomy_level: Autonomy level of the intent x language cell, when consulted.
    """

    rule: str
    version: str
    level: str
    autonomy_level: str | None


class AuditDraw(_Strict):
    """The draw that put a case the system resolved on its own in the audit sample (TRZ-29).

    Attributes:
        seed: Seed of the policy.
        n: Number of the draw; with the seed, enough to recompute it.
        rho: Sample rate of the policy.
        u: The number drawn; the case was selected because u < rho.
        source: The audit row of the draw.
    """

    seed: int
    n: int
    rho: float
    u: float
    source: Source


class Dossier(_Strict):
    """Everything the analyst needs to decide without asking again (design 12)."""

    case_id: str
    trace_id: str
    case_kind: Literal["escalation", "security_event", "audit_sample"]
    language: str | None
    original_message: str | None
    machine_translation: Translation | None
    request_summary: str
    verified_facts: list[Fact]
    extraction: list[Clue]
    identification: Identification | None
    actions_taken: list[ActionTaken]
    evidence: list[Fact]
    open_questions: list[OpenQuestion]
    policy_rule_triggered: RuleTriggered | None
    recommended_action: str | None
    later_messages: list[LaterMessage]
    info_exchanges: list[InfoExchange]
    audit_draw: AuditDraw | None = None
    # Created by the demo state, not by a customer (TRZ-38).
    simulated: bool = False

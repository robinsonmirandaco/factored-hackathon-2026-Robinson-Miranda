"""Free agent: the system baseline of the evaluation (TRZ-44, design 13.1).

One LLM call loop with a long prompt and every tool by tool calling. It has the same model as
TRAZO, the full text of the policy in its prompt (CA1), and the same tools of
`app.services.tools` under the same access control: a database session bound to the customer of
the logged-in session, row level security included, and a session check before every turn
(CA2). It has no conformal identification, no fact checker, no autonomy watch and no replies
written by code (CA3). The harness runs it with the same simulated client and cases as TRAZO
(CA4); its prompt is versioned in config/prompts/free_agent.yaml (CA5).

Equal conditions the comparison keeps: the customer's message is redacted before the LLM, as in
TRAZO; each request has the timeout of TRAZO's LLM client and one retry per customer turn; when
the LLM fails the case goes to a person, the safe fallback. The model never sees a dataset id: it
handles charges and claims by short handles (T1, C1) the harness maps back.
"""

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.adapters.db.models import Case, Customer
from app.adapters.db.rates import rates_near
from app.adapters.db.session import SchemaUrls
from app.core.errors import AppError
from app.core.logging import new_trace_id, trace_id_var
from app.core.time import utcnow
from app.domain.fx import local_currency, to_usd
from app.domain.language import decide as decide_language
from app.domain.pii import redact
from app.domain.policy_passages import policy_deadline
from app.main import create_app
from app.services import auth
from app.services import tools as T
from app.services.identification import load_candidates
from app.services.replies import check_reply, verified_facts
from pipeline.cases.schema import CaseRecord
from pipeline.simulated_client import ClientTurn, SystemTurn

PROMPT_PATH = Path("config/prompts/free_agent.yaml")
# Model requests in one customer turn; a loop that needs more ends the turn.
MAX_CALLS_PER_TURN = 6
MAX_TOKENS = 600
TEMPERATURE = 0.0
UI_TOOLS = ("ask_customer", "show_options", "show_charge", "request_confirmation")
CONFIRMS = {
    "register_and_block": ("register_dispute", "block_card"),
    "register_dispute": ("register_dispute",),
    "block_card": ("block_card",),
}
HANDOFF_STATUS = {
    "escalate": "escalated",
    "analyst_approval": "pending_analyst_approval",
    "security": "security_blocked",
}
FALLBACK_REPLY = {
    "es": "Tu caso pasó a una persona del equipo, que lo revisará.",
    "pt": "Seu caso foi encaminhado para uma pessoa da equipe, que vai analisá-lo.",
}


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_customer_profile",
        "description": "Segment, country and status of the customer, and whether a dispute of "
        "theirs is still open from the look-back window of the policy.",
        "input_schema": _schema({}, []),
    },
    {
        "name": "list_transactions",
        "description": "The customer's disputable charges (purchases, payments, withdrawals; "
        "approved or pending) of the dispute window, oldest first, each with a handle such as "
        "T1, its amount in USD at the rate of its date (null when it cannot be converted), "
        "merchant, channel, type and status.",
        "input_schema": _schema({}, []),
    },
    {
        "name": "get_open_claims",
        "description": "The customer's open claims, each with a handle such as C1, its status, "
        "opening date and backed deadline, if any.",
        "input_schema": _schema({}, []),
    },
    {
        "name": "ask_customer",
        "description": "Asks the customer for one more detail of the charge. Returns their answer.",
        "input_schema": _schema(
            {"question": {"type": "string", "description": "The question, in their language."}},
            ["question"],
        ),
    },
    {
        "name": "show_options",
        "description": "Shows up to 3 charges or claims as buttons. Returns the handle the "
        "customer chose, or none.",
        "input_schema": _schema(
            {"handles": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
            ["handles"],
        ),
    },
    {
        "name": "show_charge",
        "description": "Shows the detail of one charge and asks whether the customer recognizes "
        "it. Returns their answer.",
        "input_schema": _schema({"handle": {"type": "string"}}, ["handle"]),
    },
    {
        "name": "request_confirmation",
        "description": "Asks the customer to confirm one action on one charge before it runs. "
        "Returns their answer.",
        "input_schema": _schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["register_and_block", "register_dispute", "block_card"],
                },
                "handle": {"type": "string"},
            },
            ["action", "handle"],
        ),
    },
    {
        "name": "register_dispute",
        "description": "Registers a dispute for one charge. Returns the folio.",
        "input_schema": _schema(
            {
                "handle": {"type": "string"},
                "dispute_type": {
                    "type": "string",
                    "enum": [
                        "unrecognized_charge",
                        "billing_error_amount",
                        "billing_error_duplicate",
                    ],
                },
            },
            ["handle", "dispute_type"],
        ),
    },
    {
        "name": "block_card",
        "description": "Blocks the card a charge was made with.",
        "input_schema": _schema(
            {"handle": {"type": "string"}, "reason": {"type": "string"}}, ["handle", "reason"]
        ),
    },
    {
        "name": "escalate_to_human",
        "description": "Hands the case to a person. kind: escalate, analyst_approval (an action "
        "that needs approval) or security (a security event). priority: normal, high or urgent.",
        "input_schema": _schema(
            {
                "kind": {"type": "string", "enum": list(HANDOFF_STATUS)},
                "reason": {"type": "string"},
                "priority": {"type": "string", "enum": ["normal", "high", "urgent"]},
                "recommended_action": {"type": "string"},
            },
            ["kind", "reason"],
        ),
    },
]


@dataclass(frozen=True)
class FreeAgentPrompt:
    """The versioned prompt with the policy inserted.

    Attributes:
        version: Version of the prompt file.
        system: System prompt sent to the model.
    """

    version: str
    system: str


def load_prompt(policy: Any, path: Path = PROMPT_PATH) -> FreeAgentPrompt:
    """Reads the prompt and inserts the policy, verbatim, and its values.

    Args:
        policy: Loaded policy config (config/policy.yaml).
        path: Prompt file.

    Returns:
        The prompt.
    """
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    values = {
        "policy": Path("config/policy.yaml").read_text(encoding="utf-8").strip(),
        "passages": Path("config/policy_passages.yaml").read_text(encoding="utf-8").strip(),
        "dispute_window_days": policy.dispute_window_days,
        "max_clarifications": policy.max_clarifications,
        "human_review_above": policy.amount_usd.human_review_above,
        "auto_register_max": policy.amount_usd.auto_register_max,
        "open_dispute_lookback_days": policy.open_dispute_lookback_days,
    }
    system = spec["system"]
    for key, value in values.items():
        system = system.replace("{" + key + "}", str(value))
    return FreeAgentPrompt(spec["version"], system)


@dataclass
class _Pending:
    """A screen the customer has to answer, with the tool results that wait for it."""

    tool_use_id: str
    name: str
    tool_input: dict[str, Any]
    ready: list[dict[str, Any]] = field(default_factory=list)


class FreeAgentConversation:
    """The free agent serving one case."""

    def __init__(
        self,
        client: TestClient,
        token: str,
        sdk: anthropic.Anthropic,
        prompt: FreeAgentPrompt,
    ) -> None:
        """Keeps the app (for its database, policy and clock), the session token and the model.

        Args:
            client: Test client of the app wired to the case schema.
            token: Bearer token of the customer's session.
            sdk: Anthropic client over the harness transport.
            prompt: Prompt with the policy.
        """
        self.runtime = client.app.state.runtime  # type: ignore[attr-defined]
        self.settings = self.runtime.settings
        self.deps = self.runtime.agent
        self.token = token
        self.sdk = sdk
        self.prompt = prompt
        self.model = self.settings.llm_model_primary
        self.messages: list[dict[str, Any]] = []
        self.case_id: str | None = None
        self.language = "es"
        self.handles: dict[str, str] = {}
        self.pending: _Pending | None = None
        self.confirmed: set[tuple[str, str]] = set()
        self.violations: list[str] = []
        self.asked_in_text = False
        # What the tools returned in this conversation: the facts a reply may state.
        self.records: dict[str, dict[str, Any]] = {}
        self.disputed: str | None = None
        self.dispute: dict[str, Any] = {}
        self.actions: list[str] = []

    # ---- one customer turn ----------------------------------------------------------------

    def send(self, turn: ClientTurn) -> tuple[SystemTurn, str | None, str | None]:
        """Runs the agent on one customer turn.

        Args:
            turn: The customer turn.

        Returns:
            What the agent asks next, the outcome of the turn and the reply text.
        """
        # Each turn has its own trace id, as a request to the API gets from the middleware.
        trace_id_var.set(new_trace_id())
        try:
            principal = auth.authenticate(self.runtime.db, self.settings, self.token, utcnow())
        except AppError:
            return SystemTurn("done"), "session_ended", None
        customer_id = principal.subject
        with self.runtime.db.session(customer_id=customer_id) as s:
            customer = s.get(Customer, customer_id)
            assert customer is not None
            redacted, _ = redact(turn.message, name=customer.first_name)
            if self.case_id is None:
                self._open_case(s, customer, redacted)
                auth.remember_case(s, principal, self.case_id)  # type: ignore[arg-type]
            self.messages.append({"role": "user", "content": self._user_content(turn, redacted)})
            return self._loop(s, customer)

    def _open_case(self, s: Session, customer: Customer, redacted: str) -> None:
        self.language = decide_language(redacted, None, None, customer.country_code).language
        case = Case(
            id=f"CASE-{uuid.uuid4().hex[:10].upper()}",
            customer_id=customer.customer_id,
            intent="free_agent",
            language=self.language,
            status="open",
            trace_id=trace_id_var.get(),
        )
        s.add(case)
        s.flush()
        self.case_id = case.id

    def _user_content(self, turn: ClientTurn, redacted: str) -> Any:
        if self.pending is None:
            return redacted
        p, self.pending = self.pending, None
        if p.name == "show_options":
            back = {v: k for k, v in self.handles.items()}
            chosen = back.get(turn.option or "", "none")
            answer = f"The customer chose {chosen}."
        elif p.name == "show_charge":
            answer = f"The customer answered: {redacted}"
        elif p.name == "request_confirmation":
            if turn.confirm_action_id:
                action, handle = p.tool_input["action"], p.tool_input["handle"]
                # One confirmation of register_and_block covers both actions, as in TRAZO.
                for done in CONFIRMS[action]:
                    self.confirmed.add((done, handle))
            answer = f"The customer answered: {redacted}"
        else:
            answer = redacted
        return [*p.ready, {"type": "tool_result", "tool_use_id": p.tool_use_id, "content": answer}]

    def _loop(self, s: Session, customer: Customer) -> tuple[SystemTurn, str | None, str | None]:
        retries = self.settings.llm_max_retries
        for _ in range(MAX_CALLS_PER_TURN):
            try:
                msg = self._create()
            except Exception as exc:
                if _transient(exc) and retries > 0:
                    retries -= 1
                    time.sleep(self.settings.llm_retry_wait_seconds)
                    continue
                return self._fallback(s, type(exc).__name__)
            blocks = [b.model_dump(mode="json", exclude_none=True) for b in msg.content]
            self.messages.append({"role": "assistant", "content": blocks})
            uses = [b for b in msg.content if b.type == "tool_use"]
            if not uses:
                reply = "".join(b.text for b in msg.content if b.type == "text")
                self._observe(s, customer, reply)
                # A question in plain text: the client answers with every clue it remembers,
                # once (a choice that favors the baseline, declared in the report).
                if "?" in reply and not self.asked_in_text:
                    self.asked_in_text = True
                    return SystemTurn("text_question"), "text_question", reply
                return SystemTurn("done"), "reply", reply
            results: list[dict[str, Any]] = []
            screen: tuple[Any, SystemTurn] | None = None
            for use in uses:
                invalid = _invalid_screen(use.name, use.input)
                if use.name in UI_TOOLS and screen is None and invalid is None:
                    screen = (use, self._screen(use.name, use.input))
                    continue
                if use.name in UI_TOOLS:
                    # A screen outside its schema is not shown: the model gets the error back.
                    content = {"error": invalid or "one screen at a time"}
                else:
                    content = self._run_tool(s, customer, use.name, use.input)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": use.id,
                        "content": json.dumps(content, ensure_ascii=False, default=str),
                    }
                )
            if screen is not None:
                use, asked = screen
                self.pending = _Pending(use.id, use.name, dict(use.input), results)
                return asked, use.name, None
            self.messages.append({"role": "user", "content": results})
        self.violations.append("tool_loop_limit")
        return SystemTurn("done"), "tool_loop_limit", None

    def _create(self) -> Any:
        return self.sdk.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": self.prompt.system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=TOOLS,
            messages=self.messages,
            # Caches the conversation so far, so each request of a turn re-reads it cheaply.
            cache_control={"type": "ephemeral"},
            extra_body={"temperature": TEMPERATURE},
        )

    def _fallback(self, s: Session, error: str) -> tuple[SystemTurn, str | None, str | None]:
        self._handoff(s, "escalate", f"llm_unavailable: {error}", None)
        self.violations.append("llm_fallback")
        return SystemTurn("done"), "llm_fallback", FALLBACK_REPLY[self.language]

    # ---- screens and tools --------------------------------------------------------------

    def _screen(self, name: str, tool_input: dict[str, Any]) -> SystemTurn:
        if name == "show_options":
            ids = [self.handles[h] for h in tool_input.get("handles", []) if h in self.handles]
            return SystemTurn("show_options", options=tuple(ids))
        if name == "show_charge":
            return SystemTurn("recognition")
        if name == "request_confirmation":
            return SystemTurn("confirm", action_id=f"FA-{len(self.confirmed) + 1}")
        return SystemTurn("ask_detail")

    def _run_tool(
        self, s: Session, customer: Customer, name: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        cid, case_id = customer.customer_id, self.case_id
        assert case_id is not None
        policy, clock = self.deps.policy.config, self.deps.clock
        if name == "get_customer_profile":
            data = T.get_customer_profile(
                s, clock, cid, policy.open_dispute_lookback_days, case_id
            ).data
            return {k: v for k, v in data.items() if k not in ("customer_id", "open_dispute_ids")}
        if name == "list_transactions":
            return {"transactions": self._transactions(s, customer)}
        if name == "get_open_claims":
            claims = T.get_open_claims(s, clock, self._deadline(customer), cid, case_id).data
            out = []
            for i, c in enumerate(claims["claims"], start=1):
                self.handles[f"C{i}"] = c["claim_id"]
                out.append({"handle": f"C{i}", **{k: v for k, v in c.items() if k != "claim_id"}})
            return {"claims": out}
        transaction_id = self.handles.get(str(tool_input.get("handle", "")))
        if name in ("register_dispute", "block_card") and transaction_id is None:
            return {"ok": False, "error": "unknown handle"}
        if name == "register_dispute":
            assert transaction_id is not None
            self._check_confirmed("register_dispute", tool_input["handle"])
            r = T.register_dispute(
                s,
                clock,
                self._deadline(customer),
                cid,
                case_id,
                transaction_id,
                tool_input["dispute_type"],
            )
            if r.ok:
                self.disputed = tool_input["handle"]
                self.actions.append("register_dispute")
                self.dispute = {
                    "folio": r.data.get("folio"),
                    "registered_on": clock.today().isoformat(),
                    "due_date": r.data.get("due_date"),
                    "passage": r.data.get("due_date_passage"),
                }
            return {"ok": r.ok, "folio": r.data.get("folio"), "message": r.message}
        if name == "block_card":
            assert transaction_id is not None
            self._check_confirmed("block_card", tool_input["handle"])
            r = T.block_card(s, cid, case_id, transaction_id, tool_input.get("reason", ""))
            if r.ok:
                self.actions.append("block_card")
            return {"ok": r.ok, "message": r.message}
        if name == "escalate_to_human":
            return self._handoff(
                s,
                tool_input["kind"],
                tool_input["reason"],
                tool_input.get("recommended_action"),
                tool_input.get("priority", "normal"),
            )
        return {"ok": False, "error": f"unknown tool {name}"}

    def _transactions(self, s: Session, customer: Customer) -> list[dict[str, Any]]:
        policy, clock = self.deps.policy.config, self.deps.clock
        local = local_currency(customer.country_code)
        out = []
        for i, c in enumerate(
            load_candidates(s, customer.customer_id, clock, policy.dispute_window_days), start=1
        ):
            handle = f"T{i}"
            self.handles[handle] = c.transaction_id
            on = c.timestamp.date()
            rates = rates_near(s, on, {(c.currency, "USD"), (c.currency, local)})
            self.records[handle] = {
                "transaction_id": c.transaction_id,
                "amount": c.amount,
                "date": on.isoformat(),
                "merchant": c.merchant_name,
            }
            out.append(
                {
                    "handle": handle,
                    "date": c.timestamp.isoformat(timespec="minutes"),
                    "merchant": c.merchant_name,
                    "amount": c.amount,
                    "currency": c.currency,
                    "amount_usd": to_usd(c.amount, c.currency, on, rates),
                    "channel": c.channel,
                    "type": c.transaction_type,
                    "status": c.status,
                }
            )
        return out

    def _observe(self, s: Session, customer: Customer, reply: str) -> None:
        """Runs TRAZO's fact checker on a reply, as an observer: the reply is sent anyway.

        The free agent has no verifier (CA3); the check only measures its unsupported claims
        (design 13.3), against the facts its own tools returned in the conversation.
        """
        assert self.case_id is not None
        facts = {
            "options": [r for h, r in self.records.items() if h != self.disputed],
            "transaction": self.records.get(self.disputed or ""),
            "dispute": self.dispute if self.dispute.get("folio") else {},
            "case_number": self.case_id,
            "actions_taken": self.actions,
        }
        verified = verified_facts(
            s, customer.customer_id, facts, dict(self.deps.passages), self.deps.clock.today()
        )
        check_reply(s, self.case_id, reply, verified, enforce=False)

    def _deadline(self, customer: Customer) -> Any:
        """The response deadline of the policy passages, as TRAZO computes it."""

        def due(start: Any) -> Any:
            return policy_deadline(
                self.deps.passages,
                self.deps.calendars,
                "response_deadline",
                start,
                customer.country_code,
                self.language,  # type: ignore[arg-type]
            )

        return due

    def _check_confirmed(self, action: str, handle: str) -> None:
        if (action, handle) not in self.confirmed:
            self.violations.append(f"{action}_without_confirmation")

    def _handoff(
        self,
        s: Session,
        kind: str,
        reason: str,
        recommended: str | None,
        priority: str = "normal",
    ) -> dict[str, Any]:
        assert self.case_id is not None
        status = HANDOFF_STATUS.get(kind, "escalated")
        sla = self.deps.policy.config.queue.sla_hours[priority]
        r = T.escalate_to_human(
            s,
            self.case_id,
            reason,
            sla,
            recommended,
            status,  # type: ignore[arg-type]
            priority,
        )
        return {"ok": r.ok, "message": r.message}


def _invalid_screen(name: str, tool_input: dict[str, Any]) -> str | None:
    """Why a screen call cannot be shown, or None when it can."""
    if name == "request_confirmation" and tool_input.get("action") not in CONFIRMS:
        return f"action must be one of {', '.join(CONFIRMS)}"
    return None


def _transient(exc: Exception) -> bool:
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return isinstance(exc, anthropic.APIConnectionError | anthropic.APITimeoutError)


@contextmanager
def free_agent_conversation(urls: SchemaUrls, staging: Any, case: CaseRecord) -> Iterator[Any]:
    """The free agent wired to the schema of the case, with the case "now" and the run's LLM.

    The app is built only for its database, policy, clock and login; the agent does not use its
    chat endpoint.
    """
    settings = staging.settings.model_copy(
        update={
            "database_url": urls.app,
            "admin_database_url": urls.admin,
            "trazo_now": case.now,
            "demo_mode": True,
            "llm_warm_up": False,
        }
    )
    if staging.transport is None:
        raise ValueError("the free agent needs the LLM")
    sdk = anthropic.Anthropic(
        api_key=settings.anthropic_api_key or "unused",
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
        http_client=httpx2.Client(transport=staging.transport),
    )
    app = create_app(settings)
    prompt = load_prompt(app.state.runtime.agent.policy.config)
    with TestClient(app) as client:
        document = staging.data.document(case.customer_id)
        client.post("/auth/otp/request", json=document).raise_for_status()
        r = client.post("/auth/otp/verify", json={**document, "code": settings.demo_otp_code})
        r.raise_for_status()
        yield FreeAgentConversation(client, r.json()["access_token"], sdk, prompt)

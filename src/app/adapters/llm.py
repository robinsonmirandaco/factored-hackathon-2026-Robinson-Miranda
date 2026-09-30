"""LLM client and its deterministic fallbacks.

Claude through the Anthropic SDK. Each attempt has a 5 s deadline; the LLM is retried at most
once per customer turn, across all its calls, and a turn whose LLM failed with its retry spent
makes no further call (TRZ-36). A warm-up call at startup pays the cost of a cold call.

Calls:
  comprehend(text, ctx)    -> Comprehension of design 6.1; the rules baseline is its fallback
  compose(...)             -> customer reply; the deterministic fact checker of TRZ-20 decides
                              whether it is sent
  translate(text)          -> Spanish translation of a Portuguese message for the analyst

Every call is logged with model, prompt version, tokens, latency and cost (TRZ-12 CA6). Every
failure returns a typed fallback. Callers never see an exception from this module.
"""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from anthropic import transform_schema
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.clock import SimulatedClock
from app.domain.comprehension_rules import comprehend_rules
from app.schemas.comprehension import (
    Comprehension,
    ComprehensionContext,
    ComprehensionReading,
    DateClue,
    DateReading,
    evidence_is_faithful,
)

log = get_logger("llm")

# JSON schema the API constrains the comprehension output to; Pydantic still validates it.
READING_SCHEMA = transform_schema(ComprehensionReading)


@dataclass
class LLMCallStats:
    """Usage and outcome of one or more LLM calls.

    Attributes:
        input_tokens: Prompt tokens read without the cache.
        output_tokens: Completion tokens.
        cache_write_tokens: Prompt tokens written to the provider's prompt cache.
        cache_read_tokens: Prompt tokens read from that cache.
        latency_ms: Wall time spent in the calls.
        cost_usd: Cost of the tokens at the configured prices.
        calls: Requests made, retries included.
        model: Model id.
        prompt_version: Version of the prompt file, for versioned prompts.
        fallback: True when the deterministic path produced the result.
        error: Short reason for the fallback, if any.
        dropped_clues: Clues discarded because their evidence is not in the message.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    model: str = ""
    prompt_version: str | None = None
    fallback: bool = False
    error: str | None = None
    dropped_clues: list[str] = field(default_factory=list)

    def add(self, other: "LLMCallStats") -> None:
        """Accumulates usage from another call into this one.

        Args:
            other: Stats of a follow-up call.
        """
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.latency_ms += other.latency_ms
        self.cost_usd += other.cost_usd
        self.calls += other.calls
        self.model = other.model or self.model
        # A step that used two prompts (compose, then validate) cites both.
        if other.prompt_version and other.prompt_version != self.prompt_version:
            versions = [v for v in (self.prompt_version, other.prompt_version) if v]
            self.prompt_version = "+".join(versions)


@dataclass(frozen=True)
class ComprehensionPrompt:
    """The versioned prompt of the comprehension step.

    Attributes:
        version: Version string of the file.
        system: System prompt with the examples rendered in.
        example_sources: Development case id behind each example.
    """

    version: str
    system: str
    example_sources: tuple[str, ...]


def prompt_version_of(system: str) -> str:
    """Names an unversioned system prompt by its content, so any edit changes the name.

    Args:
        system: System prompt text.

    Returns:
        "sha256:" followed by the first 12 hex digits of the prompt's hash.
    """
    return "sha256:" + hashlib.sha256(system.encode()).hexdigest()[:12]


def comprehension_user(message: str, country_code: str, local_currency: str) -> str:
    """Renders the user turn of a comprehension call, for real messages and examples alike.

    Args:
        message: Redacted customer message.
        country_code: Home country of the customer.
        local_currency: Currency of that country.

    Returns:
        The user turn.
    """
    return f"Customer country: {country_code}\nLocal currency: {local_currency}\nMessage: {message}"


def load_comprehension_prompt(path: Path) -> ComprehensionPrompt:
    """Reads the prompt file and renders its examples into the system prompt.

    Every example output must validate against the output contract and cite only fragments of
    its own message, so the prompt never teaches an unfaithful clue.

    Args:
        path: config/prompts/comprehension.yaml.

    Returns:
        The prompt.

    Raises:
        ValueError: When an example breaks the contract or cites text not in its message.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    parts = [data["system"].rstrip(), "", "Examples. Each is a synthetic message and its output."]
    for example in data["examples"]:
        reading = ComprehensionReading.model_validate(example["output"])
        clues = [reading.amount, reading.date, reading.merchant_hint]
        clues += [reading.channel_hint, reading.card_in_possession]
        for clue in clues:
            if clue is not None and not evidence_is_faithful(clue.evidence, example["message"]):
                raise ValueError(f"example {example['derived_from']}: evidence not in message")
        user = comprehension_user(example["message"], example["country"], example["local_currency"])
        output = json.dumps(reading.model_dump(mode="json"), ensure_ascii=False)
        parts += ["", user, f"Output: {output}"]
    return ComprehensionPrompt(
        version=str(data["version"]),
        system="\n".join(parts),
        example_sources=tuple(str(e["derived_from"]) for e in data["examples"]),
    )


def resolve_reading(reading: ComprehensionReading, context: ComprehensionContext) -> Comprehension:
    """Turns the LLM reading into a Comprehension, resolving the date with the simulated clock.

    A date the clock cannot place (a calendar day after today, a relative key without a count)
    is dropped and logged.

    Args:
        reading: Validated LLM output.
        context: Simulated "now" of the case.

    Returns:
        The comprehension, before the faithfulness check.
    """
    fields = reading.model_dump(exclude={"date"})
    fields["date"] = None
    if reading.date is not None:
        fields["date"] = _resolve_date(reading.date, SimulatedClock(context.now))
        if fields["date"] is None:
            log.warning("llm_date_unresolved", kind=reading.date.kind)
    return Comprehension.model_validate(fields)


def _resolve_date(reading: DateReading, clock: SimulatedClock) -> DateClue | None:
    today = clock.today()
    if reading.kind == "calendar":
        if reading.day is None or reading.month is None:
            return None
        day = clock.resolve_calendar_date(reading.day, reading.month, reading.year)
        window = None if day is None else ((today - day).days, (today - day).days)
    elif reading.kind in {"days_ago", "weeks_ago", "months_ago"}:
        count = reading.count
        window = None if count is None else clock.relative_window(reading.kind, count)
    else:
        window = clock.relative_window(reading.kind)
    if window is None:
        return None
    return DateClue(
        expression=reading.expression,
        resolved_from=today,
        window_days=window,
        evidence=reading.evidence,
    )


COMPOSE_SYSTEM = """You are a bank customer service assistant. Reply to the customer in their
language, in 2 to 4 short sentences, plain and warm. Use only the facts you are given: write no
amount, date, folio, duration, card digits or merchant that is not in them, and state as done
only the actions listed in actions_taken. Never promise refunds or cancellations. Never ask for
or mention passwords, PINs, security codes, card numbers or identity documents. Do not state
response deadlines nor cite policy: they are added after your text. Never promise to contact
the customer or to send news. Do not mention internal scores, rules or system names. Speak for
the bank in the first person plural (nosotros in Spanish, nós in Portuguese), and call the
customer's request an "aclaración" in Spanish or a "contestação" in Portuguese."""


TRANSLATE_SYSTEM = """Translate the customer's message from Portuguese into Spanish for a bank
analyst. Keep every placeholder in brackets, such as [NAME] or [CARD], exactly as it is. Add
nothing: no explanation, no greeting, no note. Output only the translation."""


@dataclass
class TurnBudget:
    """How much the LLM may still be retried in one customer turn (TRZ-36 CA3).

    Comprehension, reply and translation share it: the LLM is retried at most once per turn,
    and once it has failed with its retry spent, no further call is made in that turn.

    Attributes:
        retries_left: Retries still allowed in the turn.
        down: The LLM failed with no retry left; the rest of the turn uses the fallbacks.
    """

    retries_left: int = 1
    down: bool = False


# A synthetic message for the warm-up call, written for it: no evaluation case repeats it.
WARM_UP_MESSAGE = "Hola, quiero revisar un movimiento de mi tarjeta."
WARM_UP_TIMEOUT_SECONDS = 15.0


class LLMClient:
    """Claude through the Anthropic SDK, with a timeout per attempt, one retry per turn and
    deterministic fallbacks."""

    provider = "anthropic"

    def __init__(self, settings: Settings, http_client: Any = None) -> None:
        """Creates the SDK client when the LLM is enabled and has a key.

        Args:
            settings: Application settings (model, timeout, retries, key, prices).
            http_client: HTTP client of the SDK's own httpx fork (httpx2); tests pass one with a
                simulated transport.
        """
        self.model = settings.llm_model_primary
        self.comprehension_prompt = load_comprehension_prompt(
            settings.llm_comprehension_prompt_path
        )
        self._price_in = settings.llm_price_input_per_mtok
        self._price_out = settings.llm_price_output_per_mtok
        self._max_retries = settings.llm_max_retries
        self._timeout = settings.llm_timeout_seconds
        self._retry_wait = settings.llm_retry_wait_seconds
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")
        self._client: Any = None
        if not settings.llm_enabled or not settings.anthropic_api_key:
            return
        try:
            import anthropic

            # The SDK does not retry: this client does, so one retry per turn is countable.
            self._client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key,
                timeout=settings.llm_timeout_seconds,
                max_retries=0,
                http_client=http_client,
            )
        except Exception as exc:  # pragma: no cover
            log.error("llm_init_failed", error=type(exc).__name__)

    @property
    def available(self) -> bool:
        """True when real LLM calls can be made."""
        return self._client is not None

    def new_turn(self) -> TurnBudget:
        """The retry budget of one customer turn.

        Returns:
            A budget with the configured retries.
        """
        return TurnBudget(retries_left=self._max_retries)

    def _call(
        self,
        system: str,
        user: str,
        max_tokens: int = 400,
        temperature: float | None = None,
        *,
        schema: dict[str, Any] | None = None,
        prompt_version: str | None = None,
        budget: TurnBudget | None = None,
        timeout: float | None = None,
    ) -> tuple[str, LLMCallStats]:
        """One LLM request, retried once within the turn's budget when the failure is transient.

        Retried: a timeout, a connection error, 429 and 5xx (529 overloaded included). Not
        retried: any other 4xx, which another attempt cannot fix, and a 429 whose Retry-After
        is longer than the wait allowed. Each attempt has its own wall-clock deadline, because
        httpx timeouts bound each network phase and a slow server could hold the turn longer.
        """
        budget = budget or self.new_turn()
        stats = LLMCallStats(
            model=self.model, prompt_version=prompt_version or prompt_version_of(system)
        )
        if not self.available or budget.down:
            stats.fallback = True
            stats.error = "llm_disabled" if not self.available else "llm_down_this_turn"
            return "", stats
        t0 = time.perf_counter()
        text = ""
        while True:
            stats.calls += 1
            try:
                future = self._pool.submit(
                    self._complete, system, user, max_tokens, temperature, schema
                )
                text, usage = future.result(timeout=timeout or self._timeout)
                stats.input_tokens, stats.output_tokens = usage[0], usage[1]
                stats.cache_write_tokens, stats.cache_read_tokens = usage[2], usage[3]
                stats.error = None
                break
            except Exception as exc:
                stats.error = type(exc).__name__
                log.warning("llm_call_failed", error=stats.error, attempt=stats.calls)
                if _retryable(exc, self._retry_wait) and budget.retries_left > 0:
                    budget.retries_left -= 1
                    time.sleep(self._retry_wait)
                    continue
                budget.down = True
                stats.fallback = True
                text = ""
                break
        stats.latency_ms = int((time.perf_counter() - t0) * 1000)
        # Anthropic list prices: a cache write costs 1.25 times an input token, a read 0.1 times.
        billed_input = (
            stats.input_tokens + 1.25 * stats.cache_write_tokens + 0.1 * stats.cache_read_tokens
        )
        billed_output = stats.output_tokens * self._price_out
        stats.cost_usd = (billed_input * self._price_in + billed_output) / 1e6
        log.info(
            "llm_call",
            provider=self.provider,
            model=stats.model,
            prompt_version=stats.prompt_version,
            input_tokens=stats.input_tokens,
            output_tokens=stats.output_tokens,
            cache_write_tokens=stats.cache_write_tokens,
            cache_read_tokens=stats.cache_read_tokens,
            latency_ms=stats.latency_ms,
            cost_usd=round(stats.cost_usd, 6),
            attempts=stats.calls,
            failed=stats.fallback,
        )
        return text, stats

    def _complete(
        self,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float | None,
        schema: dict[str, Any] | None,
    ) -> tuple[str, tuple[int, int, int, int]]:
        # SDK 1.x dropped `temperature` from its signature; models before Opus 4.7, such as
        # Haiku 4.5, still accept it in the request body.
        extra: dict[str, Any] = {}
        if temperature is not None:
            extra["extra_body"] = {"temperature": temperature}
        if schema is not None:
            extra["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        msg = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            # A system prompt below the model's cache minimum is simply not cached.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            **extra,
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        u = msg.usage
        cached = (u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0)
        return text, (u.input_tokens, u.output_tokens, *cached)

    def warm_up(self) -> None:
        """One comprehension call with a fixed synthetic message, run at startup (TRZ-36).

        A cold call with structured output took 7.5 s, over the 5 s timeout, so the first
        customer turn after a deploy would fall to the rules. This call pays that cost instead,
        with a longer deadline. The message is written for it and repeats no evaluation case.
        """
        context = ComprehensionContext(
            now=datetime(2026, 6, 17, 12, 0), country_code="MX", local_currency="MXN"
        )
        user = comprehension_user(WARM_UP_MESSAGE, context.country_code, context.local_currency)
        _, stats = self._call(
            self.comprehension_prompt.system,
            user,
            max_tokens=600,
            temperature=0.0,
            schema=READING_SCHEMA,
            prompt_version=self.comprehension_prompt.version,
            budget=TurnBudget(retries_left=0),
            timeout=WARM_UP_TIMEOUT_SECONDS,
        )
        log.info(
            "llm_warm_up", ok=not stats.fallback, latency_ms=stats.latency_ms, error=stats.error
        )

    def complete(
        self, system: str, user: str, max_tokens: int, temperature: float
    ) -> tuple[str, LLMCallStats]:
        """Free-form completion for offline tools such as the case generator (TRZ-42).

        Same timeout, bounded retry and fallback as the other calls: on failure the text is
        empty and `stats.fallback` is set.

        Args:
            system: System prompt.
            user: User message.
            max_tokens: Output cap.
            temperature: Sampling temperature.

        Returns:
            The text and the call stats.
        """
        return self._call(system, user, max_tokens, temperature)

    def read_clues(
        self,
        redacted_text: str,
        context: ComprehensionContext,
        budget: TurnBudget | None = None,
    ) -> tuple[Comprehension | None, LLMCallStats]:
        """Reads intent and clues with the LLM, before the faithfulness check.

        The date is resolved with the simulated clock. The evaluation harness scores this
        reading, so unfaithful clues count against the model.

        Args:
            redacted_text: Customer message with PII already replaced.
            context: Simulated "now" and the customer's country and currency.
            budget: Retry budget of the turn; a fresh one when omitted.

        Returns:
            The comprehension, or None when the LLM failed or answered invalid output twice,
            and the call stats.
        """
        reading, stats = self.read_raw(redacted_text, context, budget)
        return (None if reading is None else resolve_reading(reading, context)), stats

    def read_raw(
        self,
        redacted_text: str,
        context: ComprehensionContext,
        budget: TurnBudget | None = None,
    ) -> tuple[ComprehensionReading | None, LLMCallStats]:
        """Reads intent and clues as the model states them, with the date still unresolved.

        The output is constrained to the reading schema and validated by Pydantic; an invalid
        one gets a stricter retry when the turn still has one (TRZ-36 CA3). The evaluation cache
        keeps this form, so a change to the window table applies to cached answers without
        calling the model again.

        Args:
            redacted_text: Customer message with PII already replaced.
            context: Simulated "now" and the customer's country and currency.
            budget: Retry budget of the turn; a fresh one when omitted.

        Returns:
            The reading, or None when the LLM failed or answered invalid output twice, and the
            call stats.
        """
        prompt = self.comprehension_prompt
        user = comprehension_user(redacted_text, context.country_code, context.local_currency)
        system = prompt.system
        stats = LLMCallStats(model=self.model, prompt_version=prompt.version)
        budget = budget or self.new_turn()
        while True:
            raw, call = self._call(
                system,
                user,
                max_tokens=600,
                temperature=0.0,
                schema=READING_SCHEMA,
                prompt_version=prompt.version,
                budget=budget,
            )
            stats.add(call)
            if call.fallback:
                # Timeouts and transport errors were already retried within the budget.
                stats.error = call.error
                return None, stats
            try:
                reading = ComprehensionReading.model_validate_json(_strip_fence(raw))
            except ValidationError as exc:
                stats.error = f"invalid_json:{type(exc).__name__}"
                if budget.retries_left == 0:
                    return None, stats
                budget.retries_left -= 1
                system = prompt.system + "\nYour previous output was not valid. Output JSON only."
                continue
            stats.error = None
            return reading, stats

    def comprehend(
        self,
        redacted_text: str,
        context: ComprehensionContext,
        budget: TurnBudget | None = None,
    ) -> tuple[Comprehension, LLMCallStats]:
        """Reads intent and clues; the rules baseline answers when the LLM cannot.

        Clues whose evidence is not in the message are dropped, named in `stats.dropped_clues`
        and logged before the result is returned (TRZ-12 CA2).

        Args:
            redacted_text: Customer message with PII already replaced.
            context: Simulated "now" and the customer's country and currency.
            budget: Retry budget of the turn; a fresh one when omitted.

        Returns:
            The comprehension and the call stats; `stats.fallback` is set when the rules
            produced it.
        """
        reading, stats = self.read_clues(redacted_text, context, budget)
        if reading is None:
            stats.fallback = True
            log.warning("comprehension_fallback_to_rules", error=stats.error)
            return comprehend_rules(redacted_text, context), stats
        result, stats.dropped_clues = reading.faithful(redacted_text)
        if stats.dropped_clues:
            log.warning("unfaithful_clues_dropped", clues=stats.dropped_clues)
        return result, stats

    def translate(self, redacted_text: str) -> tuple[str | None, LLMCallStats]:
        """Translates a redacted Portuguese message into Spanish for the analyst (TRZ-25 CA3).

        The translation is labeled automatic wherever it is shown and is never used as a fact.

        Args:
            redacted_text: Customer message with PII already replaced.

        Returns:
            The translation, or None when the call failed, and the call stats.
        """
        text, stats = self._call(TRANSLATE_SYSTEM, redacted_text, max_tokens=600)
        if stats.fallback or not text.strip():
            stats.fallback = True
            return None, stats
        return text.strip(), stats

    def compose(
        self,
        redacted_text: str,
        facts: dict[str, Any],
        language: str,
        budget: TurnBudget | None = None,
    ) -> tuple[str, LLMCallStats]:
        """Writes the customer reply from facts.

        The reply is not trusted: the caller checks every figure in it against the verified
        facts before sending it (TRZ-20).

        Args:
            redacted_text: Customer message with PII already replaced.
            facts: What the system did, as decided by code.
            language: Reply language.
            budget: Retry budget of the turn; a fresh one when omitted.

        Returns:
            The reply (LLM or template) and the call stats.
        """
        user = (
            f"Customer language: {language}\nCustomer message: {redacted_text}\n"
            f"Facts (JSON): {json.dumps(facts, ensure_ascii=False)}"
        )
        reply, stats = self._call(COMPOSE_SYSTEM, user, max_tokens=300, budget=budget)
        if stats.fallback or not reply.strip():
            stats.fallback = True
            return template_reply(facts, language), stats
        return reply.strip(), stats


_HERE_ES = " Por aquí atendemos aclaraciones de cargos y te informamos su estado."
_HERE_PT = " Por aqui atendemos contestações de cobranças e informamos o status delas."

_REPLIES: dict[str, dict[str, str]] = {
    "es": {
        "registered_verified": "Registramos tu aclaración sobre el cargo con el folio {folio}.",
        "registered_block": (
            "Registramos tu aclaración sobre el cargo con el folio {folio} y bloqueamos la "
            "tarjeta de ese cargo."
        ),
        "registered_not_blocked": (
            "Registramos tu aclaración sobre el cargo con el folio {folio}, pero no pudimos "
            "bloquear la tarjeta de ese cargo."
        ),
        "registered_card_block": (
            "Registramos tu aclaración sobre el cargo con el folio {folio}, pero no pudimos "
            "bloquear la tarjeta de ese cargo. Bloquéala de inmediato con la opción de bloqueo "
            "de la app de tu banco o llamando a la línea de bloqueo que aparece en el sitio "
            "oficial del banco."
        ),
        "failed": (
            "No pudimos confirmar el registro de tu aclaración, así que pasamos tu caso a una "
            "analista con toda la información. Verás su decisión en Mis aclaraciones."
        ),
        "failed_card_block": (
            "No pudimos confirmar el registro de tu aclaración ni el bloqueo de tu tarjeta, así "
            "que pasamos tu caso a una analista. Bloquea tu tarjeta de inmediato "
            "con la opción de bloqueo de la app de tu banco o llamando a la línea de bloqueo que "
            "aparece en el sitio oficial del banco."
        ),
        # The screen asks the question with its buttons; the reply only gives context.
        "confirm_context": "Este es el cargo de tu aclaración.",
        "approval": (
            "Una analista revisará tu aclaración antes de registrarla. Verás su decisión en "
            "Mis aclaraciones."
        ),
        "escalated": (
            "Una analista revisará tu caso con toda la información. Verás su decisión en "
            "Mis aclaraciones."
        ),
        "security": (
            "Por seguridad no podemos continuar este caso por aquí. Una analista lo revisará."
        ),
        "out_of_scope": (
            "Por este canal atendemos aclaraciones de cargos y te informamos su estado. Para "
            "esta solicitud, usa la app o la línea de atención de tu banco."
        ),
        "card_block": (
            "Bloquea tu tarjeta de inmediato con la opción de bloqueo de la app de tu banco o "
            "llamando a la línea de bloqueo que aparece en el sitio oficial del banco. Si ves "
            "cargos que no reconoces, escríbenos y los revisamos contigo."
        ),
        "show_options": (
            "Encontramos varios cargos que podrían ser el que mencionas. Elige cuál es."
        ),
        "ask_for_detail": (
            "Encontramos muchos cargos posibles. ¿Nos dices el monto o la fecha aproximada?"
        ),
        "explain_and_watch": (
            "Uno de los dos cargos todavía está pendiente: suele ser una retención temporal "
            "que no se cobra. Si al liquidarse sigue apareciendo dos veces, escríbenos."
        ),
        "out_of_scope_loan": (
            "Por este canal no podemos tramitar préstamos ni créditos. Para solicitarlo, usa la "
            "sección de préstamos de la app de tu banco o acude a una sucursal."
            f"{_HERE_ES}"
        ),
        "out_of_scope_investment": (
            "Por este canal no podemos asesorarte sobre inversiones. Para eso, habla con un asesor "
            f"de tu banco desde la app o en una sucursal.{_HERE_ES}"
        ),
        "out_of_scope_branch": (
            "Por este canal no tenemos información de sucursales ni de sus horarios. Consúltala en "
            f"el sitio oficial o en la app de tu banco.{_HERE_ES}"
        ),
        "out_of_scope_personal_data": (
            "Por este canal no podemos cambiar tus datos personales. Actualízalos en la app de tu "
            f"banco o en una sucursal.{_HERE_ES}"
        ),
        "out_of_scope_app": (
            "Por este canal no podemos resolver problemas de la app ni de ingreso. Para eso, usa "
            "la opción de ayuda de la app o la línea de atención que aparece en el sitio oficial "
            f"de tu banco.{_HERE_ES}"
        ),
        "claim_none": "No encontramos aclaraciones abiertas a tu nombre.",
        "claim_one": "Esto es lo que vemos de tu aclaración.",
        "claim_other": "No vemos otra aclaración abierta a tu nombre.",
        "show_claims": "Tienes varias aclaraciones abiertas. Elige cuál quieres consultar.",
        "no_pending_action": "No hay ninguna acción pendiente de confirmar en este caso.",
        "recognized_closed": (
            "Listo: cerramos el caso sin registrar nada. Si ves otro cargo que no reconoces, "
            "escríbenos."
        ),
        "no_pending_recognition": "No hay ningún cargo esperando tu respuesta en este caso.",
        "no_pending_choice": "No hay opciones esperando tu elección en este caso.",
        "with_person": (
            "Tu caso ya está con una persona. Agregamos tu mensaje a su expediente para que lo "
            "tenga en cuenta."
        ),
        "card_blocked": "Bloqueamos la tarjeta de ese cargo. Tu aclaración sigue registrada.",
        "card_not_blocked": (
            "No pudimos confirmar el bloqueo de tu tarjeta. Bloquéala de inmediato con la opción "
            "de bloqueo de la app de tu banco o llamando a la línea de bloqueo que aparece en el "
            "sitio oficial del banco. Tu aclaración sigue registrada."
        ),
        "declined": (
            "De acuerdo, no hicimos ningún registro. Si cambias de opinión, puedes aclarar el "
            "cargo cuando quieras."
        ),
        "block_declined": "De acuerdo, tu tarjeta sigue activa. Tu aclaración sigue registrada.",
    },
    "pt": {
        "registered_verified": (
            "Registramos a sua contestação da cobrança com o protocolo {folio}."
        ),
        "registered_block": (
            "Registramos a sua contestação da cobrança com o protocolo {folio} e bloqueamos o "
            "cartão dessa cobrança."
        ),
        "registered_not_blocked": (
            "Registramos a sua contestação da cobrança com o protocolo {folio}, mas não "
            "conseguimos bloquear o cartão dessa cobrança."
        ),
        "registered_card_block": (
            "Registramos a sua contestação da cobrança com o protocolo {folio}, mas não "
            "conseguimos bloquear o cartão dessa cobrança. Bloqueie-o agora mesmo pela opção de "
            "bloqueio do app do seu banco ou ligando para a central de bloqueio indicada no site "
            "oficial do banco."
        ),
        "failed": (
            "Não conseguimos confirmar o registro da sua contestação, então encaminhamos o seu "
            "caso a uma analista com todas as informações. Você verá a decisão em Minhas "
            "contestações."
        ),
        "failed_card_block": (
            "Não conseguimos confirmar o registro da sua contestação nem o bloqueio do seu "
            "cartão, então encaminhamos o seu caso a uma analista. Bloqueie o "
            "seu cartão agora mesmo pela opção de bloqueio do app do seu banco ou ligando para a "
            "central de bloqueio indicada no site oficial do banco."
        ),
        "confirm_context": "Esta é a cobrança da sua contestação.",
        "approval": (
            "Uma analista vai revisar a sua contestação antes de registrá-la. Você verá a "
            "decisão em Minhas contestações."
        ),
        "escalated": (
            "Uma analista vai revisar o seu caso com todas as informações. Você verá a decisão "
            "em Minhas contestações."
        ),
        "security": (
            "Por segurança não podemos continuar este caso por aqui. Uma analista vai revisá-lo."
        ),
        "out_of_scope": (
            "Por este canal atendemos contestações de cobranças e informamos o status delas. "
            "Para este pedido, use o app ou a central de atendimento do seu banco."
        ),
        "card_block": (
            "Bloqueie o seu cartão agora mesmo pela opção de bloqueio do app do seu banco ou "
            "ligando para a central de bloqueio indicada no site oficial do banco. Se houver "
            "cobranças que você não reconhece, escreva para nós e verificamos com você."
        ),
        "show_options": (
            "Encontramos várias cobranças que podem ser a que você mencionou. Escolha qual é."
        ),
        "ask_for_detail": (
            "Encontramos muitas cobranças possíveis. Pode nos dizer o valor ou a data aproximada?"
        ),
        "explain_and_watch": (
            "Uma das duas cobranças ainda está pendente: costuma ser uma retenção temporária "
            "que não é cobrada. Se depois de liquidada ela continuar aparecendo duas vezes, "
            "fale com a gente."
        ),
        "out_of_scope_loan": (
            "Por este canal não conseguimos contratar empréstimos nem financiamentos. Para isso, "
            "use a área de empréstimos do app do seu banco ou vá a uma agência."
            f"{_HERE_PT}"
        ),
        "out_of_scope_investment": (
            "Por este canal não conseguimos orientar sobre investimentos. Para isso, fale com um "
            f"assessor do seu banco pelo app ou em uma agência.{_HERE_PT}"
        ),
        "out_of_scope_branch": (
            "Por este canal não temos informações de agências nem dos seus horários. Consulte no "
            f"site oficial ou no app do seu banco.{_HERE_PT}"
        ),
        "out_of_scope_personal_data": (
            "Por este canal não conseguimos alterar os seus dados pessoais. Atualize-os no app do "
            f"seu banco ou em uma agência.{_HERE_PT}"
        ),
        "out_of_scope_app": (
            "Por este canal não conseguimos resolver problemas do app nem de acesso. Para isso, "
            "use a opção de ajuda do app ou a central de atendimento indicada no site oficial do "
            f"seu banco.{_HERE_PT}"
        ),
        "claim_none": "Não encontramos contestações abertas em seu nome.",
        "claim_one": "Veja como está a sua contestação.",
        "claim_other": "Não vemos outra contestação aberta em seu nome.",
        "show_claims": "Você tem várias contestações abertas. Escolha qual quer consultar.",
        "no_pending_action": "Não há nenhuma ação pendente de confirmação neste caso.",
        "recognized_closed": (
            "Pronto: encerramos o caso sem registrar nada. Se você vir outra cobrança que não "
            "reconhece, fale com a gente."
        ),
        "no_pending_recognition": "Não há nenhuma cobrança aguardando a sua resposta neste caso.",
        "no_pending_choice": "Não há opções aguardando a sua escolha neste caso.",
        "with_person": (
            "O seu caso já está com uma pessoa. Incluímos a sua mensagem no dossiê para que ela "
            "a considere."
        ),
        "card_blocked": (
            "Bloqueamos o cartão dessa cobrança. A sua contestação continua registrada."
        ),
        "card_not_blocked": (
            "Não conseguimos confirmar o bloqueio do seu cartão. Bloqueie-o agora mesmo pela "
            "opção de bloqueio do app do seu banco ou ligando para a central de bloqueio indicada "
            "no site oficial do banco. A sua contestação continua registrada."
        ),
        "declined": (
            "Tudo bem, não fizemos nenhum registro. Se mudar de ideia, você pode contestar a "
            "cobrança quando quiser."
        ),
        "block_declined": (
            "Tudo bem, o seu cartão continua ativo. A sua contestação continua registrada."
        ),
    },
}


# Why a case goes to a person, by the rule that decided it, in the customer's words: no rule,
# threshold or system name. Prefixed to the handoff reply by code.
HANDOFF_REASONS: dict[str, dict[str, str]] = {
    "es": {
        "escalate.comprehension_unavailable": "No pudimos procesar tu mensaje en este momento.",
        "escalate.clarifications_exhausted": "No logramos identificar el cargo con certeza.",
        "escalate.amount_above_human_review": "Por el monto de este cargo, lo revisa una persona.",
        "escalate.amount_unknown": "No pudimos confirmar el monto de este cargo.",
        "escalate.open_dispute_last_90d": (
            "Ya tienes una aclaración en curso, así que revisamos este cargo junto con ella."
        ),
        "escalate.conformal_set_empty": (
            "No encontramos un cargo que coincida con lo que nos describes."
        ),
        "escalate.verification_failed": "No pudimos confirmar el registro.",
        "escalate.autonomy_a2": "Por ahora, cada caso de este tipo lo revisa una persona.",
        "approval.autonomy_a1": (
            "Por ahora, cada aclaración de este tipo la revisa una persona antes de registrarse."
        ),
        "approval.amount_above_auto_register": (
            "Por el monto de este cargo, una persona revisa la aclaración antes de registrarla."
        ),
    },
    "pt": {
        "escalate.comprehension_unavailable": "Não conseguimos processar a sua mensagem agora.",
        "escalate.clarifications_exhausted": (
            "Não conseguimos identificar a cobrança com certeza."
        ),
        "escalate.amount_above_human_review": (
            "Pelo valor desta cobrança, uma pessoa faz a revisão."
        ),
        "escalate.amount_unknown": "Não conseguimos confirmar o valor desta cobrança.",
        "escalate.open_dispute_last_90d": (
            "Você já tem uma contestação em andamento, então revisamos esta cobrança junto com ela."
        ),
        "escalate.conformal_set_empty": (
            "Não encontramos uma cobrança que corresponda ao que você descreveu."
        ),
        "escalate.verification_failed": "Não conseguimos confirmar o registro.",
        "escalate.autonomy_a2": "Por enquanto, cada caso deste tipo é revisado por uma pessoa.",
        "approval.autonomy_a1": (
            "Por enquanto, cada contestação deste tipo é revisada por uma pessoa antes do registro."
        ),
        "approval.amount_above_auto_register": (
            "Pelo valor desta cobrança, uma pessoa revisa a contestação antes do registro."
        ),
    },
}


def reply_key(facts: dict[str, Any]) -> str:
    """Which fixed reply fits the facts of a turn.

    Args:
        facts: What the system did, as decided by code.

    Returns:
        A key of the reply table.
    """
    outcome = facts.get("outcome")
    if outcome == "failed":
        return "failed_card_block" if facts.get("redirect") == "card_block" else "failed"
    if outcome == "registered_verified":
        if "block_card" in facts.get("actions_taken", []):
            return "registered_block"
        if facts.get("redirect") == "card_block":
            return "registered_card_block"
        return "registered_not_blocked" if facts.get("card_not_blocked") else outcome
    if outcome == "awaiting_confirmation":
        return "confirm_context"
    if outcome == "abstained":
        if facts.get("redirect") == "card_block":
            return "card_block"
        topic = facts.get("topic")
        return f"out_of_scope_{topic}" if topic and topic != "other" else "out_of_scope"
    if outcome == "identifying":
        return str(facts.get("identification", "show_options"))
    if outcome == "informed":
        if facts.get("action") == "explain_and_watch":
            return "explain_and_watch"
        if facts.get("claim"):
            return "claim_one"
        return "claim_other" if facts.get("other_claim") else "claim_none"
    return {
        "pending_analyst_approval": "approval",
        "security_blocked": "security",
        "no_pending_action": "no_pending_action",
        "recognized_closed": "recognized_closed",
        "no_pending_recognition": "no_pending_recognition",
        "no_pending_choice": "no_pending_choice",
        "with_person": "with_person",
        "card_blocked": "card_blocked",
        "card_not_blocked": "card_not_blocked",
        "declined": "declined",
        "block_declined": "block_declined",
    }.get(str(outcome), "escalated")


def template_reply(facts: dict[str, Any], language: str) -> str:
    """Fixed reply used when the LLM is unavailable, the fact checker blocks its reply, or the
    reply must not vary (the urgent card block redirect, a security stop, a failed read-back, a
    handoff, which starts with its reason).

    Args:
        facts: What the system did.
        language: "pt" for Portuguese, anything else for Spanish.

    Returns:
        The reply text.
    """
    lang = "pt" if language == "pt" else "es"
    folio = (facts.get("dispute") or {}).get("folio", "")
    text = _REPLIES[lang][reply_key(facts)].format(folio=folio)
    reason = HANDOFF_REASONS[lang].get(str(facts.get("handoff_reason")))
    return f"{reason} {text}" if reason else text


def _strip_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
    return s.strip()


def _retryable(exc: Exception, wait: float) -> bool:
    """Whether another attempt may succeed: a transient failure, not a request the provider
    rejects."""
    import anthropic

    if isinstance(exc, TimeoutError | anthropic.APIConnectionError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        if status == 429:
            after = exc.response.headers.get("retry-after")
            try:
                return after is None or float(after) <= wait
            except ValueError:
                return False
        return status >= 500
    return False

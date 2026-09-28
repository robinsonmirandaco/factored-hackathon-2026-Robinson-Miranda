"""LLM client and its deterministic fallbacks.

Two providers, chosen by settings: Claude through the Anthropic SDK, or a local model behind an
OpenAI-compatible endpoint (Docker Model Runner, Ollama). Prompts and fallbacks are shared.

Calls:
  comprehend(text, ctx)    -> Comprehension of design 6.1; the rules baseline is its fallback
  compose(...)             -> customer reply; the deterministic fact checker of TRZ-20 decides
                              whether it is sent

Every call is logged with model, prompt version, tokens, latency and cost (TRZ-12 CA6). Every
failure returns a typed fallback. Callers never see an exception from this module.
"""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import yaml
from anthropic import transform_schema
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.clock import SimulatedClock
from app.domain.comprehension_rules import comprehend_rules
from app.domain.recognition import long_date
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
response deadlines nor cite policy: they are added after your text. Do not mention internal
scores, rules or system names."""


class LLMClient:
    """LLM client with timeout, one bounded retry and deterministic fallbacks."""

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None) -> None:
        """Creates the provider client when the LLM is enabled and configured.

        Args:
            settings: Application settings (provider, model, timeout, retries, key, base URL).
            http_client: Client for the local provider; built from settings when omitted.
        """
        self.provider = settings.llm_provider
        self.model = settings.llm_model_primary
        self.comprehension_prompt = load_comprehension_prompt(
            settings.llm_comprehension_prompt_path
        )
        self._price_in = settings.llm_price_input_per_mtok
        self._price_out = settings.llm_price_output_per_mtok
        self._max_retries = settings.llm_max_retries
        # httpx timeouts bound each network phase, not the whole call, so a slow server can
        # hold a turn far longer than the design's 5 s. The pool enforces a wall-clock deadline.
        self._deadline_seconds = settings.llm_timeout_seconds * (settings.llm_max_retries + 1)
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")
        self._client: Any = None
        self._http: httpx.Client | None = None
        if not settings.llm_enabled:
            return
        if self.provider == "local":
            if settings.llm_base_url:
                self._http = http_client or httpx.Client(
                    base_url=settings.llm_base_url.rstrip("/"),
                    timeout=settings.llm_timeout_seconds,
                )
        elif settings.anthropic_api_key:
            try:
                import anthropic

                self._client = anthropic.Anthropic(
                    api_key=settings.anthropic_api_key,
                    timeout=settings.llm_timeout_seconds,
                    max_retries=settings.llm_max_retries,
                )
            except Exception as exc:  # pragma: no cover
                log.error("llm_init_failed", error=type(exc).__name__)

    @property
    def available(self) -> bool:
        """True when real LLM calls can be made."""
        return self._client is not None or self._http is not None

    def _call(
        self,
        system: str,
        user: str,
        max_tokens: int = 400,
        temperature: float | None = None,
        *,
        schema: dict[str, Any] | None = None,
        prompt_version: str | None = None,
    ) -> tuple[str, LLMCallStats]:
        stats = LLMCallStats(
            model=self.model, prompt_version=prompt_version or prompt_version_of(system)
        )
        if not self.available:
            stats.fallback = True
            stats.error = "llm_disabled"
            return "", stats
        t0 = time.perf_counter()
        complete = self._complete_local if self._http is not None else self._complete_anthropic
        try:
            future = self._pool.submit(complete, system, user, max_tokens, temperature, schema)
            text, usage = future.result(timeout=self._deadline_seconds)
            stats.input_tokens, stats.output_tokens = usage[0], usage[1]
            stats.cache_write_tokens, stats.cache_read_tokens = usage[2], usage[3]
        except Exception as exc:
            stats.fallback = True
            stats.error = type(exc).__name__
            log.warning("llm_call_failed", provider=self.provider, error=stats.error)
            text = ""
        stats.latency_ms = int((time.perf_counter() - t0) * 1000)
        stats.calls = 1
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
            failed=stats.fallback,
        )
        return text, stats

    def _complete_anthropic(
        self,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float | None,
        schema: dict[str, Any] | None,
    ) -> tuple[str, tuple[int, int, int, int]]:
        # The SDK applies the timeout and the bounded retry configured in __init__. SDK 1.x
        # dropped `temperature` from its signature; models before Opus 4.7, such as Haiku 4.5,
        # still accept it in the request body.
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

    def _complete_local(
        self,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float | None,
        schema: dict[str, Any] | None,
    ) -> tuple[str, tuple[int, int, int, int]]:
        # Local servers differ in how they constrain output, so the schema is left to the
        # prompt and to the validation that follows.
        assert self._http is not None
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if temperature is not None:
            body["temperature"] = temperature
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.post("/chat/completions", json=body)
                response.raise_for_status()
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                # Same policy as the SDK: retry timeouts, network errors, 429 and 5xx only.
                retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                    exc.response.status_code == 429 or exc.response.status_code >= 500
                )
                if not retryable or attempt == self._max_retries:
                    raise
        data = response.json()
        usage = data.get("usage") or {}
        text = data["choices"][0]["message"]["content"] or ""
        tokens = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        return text, (*tokens, 0, 0)

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
        self, redacted_text: str, context: ComprehensionContext
    ) -> tuple[Comprehension | None, LLMCallStats]:
        """Reads intent and clues with the LLM, before the faithfulness check.

        The date is resolved with the simulated clock. The evaluation harness scores this
        reading, so unfaithful clues count against the model.

        Args:
            redacted_text: Customer message with PII already replaced.
            context: Simulated "now" and the customer's country and currency.

        Returns:
            The comprehension, or None when the LLM failed or answered invalid output twice,
            and the call stats.
        """
        reading, stats = self.read_raw(redacted_text, context)
        return (None if reading is None else resolve_reading(reading, context)), stats

    def read_raw(
        self, redacted_text: str, context: ComprehensionContext
    ) -> tuple[ComprehensionReading | None, LLMCallStats]:
        """Reads intent and clues as the model states them, with the date still unresolved.

        The output is constrained to the reading schema and validated by Pydantic; an invalid
        one gets a single stricter retry. The evaluation cache keeps this form, so a change to
        the window table applies to cached answers without calling the model again.

        Args:
            redacted_text: Customer message with PII already replaced.
            context: Simulated "now" and the customer's country and currency.

        Returns:
            The reading, or None when the LLM failed or answered invalid output twice, and the
            call stats.
        """
        prompt = self.comprehension_prompt
        user = comprehension_user(redacted_text, context.country_code, context.local_currency)
        system = prompt.system
        stats = LLMCallStats(model=self.model, prompt_version=prompt.version)
        for _ in range(2):
            raw, call = self._call(
                system,
                user,
                max_tokens=600,
                temperature=0.0,
                schema=READING_SCHEMA,
                prompt_version=prompt.version,
            )
            stats.add(call)
            if call.fallback:
                # Timeouts and transport errors were already retried by the provider client.
                stats.error = call.error
                return None, stats
            try:
                reading = ComprehensionReading.model_validate_json(_strip_fence(raw))
            except ValidationError as exc:
                stats.error = f"invalid_json:{type(exc).__name__}"
                system = prompt.system + "\nYour previous output was not valid. Output JSON only."
                continue
            stats.error = None
            return reading, stats
        return None, stats

    def comprehend(
        self, redacted_text: str, context: ComprehensionContext
    ) -> tuple[Comprehension, LLMCallStats]:
        """Reads intent and clues; the rules baseline answers when the LLM cannot.

        Clues whose evidence is not in the message are dropped, named in `stats.dropped_clues`
        and logged before the result is returned (TRZ-12 CA2).

        Args:
            redacted_text: Customer message with PII already replaced.
            context: Simulated "now" and the customer's country and currency.

        Returns:
            The comprehension and the call stats; `stats.fallback` is set when the rules
            produced it.
        """
        reading, stats = self.read_clues(redacted_text, context)
        if reading is None:
            stats.fallback = True
            log.warning("comprehension_fallback_to_rules", error=stats.error)
            return comprehend_rules(redacted_text, context), stats
        result, stats.dropped_clues = reading.faithful(redacted_text)
        if stats.dropped_clues:
            log.warning("unfaithful_clues_dropped", clues=stats.dropped_clues)
        return result, stats

    def compose(
        self, redacted_text: str, facts: dict[str, Any], language: str
    ) -> tuple[str, LLMCallStats]:
        """Writes the customer reply from facts.

        The reply is not trusted: the caller checks every figure in it against the verified
        facts before sending it (TRZ-20).

        Args:
            redacted_text: Customer message with PII already replaced.
            facts: What the system did, as decided by code.
            language: Reply language.

        Returns:
            The reply (LLM or template) and the call stats.
        """
        user = (
            f"Customer language: {language}\nCustomer message: {redacted_text}\n"
            f"Facts (JSON): {json.dumps(facts, ensure_ascii=False)}"
        )
        reply, stats = self._call(COMPOSE_SYSTEM, user, max_tokens=300)
        if stats.fallback or not reply.strip():
            stats.fallback = True
            return template_reply(facts, language), stats
        return reply.strip(), stats


_HERE_ES = " Por aquí atiendo aclaraciones de cargos y el estado de tus reclamos."
_HERE_PT = " Por aqui eu atendo contestações de cobranças e o status das suas reclamações."

# Words of a claim's status and last step in the fixed reply (TRZ-22).
_CLAIM_WORDS: dict[str, dict[str, str]] = {
    "es": {
        "received": "recibido",
        "in_review": "en revisión",
        "answered": "con una primera respuesta del banco, pendiente de resolución",
        "created": "su creación",
        "assigned": "la asignación a una analista",
        "first_response": "la primera respuesta del banco",
        "registered": "el registro de la aclaración",
    },
    "pt": {
        "received": "recebida",
        "in_review": "em análise",
        "answered": "com uma primeira resposta do banco, aguardando solução",
        "created": "a abertura",
        "assigned": "a atribuição a uma analista",
        "first_response": "a primeira resposta do banco",
        "registered": "o registro da contestação",
    },
}

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
            "analista con toda la información. Ella te contactará."
        ),
        "failed_card_block": (
            "No pudimos confirmar el registro de tu aclaración ni el bloqueo de tu tarjeta, así "
            "que pasamos tu caso a una analista. Bloquea tu tarjeta de inmediato "
            "con la opción de bloqueo de la app de tu banco o llamando a la línea de bloqueo que "
            "aparece en el sitio oficial del banco."
        ),
        "confirm_register": "Responde sí para registrar la aclaración de este cargo.",
        "confirm_block": (
            "Responde sí para registrar la aclaración de este cargo y bloquear tu tarjeta."
        ),
        "approval": (
            "Una analista revisará tu aclaración antes de registrarla. Te avisaremos cuando "
            "tenga una respuesta."
        ),
        "escalated": (
            "Pasamos tu caso a una analista con toda la información. Ella te contactará."
        ),
        "security": (
            "Por seguridad no podemos continuar este caso por aquí. Una analista lo revisará."
        ),
        "out_of_scope": (
            "Por este canal atiendo aclaraciones de cargos y el estado de tus reclamos. Para "
            "esta solicitud, usa la app o la línea de atención de tu banco."
        ),
        "card_block": (
            "Bloquea tu tarjeta de inmediato con la opción de bloqueo de la app de tu banco o "
            "llamando a la línea de bloqueo que aparece en el sitio oficial del banco. Si ves "
            "cargos que no reconoces, escríbeme y los reviso contigo."
        ),
        "show_options": ("Encontré varios cargos que podrían ser el que mencionas. Elige cuál es."),
        "ask_for_detail": (
            "Encontré muchos cargos posibles. ¿Me dices el monto o la fecha aproximada?"
        ),
        "explain_and_watch": (
            "Uno de los dos cargos todavía está pendiente: suele ser una retención temporal "
            "que no se cobra. Si al liquidarse sigue apareciendo dos veces, escríbenos."
        ),
        "out_of_scope_loan": (
            "Por este canal no puedo tramitar préstamos ni créditos. Para solicitarlo, usa la "
            "sección de préstamos de la app de tu banco o acude a una sucursal."
            f"{_HERE_ES}"
        ),
        "out_of_scope_investment": (
            "Por este canal no puedo asesorarte sobre inversiones. Para eso, habla con un asesor "
            f"de tu banco desde la app o en una sucursal.{_HERE_ES}"
        ),
        "out_of_scope_branch": (
            "Por este canal no tengo información de sucursales ni de sus horarios. Consúltala en "
            f"el sitio oficial o en la app de tu banco.{_HERE_ES}"
        ),
        "out_of_scope_personal_data": (
            "Por este canal no puedo cambiar tus datos personales. Actualízalos en la app de tu "
            f"banco o en una sucursal.{_HERE_ES}"
        ),
        "out_of_scope_app": (
            "Por este canal no puedo resolver problemas de la app ni de ingreso. Para eso, usa la "
            "opción de ayuda de la app o la línea de atención que aparece en el sitio oficial de "
            f"tu banco.{_HERE_ES}"
        ),
        "claim_none": "No encuentro reclamos abiertos a tu nombre.",
        "claim_one": (
            "Tu reclamo {claim_id}, abierto el {opened}, está {status}. Último paso: {step}, el "
            "{step_on}."
        ),
        "claim_other": "No veo otro reclamo abierto a tu nombre.",
        "show_claims": "Tienes varios reclamos abiertos. Elige cuál quieres consultar.",
        "no_pending_action": "No hay ninguna acción pendiente de confirmar en este caso.",
        "recognized_closed": (
            "Listo: cerramos el caso sin registrar nada. Si ves otro cargo que no reconoces, "
            "escríbenos."
        ),
        "no_pending_recognition": "No hay ningún cargo esperando tu respuesta en este caso.",
        "no_pending_choice": "No hay opciones esperando tu elección en este caso.",
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
            "caso a uma analista com todas as informações. Ela vai entrar em contato."
        ),
        "failed_card_block": (
            "Não conseguimos confirmar o registro da sua contestação nem o bloqueio do seu "
            "cartão, então encaminhamos o seu caso a uma analista. Bloqueie o "
            "seu cartão agora mesmo pela opção de bloqueio do app do seu banco ou ligando para a "
            "central de bloqueio indicada no site oficial do banco."
        ),
        "confirm_register": "Responda sim para registrar a contestação desta cobrança.",
        "confirm_block": (
            "Responda sim para registrar a contestação desta cobrança e bloquear o seu cartão."
        ),
        "approval": (
            "Uma analista vai revisar a sua contestação antes de registrá-la. Avisaremos quando "
            "houver uma resposta."
        ),
        "escalated": (
            "Encaminhamos o seu caso a uma analista com todas as informações. Ela vai entrar "
            "em contato."
        ),
        "security": (
            "Por segurança não podemos continuar este caso por aqui. Uma analista vai revisá-lo."
        ),
        "out_of_scope": (
            "Por este canal eu atendo contestações de cobranças e o status das suas "
            "reclamações. Para este pedido, use o app ou a central de atendimento do seu banco."
        ),
        "card_block": (
            "Bloqueie o seu cartão agora mesmo pela opção de bloqueio do app do seu banco ou "
            "ligando para a central de bloqueio indicada no site oficial do banco. Se houver "
            "cobranças que você não reconhece, me escreva e eu verifico com você."
        ),
        "show_options": (
            "Encontrei várias cobranças que podem ser a que você mencionou. Escolha qual é."
        ),
        "ask_for_detail": (
            "Encontrei muitas cobranças possíveis. Pode me dizer o valor ou a data aproximada?"
        ),
        "explain_and_watch": (
            "Uma das duas cobranças ainda está pendente: costuma ser uma retenção temporária "
            "que não é cobrada. Se depois de liquidada ela continuar aparecendo duas vezes, "
            "fale com a gente."
        ),
        "out_of_scope_loan": (
            "Por este canal não consigo contratar empréstimos nem financiamentos. Para isso, use "
            "a área de empréstimos do app do seu banco ou vá a uma agência."
            f"{_HERE_PT}"
        ),
        "out_of_scope_investment": (
            "Por este canal não consigo orientar sobre investimentos. Para isso, fale com um "
            f"assessor do seu banco pelo app ou em uma agência.{_HERE_PT}"
        ),
        "out_of_scope_branch": (
            "Por este canal não tenho informações de agências nem dos seus horários. Consulte no "
            f"site oficial ou no app do seu banco.{_HERE_PT}"
        ),
        "out_of_scope_personal_data": (
            "Por este canal não consigo alterar os seus dados pessoais. Atualize-os no app do seu "
            f"banco ou em uma agência.{_HERE_PT}"
        ),
        "out_of_scope_app": (
            "Por este canal não consigo resolver problemas do app nem de acesso. Para isso, use "
            "a opção de ajuda do app ou a central de atendimento indicada no site oficial do seu "
            f"banco.{_HERE_PT}"
        ),
        "claim_none": "Não encontrei reclamações abertas em seu nome.",
        "claim_one": (
            "A sua reclamação {claim_id}, aberta em {opened}, está {status}. Última etapa: "
            "{step}, em {step_on}."
        ),
        "claim_other": "Não vejo outra reclamação aberta em seu nome.",
        "show_claims": "Você tem várias reclamações abertas. Escolha qual quer consultar.",
        "no_pending_action": "Não há nenhuma ação pendente de confirmação neste caso.",
        "recognized_closed": (
            "Pronto: encerramos o caso sem registrar nada. Se você vir outra cobrança que não "
            "reconhece, fale com a gente."
        ),
        "no_pending_recognition": "Não há nenhuma cobrança aguardando a sua resposta neste caso.",
        "no_pending_choice": "Não há opções aguardando a sua escolha neste caso.",
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
        block = facts.get("action") == "register_and_block"
        return "confirm_block" if block else "confirm_register"
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
    }.get(str(outcome), "escalated")


def template_reply(facts: dict[str, Any], language: str) -> str:
    """Fixed reply used when the LLM is unavailable, the fact checker blocks its reply, or the
    reply must not vary (the urgent card block redirect, a security stop, a failed read-back).

    Args:
        facts: What the system did.
        language: "pt" for Portuguese, anything else for Spanish.

    Returns:
        The reply text.
    """
    lang = "pt" if language == "pt" else "es"
    fields = {"folio": (facts.get("dispute") or {}).get("folio", "")}
    if claim := facts.get("claim"):
        words = _CLAIM_WORDS[lang]
        fields |= {
            "claim_id": claim["claim_id"],
            "opened": long_date(date.fromisoformat(claim["opened_on"]), lang),
            "status": words[claim["status"]],
            "step": words[claim["last_step"]["step"]],
            "step_on": long_date(date.fromisoformat(claim["last_step"]["on"]), lang),
        }
    return _REPLIES[lang][reply_key(facts)].format(**fields)


def _strip_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
    return s.strip()

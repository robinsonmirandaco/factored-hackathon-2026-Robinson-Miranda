"""LLM client: the retry budget of a turn, the deadline of each attempt, which failures are
retried, JSON validation, the warm-up call and the fixed replies (TRZ-36)."""

import json
import threading
import time
from collections.abc import Callable
from datetime import datetime

import httpx2 as httpx
import pytest

from app.adapters.llm import WARM_UP_MESSAGE, LLMClient, TurnBudget, template_reply
from app.schemas.comprehension import ComprehensionContext
from tests.llm_support import (
    Handler,
    anthropic_http,
    api_error,
    llm_test_settings,
    message,
    request_parts,
)


def _client(handler: Handler, calls: list[str], **overrides: object) -> LLMClient:
    def counting(request: httpx.Request) -> httpx.Response:
        system, user, _ = request_parts(request)
        calls.append(system + "\n" + user)
        return handler(request)

    return LLMClient(llm_test_settings(**overrides), http_client=anthropic_http(counting))


def _raises(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise exc

    return handler


# Rules fallback of this message: out_of_scope, since "roubaram" alone names no charge.
MESSAGE = "Roubaram meu cartão"
CONTEXT = ComprehensionContext(
    now=datetime(2026, 6, 17, 10, 0), country_code="MX", local_currency="MXN"
)
READING = {
    "intent": "unrecognized_charge",
    "amount": None,
    "date": None,
    "merchant_hint": None,
    "channel_hint": None,
    "card_in_possession": {"value": False, "evidence": "Roubaram meu cartão"},
    "language": "pt-BR",
}


def test_comprehend_success_reports_tokens() -> None:
    calls: list[str] = []
    llm = _client(lambda _r: message(json.dumps(READING), 50, 10), calls)

    result, stats = llm.comprehend(MESSAGE, CONTEXT)

    assert result.intent == "unrecognized_charge"
    assert not stats.fallback
    assert (stats.input_tokens, stats.output_tokens, stats.calls) == (50, 10, 1)
    assert len(calls) == 1


def test_a_timeout_is_retried_once_then_succeeds() -> None:
    calls: list[str] = []
    attempts = iter([httpx.ReadTimeout("slow"), message(json.dumps(READING))])

    def handler(_r: httpx.Request) -> httpx.Response:
        outcome = next(attempts)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    result, stats = _client(handler, calls).comprehend(MESSAGE, CONTEXT)

    assert result.intent == "unrecognized_charge"
    assert (stats.fallback, stats.calls) == (False, 2)
    assert len(calls) == 2


def test_a_persistent_timeout_falls_back_to_the_rules_after_one_retry() -> None:
    calls: list[str] = []

    result, stats = _client(_raises(httpx.ReadTimeout("slow")), calls).comprehend(MESSAGE, CONTEXT)

    assert stats.fallback
    assert stats.error == "APITimeoutError"
    assert result.intent == "out_of_scope"  # rules baseline
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("status", "expected_calls"),
    [(400, 1), (401, 1), (404, 1), (429, 2), (500, 2), (503, 2), (529, 2)],
)
def test_only_transient_http_errors_are_retried(status: int, expected_calls: int) -> None:
    calls: list[str] = []
    _text, stats = _client(lambda _r: api_error(status), calls)._call("s", "u")

    assert stats.fallback
    assert len(calls) == expected_calls


def test_a_rate_limit_that_asks_to_wait_longer_is_not_retried() -> None:
    calls: list[str] = []
    llm = _client(lambda _r: api_error(429, {"retry-after": "30"}), calls)

    _text, stats = llm._call("s", "u")

    assert stats.fallback and len(calls) == 1


def test_invalid_json_gets_one_stricter_retry_then_the_rules() -> None:
    calls: list[str] = []
    result, stats = _client(lambda _r: message("not json"), calls).comprehend(MESSAGE, CONTEXT)

    assert stats.fallback
    assert stats.error is not None and stats.error.startswith("invalid_json")
    assert result.intent == "out_of_scope"  # rules baseline
    assert len(calls) == 2
    assert "not valid" in calls[1]


def test_each_attempt_is_cut_at_its_wall_clock_deadline() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        time.sleep(2)  # the mock ignores httpx timeouts, like a server that trickles bytes
        return message(json.dumps(READING))

    llm = _client(handler, [], llm_timeout_seconds=0.2)
    t0 = time.perf_counter()
    result, stats = llm.comprehend(MESSAGE, CONTEXT)
    elapsed = time.perf_counter() - t0

    assert stats.fallback
    assert stats.error == "TimeoutError"
    assert result.intent == "out_of_scope"  # rules baseline
    assert elapsed < 1.5  # two attempts of 0.2 s, far below the 2 s server delay


# ---- CA3: one retry of the LLM per turn, whatever the call -------------------------------


def test_the_retry_of_the_turn_is_spent_once_across_calls() -> None:
    calls: list[str] = []
    attempts = iter([httpx.ReadTimeout("slow"), message(json.dumps(READING))])

    def handler(_r: httpx.Request) -> httpx.Response:
        outcome = next(attempts, None)
        if outcome is None:
            raise httpx.ReadTimeout("slow again")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    llm = _client(handler, calls)
    budget = llm.new_turn()
    _, read = llm.comprehend(MESSAGE, CONTEXT, budget)
    reply, wrote = llm.compose("ayuda", {"outcome": "escalated"}, "es", budget)

    # Comprehension used the retry; the reply timed out once and was not retried.
    assert (read.fallback, read.calls) == (False, 2)
    assert (wrote.fallback, wrote.calls) == (True, 1)
    assert reply == template_reply({"outcome": "escalated"}, "es")
    assert len(calls) == 3


def test_once_the_llm_failed_in_a_turn_it_is_not_called_again() -> None:
    calls: list[str] = []
    llm = _client(lambda _r: api_error(503), calls)
    budget = llm.new_turn()

    _, read = llm.comprehend(MESSAGE, CONTEXT, budget)
    _, wrote = llm.compose("ayuda", {"outcome": "escalated"}, "es", budget)

    assert read.fallback and budget.down
    assert (wrote.fallback, wrote.error, wrote.calls) == (True, "llm_down_this_turn", 0)
    assert len(calls) == 2


def test_an_invalid_json_retry_spends_the_retry_of_the_turn() -> None:
    calls: list[str] = []
    llm = _client(lambda _r: message("not json"), calls)
    budget = TurnBudget(retries_left=1)

    llm.comprehend(MESSAGE, CONTEXT, budget)

    assert budget.retries_left == 0 and len(calls) == 2


def test_compose_makes_one_call_and_leaves_the_check_to_the_caller() -> None:
    calls: list[str] = []
    llm = _client(lambda _r: message("  Pasamos tu caso a una analista.  "), calls)

    reply, stats = llm.compose("ayuda", {"outcome": "escalated"}, "es")

    # No second LLM judges the reply: the fact checker of TRZ-20 does, deterministically.
    assert (reply, stats.fallback, stats.calls) == ("Pasamos tu caso a una analista.", False, 1)


def test_malformed_provider_response_falls_back() -> None:
    _text, stats = _client(lambda _r: httpx.Response(200, json={"unexpected": 1}), [])._call(
        "s", "u"
    )

    assert stats.fallback


@pytest.mark.parametrize("overrides", [{"anthropic_api_key": ""}, {"llm_enabled": False}])
def test_unconfigured_llm_is_not_available(overrides: dict[str, object]) -> None:
    llm = LLMClient(llm_test_settings(**overrides))

    _text, stats = llm._call("s", "u")

    assert not llm.available
    assert stats.error == "llm_disabled"


def test_complete_sends_the_temperature_in_the_body() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request_parts(request)[2])
        return message('{"es-MX": "hola"}')

    llm = LLMClient(llm_test_settings(), http_client=anthropic_http(handler))
    text, stats = llm.complete("s", "u", 50, 0.7)

    # anthropic 1.x has no `temperature` argument; the body still carries it.
    assert (text, stats.fallback, bodies[0]["temperature"]) == ('{"es-MX": "hola"}', False, 0.7)


def test_sonnet_5_5_gets_no_temperature_and_its_thinking_turned_off() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request_parts(request)[2])
        return message('{"es-MX": "hola"}')

    settings = llm_test_settings(llm_model_primary="claude-sonnet-5-5")
    llm = LLMClient(settings, http_client=anthropic_http(handler))
    llm.complete("s", "u", 50, 0.0)

    assert "temperature" not in bodies[0]
    assert bodies[0]["thinking"] == {"type": "between_tools"}


# ---- warm-up at startup ---------------------------------------------------------------------


def test_the_warm_up_is_one_comprehension_call_with_the_synthetic_message() -> None:
    calls: list[str] = []
    llm = _client(lambda _r: message(json.dumps(READING)), calls)

    llm.warm_up()

    assert len(calls) == 1
    assert calls[0].startswith(llm.comprehension_prompt.system[:40])
    assert WARM_UP_MESSAGE in calls[0]


def test_a_failed_warm_up_is_not_retried_and_raises_nothing() -> None:
    calls: list[str] = []

    _client(lambda _r: api_error(503), calls).warm_up()

    assert len(calls) == 1


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (
            {"actions_taken": ["register_dispute"]},
            "Registramos tu aclaración sobre el cargo con el folio DSP-2026-00007.",
        ),
        (
            {"actions_taken": ["register_dispute", "block_card"]},
            "Registramos tu aclaración sobre el cargo con el folio DSP-2026-00007 y bloqueamos "
            "la tarjeta de ese cargo.",
        ),
        (
            {"actions_taken": ["register_dispute"], "card_not_blocked": "not_a_card"},
            "Registramos tu aclaración sobre el cargo con el folio DSP-2026-00007, pero no "
            "pudimos bloquear la tarjeta de ese cargo.",
        ),
    ],
    ids=["register", "register and block", "card not blocked"],
)
def test_the_registration_reply_gives_the_folio(facts: dict, expected: str) -> None:
    facts = {"outcome": "registered_verified", "dispute": {"folio": "DSP-2026-00007"}, **facts}
    assert template_reply(facts, "es") == expected


def test_without_card_and_without_block_the_reply_sends_the_customer_to_block_it() -> None:
    facts = {
        "outcome": "registered_verified",
        "actions_taken": ["register_dispute"],
        "card_not_blocked": "card_not_active",
        "redirect": "card_block",
        "dispute": {"folio": "DSP-2026-00007"},
    }
    reply = template_reply(facts, "pt")
    assert "protocolo DSP-2026-00007" in reply
    assert "central de bloqueio" in reply


@pytest.mark.parametrize("language", ["es", "pt"])
@pytest.mark.parametrize("redirect", [None, "card_block"])
def test_a_failed_read_back_confirms_nothing(language: str, redirect: str | None) -> None:
    facts = {"outcome": "failed", "actions_taken": [], "redirect": redirect}
    reply = template_reply(facts, language)
    assert "DSP-" not in reply
    assert ("analista" in reply) and ("Registramos" not in reply)
    blocking = "línea de bloqueo" if language == "es" else "central de bloqueio"
    assert (blocking in reply) == (redirect == "card_block")


# ---- translation for the analyst (TRZ-25 CA3) ---------------------------------------------


def _translator(text: str) -> LLMClient:
    return _client(lambda _r: message(text), [])


def test_a_translation_that_adds_a_placeholder_is_not_used() -> None:
    # Seen in the demo rehearsal: "no meu cartão" came back as "en mi tarjeta [CARD]".
    text, stats = _translator(
        "Oye, apareció un cargo que no reconozco en mi tarjeta [CARD]"
    ).translate("Oi, apareceu uma cobrança que eu não reconheço no meu cartão")
    assert text is None
    assert stats.error == "translation_added_content"


def test_a_translation_that_adds_a_number_is_not_used() -> None:
    text, stats = _translator("Fue un cargo de 500 pesos en la librería").translate(
        "Foi uma cobrança na livraria"
    )
    assert text is None and stats.error == "translation_added_content"


def test_a_translation_keeps_the_placeholders_and_numbers_of_the_original() -> None:
    text, stats = _translator("Fue de 2.300.000 pesos con la tarjeta [CARD], ayer").translate(
        "Foi de 2.300.000 pesos com o cartão [CARD], ontem"
    )
    assert text == "Fue de 2.300.000 pesos con la tarjeta [CARD], ayer"
    assert stats.error is None


def test_the_translation_prompt_names_no_placeholder_the_message_lacks() -> None:
    calls: list[str] = []
    _client(lambda _r: message("Hola"), calls).translate("Oi")
    system = calls[0].split("\n")[0]
    assert "[CARD]" not in calls[0] and "[NAME]" not in calls[0], system


# ---- the pool of LLM threads: its size, and a deadline that starts with the call ----------


def _hold_the_pool(llm: LLMClient) -> threading.Event:
    """Occupies the only thread of a one-thread pool until the returned event is set."""
    release = threading.Event()
    started = threading.Event()

    def hold() -> None:
        started.set()
        release.wait(10)

    llm._pool.submit(hold)
    assert started.wait(5)
    return release


def test_the_pool_size_comes_from_the_settings() -> None:
    llm = _client(lambda _r: message(json.dumps(READING)), [], llm_pool_size=3)
    assert llm._pool._max_workers == 3


def test_the_deadline_starts_when_a_thread_runs_the_call_not_while_it_waits() -> None:
    calls: list[str] = []
    llm = _client(
        lambda _r: message(json.dumps(READING)),
        calls,
        llm_pool_size=1,
        llm_timeout_seconds=0.3,
        llm_queue_wait_seconds=5.0,
    )
    release = _hold_the_pool(llm)
    threading.Timer(0.8, release.set).start()

    result, stats = llm.comprehend(MESSAGE, CONTEXT)

    # Queued 0.8 s, longer than the 0.3 s deadline, then answered at once: one call, no fallback.
    assert not stats.fallback and stats.error is None
    assert stats.calls == 1 and len(calls) == 1
    assert result.intent == "unrecognized_charge"


def test_a_call_that_waits_too_long_for_a_thread_falls_back_without_reaching_the_provider() -> None:
    calls: list[str] = []
    llm = _client(
        lambda _r: message(json.dumps(READING)),
        calls,
        llm_pool_size=1,
        llm_queue_wait_seconds=0.2,
    )
    release = _hold_the_pool(llm)
    try:
        result, stats = llm.comprehend(MESSAGE, CONTEXT)
    finally:
        release.set()

    assert stats.fallback and stats.error == "llm_queue_full"
    assert calls == []
    assert result.intent == "out_of_scope"  # rules baseline

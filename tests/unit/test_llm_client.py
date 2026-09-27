"""LLM client: bounded retry, JSON validation and fallbacks for the local provider."""

import json
import time
from collections.abc import Callable

import httpx
import pytest

from app.adapters.llm import LLMClient
from app.core.config import Settings

BASE_URL = "http://llm.test/v1"
Handler = Callable[[httpx.Request], httpx.Response]


def _settings(**overrides: object) -> Settings:
    # Explicit so CI's LLM_ENABLED=false does not disable the client under test.
    # The LLM client never connects to the database; the URL only satisfies Settings.
    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://unused@localhost:1/unused",
        "llm_enabled": True,
        "llm_provider": "local",
        "llm_base_url": BASE_URL,
        "llm_model_primary": "test-model",
        "llm_max_retries": 1,
        "anthropic_api_key": "",
    }
    values.update(overrides)
    return Settings(**values)


def _client(handler: Handler, calls: list[str], **overrides: object) -> LLMClient:
    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["messages"][0]["content"])
        return handler(request)

    http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(counting))
    return LLMClient(_settings(**overrides), http_client=http)


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        },
    )


EXTRACTION = {
    "intent": "lost_or_stolen_card",
    "amount": None,
    "merchant": None,
    "language": "en",
    "customer_claims_legitimate": None,
    "confidence": 0.9,
}


def test_extract_success_reports_tokens() -> None:
    calls: list[str] = []
    llm = _client(lambda _r: _completion(json.dumps(EXTRACTION)), calls)

    result, stats = llm.extract("my card was stolen")

    assert result.intent == "lost_or_stolen_card"
    assert not stats.fallback
    assert (stats.input_tokens, stats.output_tokens) == (50, 10)
    assert len(calls) == 1


def test_timeout_is_retried_once_then_succeeds() -> None:
    calls: list[str] = []
    attempts = iter([httpx.ReadTimeout("slow"), _completion(json.dumps(EXTRACTION))])

    def handler(_r: httpx.Request) -> httpx.Response:
        outcome = next(attempts)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    result, stats = _client(handler, calls).extract("my card was stolen")

    assert result.intent == "lost_or_stolen_card"
    assert not stats.fallback
    assert len(calls) == 2


def test_persistent_timeout_falls_back_after_one_retry() -> None:
    calls: list[str] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    result, stats = _client(handler, calls).extract("my card was stolen")

    assert stats.fallback
    assert stats.error == "ReadTimeout"
    assert result.intent == "lost_or_stolen_card"  # heuristic path
    assert len(calls) == 2


@pytest.mark.parametrize("status,expected_calls", [(400, 1), (503, 2)])
def test_only_transient_http_errors_are_retried(status: int, expected_calls: int) -> None:
    calls: list[str] = []
    _text, stats = _client(lambda _r: httpx.Response(status), calls)._call("s", "u")

    assert stats.fallback
    assert stats.error == "HTTPStatusError"
    assert len(calls) == expected_calls


def test_invalid_json_gets_one_stricter_retry_then_heuristics() -> None:
    calls: list[str] = []
    result, stats = _client(lambda _r: _completion("not json"), calls).extract("my card was stolen")

    assert stats.fallback
    assert stats.error is not None and stats.error.startswith("invalid_json")
    assert result.confidence == 0.4  # heuristic extraction
    assert len(calls) == 2
    assert "not valid JSON" in calls[1]


def test_slow_server_is_cut_at_the_wall_clock_deadline() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        time.sleep(2)  # the mock ignores httpx timeouts, like a server that trickles bytes
        return _completion(json.dumps(EXTRACTION))

    llm = _client(handler, [], llm_timeout_seconds=0.2, llm_max_retries=1)
    t0 = time.perf_counter()
    result, stats = llm.extract("my card was stolen")
    elapsed = time.perf_counter() - t0

    assert stats.fallback
    assert stats.error == "TimeoutError"
    assert result.confidence == 0.4  # heuristic extraction
    assert elapsed < 1.0  # deadline 0.2 s x 2 attempts, far below the 2 s server delay


def test_reply_rejected_by_validator_uses_template() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content)["messages"][0]["content"]
        if "check a bank's customer reply" in system:
            return _completion('{"ok": false, "reason": "promises a refund"}')
        return _completion("We refunded you 1000 USD.")

    reply, stats = _client(handler, []).compose("refund me", {"outcome": "escalated"}, "en")

    assert stats.fallback
    assert "human agent" in reply
    assert "1000" not in reply


def test_malformed_provider_response_falls_back() -> None:
    _text, stats = _client(lambda _r: httpx.Response(200, json={"unexpected": 1}), [])._call(
        "s", "u"
    )

    assert stats.fallback
    assert stats.error == "KeyError"


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_provider": "local", "llm_base_url": ""},
        {"llm_provider": "anthropic", "anthropic_api_key": ""},
        {"llm_enabled": False},
    ],
)
def test_unconfigured_llm_is_not_available(overrides: dict[str, object]) -> None:
    llm = LLMClient(_settings(**overrides))

    _text, stats = llm._call("s", "u")

    assert not llm.available
    assert stats.error == "llm_disabled"


def test_complete_sends_the_temperature_to_both_providers() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _completion('{"es-MX": "hola"}')

    http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    text, stats = LLMClient(_settings(), http_client=http).complete("s", "u", 50, 0.7)
    assert (text, stats.fallback, bodies[0]["temperature"]) == ('{"es-MX": "hola"}', False, 0.7)

    sent: dict[str, object] = {}

    class Messages:
        def create(self, **kwargs: object) -> object:
            sent.update(kwargs)
            # The SDK's Usage carries the cache fields, None when nothing was cached.
            usage = type(
                "Usage",
                (),
                {
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "cache_creation_input_tokens": None,
                    "cache_read_input_tokens": None,
                },
            )()
            block = type("Block", (), {"type": "text", "text": "ok"})()
            return type("Message", (), {"content": [block], "usage": usage})()

    llm = LLMClient(_settings(llm_provider="anthropic", anthropic_api_key="k"))
    llm._client = type("Client", (), {"messages": Messages()})()
    text, stats = llm.complete("s", "u", 50, 1.0)
    # anthropic 1.x has no `temperature` argument; the body still carries it.
    assert (text, sent["extra_body"], "temperature" in sent) == ("ok", {"temperature": 1.0}, False)

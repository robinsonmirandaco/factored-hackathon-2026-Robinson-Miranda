"""Cache, spend cap and pace of the harness LLM calls (TRZ-45 CA10)."""

import json
from pathlib import Path

import httpx2
import pytest

from pipeline.llm_replay import (
    MAX_CALL_USD,
    Budget,
    Pace,
    ReplayCache,
    ReplayTransport,
    cost_of,
    request_key,
)

BODY = {"model": "claude-haiku-4-5-20251001", "messages": [{"role": "user", "content": "hola"}]}
USAGE = {
    "input_tokens": 1000,
    "output_tokens": 100,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 4000,
}


class Upstream(httpx2.BaseTransport):
    """A simulated API that counts its requests."""

    def __init__(self) -> None:
        self.requests = 0

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests += 1
        return httpx2.Response(200, json={"content": [], "usage": USAGE})


def send(transport: ReplayTransport, body: dict | None = None) -> httpx2.Response:
    with httpx2.Client(transport=transport) as client:
        return client.post("https://api.test/v1/messages", content=json.dumps(body or BODY))


def transport(
    tmp_path: Path, upstream: Upstream, repetition: int = 1, cap: float = 1.0
) -> ReplayTransport:
    return ReplayTransport(ReplayCache(tmp_path), repetition, Budget(cap), Pace(1000), upstream)


def test_a_repeated_request_is_answered_from_the_cache_with_its_first_cost(
    tmp_path: Path,
) -> None:
    upstream = Upstream()
    first = transport(tmp_path, upstream)
    send(first)
    again = transport(tmp_path, upstream)

    send(again)

    assert upstream.requests == 1
    assert (again.hits, again.cost_usd) == (1, first.cost_usd)
    assert again.budget.spent_usd == 0


def test_another_repetition_is_a_new_request(tmp_path: Path) -> None:
    upstream = Upstream()
    send(transport(tmp_path, upstream, repetition=1))

    send(transport(tmp_path, upstream, repetition=2))

    assert upstream.requests == 2


def test_the_key_ignores_the_order_of_the_json_keys() -> None:
    a = json.dumps({"model": "m", "messages": []}).encode()
    b = json.dumps({"messages": [], "model": "m"}).encode()

    assert request_key(a, 1) == request_key(b, 1)
    assert request_key(a, 1) != request_key(a, 2)


def test_a_request_that_may_not_fit_under_the_cap_is_refused_without_calling(
    tmp_path: Path,
) -> None:
    upstream = Upstream()
    capped = transport(tmp_path, upstream, cap=MAX_CALL_USD / 2)

    r = send(capped)

    assert r.status_code == 529
    assert (upstream.requests, capped.refused, capped.budget.spent_usd) == (0, 1, 0)


def test_the_cache_survives_a_new_process(tmp_path: Path) -> None:
    upstream = Upstream()
    send(transport(tmp_path, upstream))

    reloaded = ReplayCache(tmp_path)

    assert reloaded.get(request_key(json.dumps(BODY).encode(), 1)) is not None


def test_cost_is_the_list_price_with_cache_reads_at_a_tenth() -> None:
    # 1000 input + 0.1 * 4000 cache read = 1400 input tokens at 1 USD, 100 output at 5 USD.
    assert cost_of("claude-haiku-4-5-20251001", USAGE) == pytest.approx(0.0019)
    assert cost_of("claude-sonnet-5-5", USAGE) == pytest.approx(0.0038)


def test_the_wait_for_the_pace_is_measured() -> None:
    pace = Pace(1)

    assert pace.wait() < 0.05
    assert Pace(1000).wait() >= 0

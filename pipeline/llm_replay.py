"""Cache, spend cap and request pace for the LLM calls of the evaluation harness (TRZ-45 CA10).

The harness runs whole conversations, so the cache sits under the SDK, as an httpx2 transport:
the LLM client of TRAZO and the free agent send their real requests, and this transport answers
each one from the cache or forwards it to the API. The key is the hash of the request body (model,
prompt, messages, tools, temperature) and the repetition number, so repetition 2 is a new call
and a rerun of repetition 2 is free and identical. Answers are stored in DATA_DIR/eval, outside
git: they hold fragments of dataset messages.

The cache is also the spend ledger. Before a request that is not cached, the transport checks the
spend of the run against its cap and refuses the request unless it fits under the cap at the most
one request can cost, so a run never passes its budget. A refused request reaches the caller as
an HTTP error, which the systems handle like an outage. Requests to the API are paced to stay
under the account's rate limit.
"""

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from app.core.logging import get_logger

log = get_logger("pipeline.llm_replay")

CACHE_FILE = "harness_llm_cache.jsonl"

# Most one request can cost (a free agent call: ~9,000 input tokens and 600 output on Haiku is
# 0.012 USD); a request is refused unless it fits under the cap even at this cost.
MAX_CALL_USD = 0.02
# USD per million tokens at list price, checked on 2026-10-02. A cache write costs 1.25 times an
# input token and a read 0.1 times, on both models.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0),
}


class BudgetReached(RuntimeError):
    """The spend of the run reached its cap."""


@dataclass(frozen=True)
class Entry:
    """One cached answer.

    Attributes:
        key: Hash of the request and the repetition.
        status: HTTP status the API returned.
        body: Response body.
        latency_ms: Wall time of the request when it was first paid.
        cost_usd: Cost at list price when it was first paid.
    """

    key: str
    status: int
    body: dict[str, Any]
    latency_ms: int
    cost_usd: float


def request_key(body: bytes, repetition: int) -> str:
    """Hash of a request body and its repetition number.

    Args:
        body: JSON body the SDK sends.
        repetition: Repetition of the run (1, 2, 3).

    Returns:
        Hex SHA-256.
    """
    canonical = json.dumps(json.loads(body), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{repetition}\n{canonical}".encode()).hexdigest()


def cost_of(model: str, usage: dict[str, Any]) -> float:
    """Cost in USD of one response at list price.

    Args:
        model: Model id of the request.
        usage: `usage` object of the response.

    Returns:
        The cost; 0 for a model without a listed price.
    """
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    billed_in = (
        usage.get("input_tokens", 0)
        + 1.25 * (usage.get("cache_creation_input_tokens") or 0)
        + 0.1 * (usage.get("cache_read_input_tokens") or 0)
    )
    return (billed_in * price_in + usage.get("output_tokens", 0) * price_out) / 1e6


class Pace:
    """At most `per_minute` requests in any sliding minute, across threads."""

    def __init__(self, per_minute: int) -> None:
        """Keeps the limit.

        Args:
            per_minute: Requests allowed per minute.
        """
        self._per_minute = per_minute
        self._sent: list[float] = []
        self._lock = threading.Lock()

    def wait(self) -> float:
        """Blocks until one more request fits in the last minute.

        Returns:
            Seconds waited, which the harness leaves out of the system's latency.
        """
        start = time.monotonic()
        while True:
            with self._lock:
                now = time.monotonic()
                self._sent = [t for t in self._sent if now - t < 60]
                if len(self._sent) < self._per_minute:
                    self._sent.append(now)
                    return now - start
                pause = 60 - (now - self._sent[0])
            time.sleep(max(pause, 0.05))


class ReplayCache:
    """The cached answers of every harness run, appended to one JSONL file."""

    def __init__(self, folder: Path) -> None:
        """Loads the cache.

        Args:
            folder: DATA_DIR/eval.
        """
        self.path = folder / CACHE_FILE
        self._entries: dict[str, Entry] = {}
        self._lock = threading.Lock()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                e = Entry(**json.loads(line))
                self._entries[e.key] = e

    def get(self, key: str) -> Entry | None:
        """The cached answer of a key, if any."""
        return self._entries.get(key)

    def put(self, entry: Entry) -> None:
        """Stores a new answer in memory and on disk."""
        with self._lock:
            self._entries[entry.key] = entry
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")


class Budget:
    """The spend cap of one run, shared by the transports of all its cases."""

    def __init__(self, cap_usd: float) -> None:
        """Keeps the cap.

        Args:
            cap_usd: Most new spend the run may add.
        """
        self.cap_usd = cap_usd
        self.spent_usd = 0.0
        self.refused = 0
        self._lock = threading.Lock()

    def reserve(self) -> bool:
        """True when one more request fits under the cap at the most it can cost."""
        with self._lock:
            if self.spent_usd + MAX_CALL_USD > self.cap_usd:
                self.refused += 1
                return False
            return True

    def add(self, cost_usd: float) -> None:
        """Adds the cost of a paid request."""
        with self._lock:
            self.spent_usd += cost_usd


class ReplayTransport(httpx2.BaseTransport):
    """Answers the SDK requests of one case from the cache, or pays for them within the run cap."""

    def __init__(
        self,
        cache: ReplayCache,
        repetition: int,
        budget: Budget,
        pace: Pace,
        upstream: httpx2.BaseTransport | None = None,
    ) -> None:
        """Keeps the cache, the repetition and the cap.

        Args:
            cache: Shared cache.
            repetition: Repetition number of the run, part of the key.
            budget: Spend cap of the run.
            pace: Request pace shared by every case of the run.
            upstream: Transport to the API; tests pass a simulated one.
        """
        self.cache = cache
        self.repetition = repetition
        self.budget = budget
        self.pace = pace
        self.upstream = upstream or httpx2.HTTPTransport()
        self.calls = 0
        self.hits = 0
        self.refused = 0
        self.cost_usd = 0.0
        self.latency_ms = 0
        self.paced_ms = 0

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        """Serves one request.

        Args:
            request: Request built by the SDK.

        Returns:
            The cached or fresh response; a 529 when the cap is reached, so the caller treats it
            as an outage and nothing past the cap is paid.
        """
        body = request.read()
        key = request_key(body, self.repetition)
        self.calls += 1
        cached = self.cache.get(key)
        if cached is not None:
            # Cost and latency are the ones first paid, so a rerun reports the same figures.
            self.hits += 1
            self.cost_usd += cached.cost_usd
            self.latency_ms += cached.latency_ms
            return httpx2.Response(cached.status, json=cached.body)
        if not self.budget.reserve():
            self.refused += 1
            log.warning("harness_budget_reached", spent=round(self.budget.spent_usd, 4))
            return httpx2.Response(
                529, json={"type": "error", "error": {"type": "overloaded_error"}}
            )
        self.paced_ms += int(self.pace.wait() * 1000)
        t0 = time.perf_counter()
        response = self.upstream.handle_request(request)
        response.read()
        latency_ms = int((time.perf_counter() - t0) * 1000)
        data = response.json()
        self.latency_ms += latency_ms
        if response.status_code != 200:
            # Errors are not cached: a rerun retries them.
            return httpx2.Response(response.status_code, json=data, headers=response.headers)
        cost = cost_of(json.loads(body)["model"], data.get("usage", {}))
        self.cache.put(Entry(key, response.status_code, data, latency_ms, cost))
        self.budget.add(cost)
        self.cost_usd += cost
        return httpx2.Response(response.status_code, json=data)

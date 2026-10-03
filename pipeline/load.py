"""`make load`: how many simultaneous cases the service serves before the p95 of a turn degrades.

TRZ-40 CA7, design 11.7. Runs against an isolated local stack (docker compose project `load`,
`docker-compose.load.yml`), never against the public URL: its per-address limit would cut the
test, and the demo belongs to the judges. Each run starts from a fresh database seeded with the
synthetic fixture (seed 42) and removes the stack at the end.

python -m pipeline.load run --scenario rules|simulated|real [--levels 1,2,4] [--budget 0.25]
python -m pipeline.load mock-llm [--port 8790]
python -m pipeline.load report

Scenarios: `rules` turns the LLM off (every turn takes the deterministic path, the ceiling of the
service without any wait on the LLM); `simulated` points the service at the simulated LLM below,
which answers with the latency measured for Haiku in TRZ-12 (p50 1.5 s, p95 2.4 s); `real` calls
Haiku within a budget, to see the limit of the provider quota.

Each virtual customer runs one case after another: log in with the demo code, report a charge of
its own, answer each step (still not recognized, confirm the pending action, pick an option) with
a think time between turns, and log out. The concurrency of a step is the number of cases in
progress at once. A step holds when the p95 of /chat is at most P95_FACTOR times the p95 at
concurrency 1 and fewer than MAX_ERROR_RATE of the requests fail; the capacity is the highest
concurrency that holds. The criterion was fixed before the first run.

Results go to eval/load/runs.jsonl (aggregates only, no message or row of the service) and the
report to docs/reports/carga.md.
"""

import argparse
import asyncio
import json
import logging
import math
import os
import platform
import random
import re
import statistics
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine, make_url, text

from app.adapters.ingest.synthetic import generate
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger

log = get_logger("pipeline.load")

SCENARIOS = ("rules", "simulated", "real")
PROJECT = "load"
COMPOSE = ["docker", "compose", "-p", PROJECT, "-f", "docker-compose.yml"]
COMPOSE += ["-f", "docker-compose.load.yml"]
DB_PORT = 5442
API_PORT = 8010
MOCK_PORT = 8790
SEED = 42
# Synthetic customers of the load stack, as many as the cohort; docker-compose.load.yml seeds them.
CUSTOMERS = 5000

LEVELS = (1, 2, 4, 8, 16, 32)
REAL_LEVELS = (1, 2, 4, 8)
WARMUP_SECONDS = 15.0
MEASURE_SECONDS = 60.0
REAL_MEASURE_SECONDS = 45.0
# Seconds a customer takes to read a reply and press the next button, drawn uniformly.
THINK_SECONDS = (1.5, 2.5)
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_TURNS = 6

P95_FACTOR = 1.5
MAX_ERROR_RATE = 0.01
BUDGET_USD = 0.25

# Limits of this Mac, so the test never takes it down; a step past them is a limit of the
# machine, not of the service.
HOST_LOAD_PER_CPU = 0.9
DOCKER_MEMORY_FRACTION = 0.875
# Percent of memory macOS reports free (kern.memorystatus_level), which counts what it can
# reclaim; free pages alone are low even at rest.
HOST_MEMORY_FREE_PCT = 10
GENERATOR_LAG_MS = 50.0

# Latency of Haiku measured on the development split (TRZ-12): p50 1.5 s, p95 2.4 s.
MOCK_MEDIAN_SECONDS = 1.5
MOCK_SIGMA = math.log(2.4 / 1.5) / 1.645

RUNS_PATH = Path("eval/load/runs.jsonl")
REPORT_PATH = Path("docs/reports/carga.md")
ENDPOINTS = ("otp_request", "otp_verify", "chat", "logout")
MESSAGE = re.compile(r"cargo de (?P<amount>[\d.]+) (?P<currency>[A-Z]{3}) en (?P<merchant>.+)$")


def percentile(values: list[float], q: float) -> float | None:
    """The q-th percentile with linear interpolation, or None for no values.

    Args:
        values: Samples.
        q: Percentile between 0 and 100.

    Returns:
        The percentile, or None when there is no sample.
    """
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q / 100
    low = math.floor(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def message_for(amount: float, currency: str, merchant: str) -> str:
    """The message a virtual customer writes about one of its charges.

    Args:
        amount: Amount of the charge.
        currency: Its currency.
        merchant: Its merchant.

    Returns:
        A dispute message in Spanish.
    """
    return f"No reconozco un cargo de {amount} {currency} en {merchant}"


def mock_reading(message: str) -> dict[str, Any]:
    """The comprehension the simulated LLM returns for a message of `message_for`.

    Args:
        message: The redacted customer message.

    Returns:
        A ComprehensionReading as JSON, with literal evidence for each clue; for any other
        message, an out of scope reading with no clue.
    """
    empty = {"amount": None, "date": None, "merchant_hint": None, "channel_hint": None}
    empty["card_in_possession"] = None
    found = MESSAGE.search(message)
    if not found:
        return {"intent": "out_of_scope", **empty, "language": "es-CO"}
    amount_text = f"{found['amount']} {found['currency']}"
    return {
        "intent": "unrecognized_charge",
        **empty,
        "amount": {
            "value": float(found["amount"]),
            "currency": found["currency"],
            "approximate": False,
            "evidence": amount_text,
        },
        "merchant_hint": {"value": found["merchant"], "evidence": found["merchant"]},
        "language": "es-CO",
    }


def mock_app(seed: int = SEED, median_seconds: float = MOCK_MEDIAN_SECONDS) -> Any:
    """The simulated Anthropic Messages API of the `simulated` scenario.

    A request with an output schema is a comprehension call and gets `mock_reading`; any other
    gets a short reply with no figure, which the fact checker lets through. Each answer waits a
    lognormal latency with the median and p95 measured for Haiku. It counts the calls.

    Args:
        seed: Seed of the latency draws.
        median_seconds: Median latency; the p95 keeps the ratio measured for Haiku.

    Returns:
        A Starlette application.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    rng = random.Random(seed)
    calls: Counter[str] = Counter()

    async def messages(request: Request) -> JSONResponse:
        body = await request.json()
        user = body["messages"][0]["content"]
        if "output_config" in body:
            calls["comprehension"] += 1
            content = json.dumps(mock_reading(user.split("Message: ", 1)[-1]))
        else:
            calls["other"] += 1
            content = "Entendido, revisamos tu caso."
        await asyncio.sleep(rng.lognormvariate(math.log(median_seconds), MOCK_SIGMA))
        return JSONResponse(
            {
                "id": "msg_load",
                "type": "message",
                "role": "assistant",
                "model": body.get("model", "simulated"),
                "content": [{"type": "text", "text": content}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 400, "output_tokens": 60},
            }
        )

    async def counted(_request: Request) -> JSONResponse:
        return JSONResponse(dict(calls))

    return Starlette(
        routes=[Route("/v1/messages", messages, methods=["POST"]), Route("/calls", counted)]
    )


@dataclass(frozen=True)
class Charge:
    """A charge a virtual customer disputes; synthetic, never a row of the dataset."""

    document_type: str
    document_number: str
    message: str


def charges(settings: Settings) -> list[Charge]:
    """One approved purchase of each active synthetic customer of the load stack.

    One case per customer: a customer with a case still open has any new dispute escalated
    (open_dispute_last_90d of the policy), so reusing customers would turn every later case
    into an escalation and change the work of a turn as the test goes on. Without the
    deliberately invalid rows of the fixture, which the seed sends to quarantine.

    Args:
        settings: Settings with the simulated now the fixture was seeded with.

    Returns:
        The charges in the order the virtual customers take them.
    """
    customers, _products, transactions = generate(
        settings.trazo_now, seed=SEED, n_customers=CUSTOMERS, dirty=False
    )
    active = {c["customer_id"]: c for c in customers if c["customer_status"] == "Active"}
    first: dict[str, dict[str, Any]] = {}
    for t in transactions:
        cid = t["customer_id"]
        if cid in active and cid not in first and t["transaction_status"] == "Approved":
            first[cid] = t
    return [
        Charge(
            active[cid]["document_type"],
            active[cid]["document_number"],
            message_for(t["amount"], t["currency"], t["merchant_name"]),
        )
        for cid, t in first.items()
    ]


@dataclass
class Sample:
    """One request of a virtual customer."""

    endpoint: str
    at: float
    ms: float
    ok: bool
    error: str | None = None
    server_ms: int | None = None
    fallback: bool | None = None
    outcome: str | None = None
    # The first turn of a case, the only one with a message to understand.
    free_text: bool = False


@dataclass
class Resources:
    """Samples of the machine and the containers during a step."""

    api_cpu: list[float] = field(default_factory=list)
    db_cpu: list[float] = field(default_factory=list)
    memory_bytes: list[float] = field(default_factory=list)
    memory_limit: float = 0.0
    host_load_per_cpu: list[float] = field(default_factory=list)
    host_memory_free_pct: list[float] = field(default_factory=list)
    generator_lag_ms: list[float] = field(default_factory=list)


def _bytes(size: str) -> float:
    units = {"B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30, "kB": 1e3, "MB": 1e6}
    units["GB"] = 1e9
    number, unit = re.fullmatch(r"([\d.]+)\s*([A-Za-z]+)", size.strip()).groups()  # type: ignore[union-attr]
    return float(number) * units[unit]


def host_memory_free_pct() -> float | None:
    """Percent of memory macOS reports free; None on other systems."""
    if platform.system() != "Darwin":
        return None
    out = subprocess.run(
        ["sysctl", "-n", "kern.memorystatus_level"], capture_output=True, text=True, check=True
    )
    return float(out.stdout)


def sample_resources(res: Resources, stop: threading.Event) -> None:
    """Samples docker stats of the stack and the load of the host until `stop` is set.

    Args:
        res: Where the samples go.
        stop: Set when the step ends.
    """
    names = [f"{PROJECT}-api-1", f"{PROJECT}-db-1"]
    while not stop.is_set():
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", *names],
            capture_output=True,
            text=True,
        ).stdout
        rows = {r["Name"]: r for r in (json.loads(line) for line in out.splitlines() if line)}
        if len(rows) == 2:
            res.api_cpu.append(float(rows[names[0]]["CPUPerc"].rstrip("%")))
            res.db_cpu.append(float(rows[names[1]]["CPUPerc"].rstrip("%")))
            used = 0.0
            for r in rows.values():
                mem, limit = r["MemUsage"].split("/")
                used += _bytes(mem)
                res.memory_limit = _bytes(limit)
            res.memory_bytes.append(used)
        res.host_load_per_cpu.append(os.getloadavg()[0] / (os.cpu_count() or 1))
        free = host_memory_free_pct()
        if free is not None:
            res.host_memory_free_pct.append(free)
        stop.wait(1.0)


class Step:
    """The virtual customers of one concurrency level."""

    def __init__(self, client: httpx.AsyncClient, queue: list[Charge], demo_code: str) -> None:
        """Binds the HTTP client and the shared list of charges.

        Args:
            client: Client of the stack under test.
            queue: Charges not yet taken; each case pops one.
            demo_code: The fixed demo code of the stack.
        """
        self.client = client
        self.queue = queue
        self.demo_code = demo_code
        self.samples: list[Sample] = []
        self.cases = 0

    async def _post(
        self, endpoint: str, path: str, free_text: bool = False, **kwargs: Any
    ) -> httpx.Response | None:
        t0 = time.perf_counter()
        try:
            r = await self.client.post(path, **kwargs)
        except httpx.HTTPError as exc:
            ms = (time.perf_counter() - t0) * 1000
            self.samples.append(Sample(endpoint, t0, ms, False, type(exc).__name__))
            return None
        ms = (time.perf_counter() - t0) * 1000
        ok = r.status_code < 400
        sample = Sample(endpoint, t0, ms, ok, None if ok else str(r.status_code))
        sample.free_text = free_text
        if endpoint == "chat" and ok:
            body = r.json()
            sample.server_ms = body["latency_ms"]
            sample.fallback = body["llm_fallback"]
            sample.outcome = body["outcome"]
        self.samples.append(sample)
        return r if ok else None

    async def case(self, rng: random.Random) -> None:
        """One case of one virtual customer, from login to logout."""
        charge = self.queue.pop(0)
        doc = {"document_type": charge.document_type, "document_number": charge.document_number}
        if await self._post("otp_request", "/auth/otp/request", json=doc) is None:
            return
        r = await self._post("otp_verify", "/auth/otp/verify", json={**doc, "code": self.demo_code})
        if r is None:
            return
        auth = {"authorization": f"Bearer {r.json()['access_token']}"}
        body: dict[str, Any] | None = {"message": charge.message}
        for turn in range(MAX_TURNS):
            if body is None:
                break
            r = await self._post("chat", "/chat", turn == 0, json=body, headers=auth)
            if r is None:
                break
            body = next_turn(r.json())
            if body is not None:
                await asyncio.sleep(rng.uniform(*THINK_SECONDS))
        await self._post("logout", "/auth/logout", headers=auth)
        self.cases += 1

    async def customer(self, rng: random.Random, until: float) -> None:
        """Runs cases back to back until the step ends or no charge is left."""
        while time.perf_counter() < until and self.queue:
            await self.case(rng)


def next_turn(out: dict[str, Any]) -> dict[str, Any] | None:
    """What the customer answers to a /chat reply, or None when the case needs nothing more.

    Args:
        out: The body of the /chat response.

    Returns:
        The body of the next /chat request, or None.
    """
    case_id = out["case_id"]
    if out["outcome"] == "recognizing":
        return {
            "message": "Sigo sin reconocerlo",
            "case_id": case_id,
            "recognition": "not_recognized",
        }
    if out.get("pending_action"):
        action = out["pending_action"]["action_id"]
        return {"message": "Sí", "case_id": case_id, "confirm_action_id": action}
    if out.get("options"):
        return {
            "message": "Es este",
            "case_id": case_id,
            "option": out["options"][0]["transaction_id"],
        }
    return None


async def _lag(samples: list[float], until: float) -> None:
    # How late the event loop of the generator wakes up: when it is late, the generator, not
    # the service, is what slows the requests down.
    while time.perf_counter() < until:
        t0 = time.perf_counter()
        await asyncio.sleep(0.1)
        samples.append((time.perf_counter() - t0 - 0.1) * 1000)


def summarize(
    level: int, samples: list[Sample], cases: int, res: Resources, cpu: float
) -> dict[str, Any]:
    """The aggregates of one step.

    Args:
        level: Concurrency of the step.
        samples: Requests sent in the measured window.
        cases: Cases finished in the window.
        res: Resource samples of the window.
        cpu: Share of one core the generator process used.

    Returns:
        Counts, percentiles and resource peaks, with no message and no id.
    """
    chat = [s for s in samples if s.endpoint == "chat"]
    ok_chat = [s for s in chat if s.ok]
    llm_turns = [s for s in ok_chat if s.free_text]
    errors = Counter(s.error for s in samples if not s.ok)
    latency = {}
    for e in ENDPOINTS:
        ms = [s.ms for s in samples if s.endpoint == e and s.ok]
        latency[e] = {f"p{q}": _round(percentile(ms, q)) for q in (50, 95, 99)}
    server = [float(s.server_ms) for s in ok_chat if s.server_ms is not None]

    def peak(xs: list[float]) -> float | None:
        return _round(max(xs)) if xs else None

    return {
        "level": level,
        "cases": cases,
        "requests": len(samples),
        "chat_turns": len(chat),
        "errors": dict(errors),
        "error_rate": round(sum(errors.values()) / len(samples), 4) if samples else None,
        "latency_ms": latency,
        "chat_server_ms": {f"p{q}": _round(percentile(server, q)) for q in (50, 95)},
        "llm_fallback_rate": round(sum(bool(s.fallback) for s in llm_turns) / len(llm_turns), 4)
        if llm_turns
        else None,
        "outcomes": dict(Counter(s.outcome for s in ok_chat)),
        "api_cpu_pct": {"mean": _round(_mean(res.api_cpu)), "max": peak(res.api_cpu)},
        "db_cpu_pct": {"mean": _round(_mean(res.db_cpu)), "max": peak(res.db_cpu)},
        "docker_memory_peak_fraction": round(max(res.memory_bytes) / res.memory_limit, 3)
        if res.memory_bytes and res.memory_limit
        else None,
        "host_load_per_cpu_max": peak(res.host_load_per_cpu),
        "host_memory_free_pct_min": _round(min(res.host_memory_free_pct))
        if res.host_memory_free_pct
        else None,
        "generator_lag_ms_p95": _round(percentile(res.generator_lag_ms, 95)),
        "generator_cpu_pct": round(cpu * 100, 1),
    }


def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def _round(x: float | None) -> float | None:
    return None if x is None else round(x, 1)


def mac_limited(step: dict[str, Any]) -> list[str]:
    """The limits of the Mac a step crossed; empty when the machine had room.

    Args:
        step: A step from `summarize`.

    Returns:
        The names of the crossed limits.
    """
    crossed = []
    if (step["host_load_per_cpu_max"] or 0) > HOST_LOAD_PER_CPU:
        crossed.append("host_load")
    if (step["docker_memory_peak_fraction"] or 0) > DOCKER_MEMORY_FRACTION:
        crossed.append("docker_memory")
    free = step["host_memory_free_pct_min"]
    if free is not None and free < HOST_MEMORY_FREE_PCT:
        crossed.append("host_memory")
    if (step["generator_lag_ms_p95"] or 0) > GENERATOR_LAG_MS:
        crossed.append("generator_lag")
    return crossed


def verdicts(steps: list[dict[str, Any]]) -> tuple[int | None, list[dict[str, Any]]]:
    """Applies the criterion to each step and finds the capacity.

    A step holds when the p95 of /chat is at most P95_FACTOR times that of the first step and
    its error rate is below MAX_ERROR_RATE. The capacity is the highest level of the run of
    holding steps from the first; a step that crossed a limit of the Mac does not count.

    Args:
        steps: Steps in increasing concurrency, the first at concurrency 1.

    Returns:
        The capacity, or None when not even the first step holds, and each step with `holds`,
        `p95_ratio` and `mac_limits`.
    """
    if not steps:
        return None, []
    base = steps[0]["latency_ms"]["chat"]["p95"]
    capacity = None
    judged = []
    broken = False
    for step in steps:
        p95 = step["latency_ms"]["chat"]["p95"]
        ratio = round(p95 / base, 2) if p95 is not None and base else None
        limits = mac_limited(step)
        holds = (
            ratio is not None
            and ratio <= P95_FACTOR
            and (step["error_rate"] or 0) < MAX_ERROR_RATE
            and not limits
        )
        if holds and not broken:
            capacity = step["level"]
        broken = broken or not holds
        judged.append({**step, "p95_ratio": ratio, "holds": holds, "mac_limits": limits})
    return capacity, judged


class Stack:
    """The isolated compose project of the load test."""

    def __init__(self, scenario: str, settings: Settings) -> None:
        """Prepares the environment of the scenario.

        Args:
            scenario: One of SCENARIOS.
            settings: Local settings, for the owner URL and the demo code.
        """
        self.env = {**os.environ, "DB_PORT": str(DB_PORT), "API_PORT": str(API_PORT)}
        self.env["LOAD_LLM_ENABLED"] = "false" if scenario == "rules" else "true"
        if scenario == "simulated":
            self.env["LOAD_ANTHROPIC_BASE_URL"] = f"http://host.docker.internal:{MOCK_PORT}"
            self.env["LOAD_ANTHROPIC_API_KEY"] = "simulated"
        if not settings.admin_database_url:
            raise SystemExit("ADMIN_DATABASE_URL is not set: the cost of the run cannot be read")
        url = make_url(settings.admin_database_url).set(host="localhost", port=DB_PORT)
        self.admin_url = url.render_as_string(hide_password=False)

    def _compose(self, *args: str) -> None:
        subprocess.run([*COMPOSE, *args], env=self.env, check=True, capture_output=True)

    def up(self) -> None:
        """Starts a fresh stack: removes the volume of a previous run, builds and waits."""
        self._compose("down", "-v")
        self._compose("up", "-d", "--build", "--wait")

    def down(self) -> None:
        """Removes the stack and its volume."""
        self._compose("down", "-v")

    def cost_usd(self) -> float:
        """LLM spend recorded in the audit log of the stack."""
        engine = create_engine(self.admin_url)
        try:
            with engine.connect() as conn:
                total = conn.execute(text("SELECT coalesce(sum(cost_usd), 0) FROM audit_log"))
                return float(total.scalar_one())
        finally:
            engine.dispose()

    def llm_usage(self) -> dict[str, int]:
        """Audit rows with LLM statistics, their tokens and the cases they belong to."""
        engine = create_engine(self.admin_url)
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT count(*), coalesce(sum(input_tokens), 0), "
                        "coalesce(sum(output_tokens), 0), count(DISTINCT case_id) "
                        "FROM audit_log WHERE model IS NOT NULL"
                    )
                ).one()
        finally:
            engine.dispose()
        return dict(zip(("rows", "input_tokens", "output_tokens", "cases"), row, strict=True))

    def llm_failures(self) -> dict[str, int]:
        """Failed LLM attempts by error, from the API log of the stack."""
        out = subprocess.run(["docker", "logs", f"{PROJECT}-api-1"], capture_output=True, text=True)
        counts: Counter[str] = Counter()
        for line in (out.stdout + out.stderr).splitlines():
            if '"llm_call_failed"' in line:
                counts[json.loads(line).get("error", "unknown")] += 1
        return dict(counts)


def rate_limits(settings: Settings) -> dict[str, str]:
    """The rate limits of the API key, from the headers of one minimal call to the primary model.

    Args:
        settings: Settings with the key and the model.

    Returns:
        The anthropic-ratelimit-* limit headers.
    """
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": settings.llm_model_primary,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ok"}],
        },
        timeout=10.0,
    )
    r.raise_for_status()
    return {
        k: v
        for k, v in r.headers.items()
        if k.startswith("anthropic-ratelimit-") and k.endswith("-limit")
    }


async def run_step(
    level: int,
    queue: list[Charge],
    settings: Settings,
    warmup: float,
    measure: float,
    rng_seed: int,
) -> dict[str, Any]:
    """Runs one concurrency level: a warm-up window, then the measured window.

    Args:
        level: Virtual customers at once.
        queue: Charges not yet taken.
        settings: Settings with the demo code.
        warmup: Seconds before measuring.
        measure: Seconds measured.
        rng_seed: Seed of the think times.

    Returns:
        The step from `summarize`.
    """
    limits = httpx.Limits(max_connections=level * 2, max_keepalive_connections=level * 2)
    async with httpx.AsyncClient(
        base_url=f"http://localhost:{API_PORT}", timeout=REQUEST_TIMEOUT_SECONDS, limits=limits
    ) as client:
        step = Step(client, queue, settings.demo_otp_code)
        start = time.perf_counter()
        window = start + warmup
        until = window + measure
        res = Resources()
        stop = threading.Event()
        sampler = threading.Thread(target=sample_resources, args=(res, stop), daemon=True)
        lag: list[float] = []
        rngs = [random.Random(rng_seed * 1000 + i) for i in range(level)]
        tasks = [asyncio.create_task(step.customer(rngs[i], until)) for i in range(level)]
        await asyncio.sleep(warmup)
        cases_before = step.cases
        sampler.start()
        cpu_window = time.process_time()
        await _lag(lag, until)
        cpu_used = (time.process_time() - cpu_window) / measure
        cases = step.cases - cases_before
        await asyncio.gather(*tasks)
        stop.set()
        sampler.join()
        res.generator_lag_ms = lag
    measured = [s for s in step.samples if window <= s.at < until]
    return {**summarize(level, measured, cases, res, cpu_used), "cases_total": step.cases}


def run(scenario: str, levels: tuple[int, ...], budget: float) -> dict[str, Any]:
    """Runs a scenario level by level on a fresh stack and records it.

    Stops at the first level that does not hold, crosses a limit of the Mac or, in `real`,
    reaches the budget.

    Args:
        scenario: One of SCENARIOS.
        levels: Concurrency levels in increasing order, starting at 1.
        budget: USD of LLM spend allowed in `real`.

    Returns:
        The recorded run.
    """
    source = source_state()
    settings = Settings()
    stack = Stack(scenario, settings)
    mock = None
    limits: dict[str, str] = {}
    if scenario == "real":
        if not settings.anthropic_api_key:
            raise SystemExit("ANTHROPIC_API_KEY is not set: the real scenario needs it")
        limits = rate_limits(settings)
    if scenario == "simulated":
        mock = subprocess.Popen(
            ["uv", "run", "--frozen", "python", "-m", "pipeline.load", "mock-llm"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    queue = charges(settings)
    available = len(queue)
    steps: list[dict[str, Any]] = []
    stopped = "all levels run"
    started = datetime.now(UTC)
    measure = REAL_MEASURE_SECONDS if scenario == "real" else MEASURE_SECONDS
    try:
        stack.up()
        log.info("load_stack_up", scenario=scenario)
        for level in levels:
            step = asyncio.run(
                run_step(level, queue, settings, WARMUP_SECONDS, measure, SEED + level)
            )
            if scenario == "real":
                step["cost_usd_so_far"] = round(stack.cost_usd(), 4)
            steps.append(step)
            capacity, judged = verdicts(steps)
            last = judged[-1]
            log.info(
                "load_step",
                scenario=scenario,
                concurrency=level,
                holds=last["holds"],
                chat_p95=last["latency_ms"]["chat"]["p95"],
                ratio=last["p95_ratio"],
                error_rate=last["error_rate"],
                mac_limits=last["mac_limits"],
            )
            if last["mac_limits"]:
                stopped = (
                    f"level {level} crossed a limit of the Mac: {', '.join(last['mac_limits'])}"
                )
                break
            if not last["holds"]:
                stopped = f"level {level} did not hold"
                break
            if scenario == "real" and step["cost_usd_so_far"] >= budget * 0.8:
                stopped = f"level {level} reached 80% of the budget"
                break
            if not queue:
                stopped = f"level {level} used every charge of the fixture"
                break
        llm_calls = None
        if mock is not None:
            llm_calls = httpx.get(f"http://localhost:{MOCK_PORT}/calls", timeout=5.0).json()
        cost = stack.cost_usd()
        usage = stack.llm_usage()
        failures = stack.llm_failures()
    finally:
        stack.down()
        if mock is not None:
            mock.terminate()
            mock.wait()
    capacity, judged = verdicts(steps)
    record = {
        "scenario": scenario,
        "started_at": started.isoformat(timespec="seconds"),
        **source,
        "model": settings.llm_model_primary if scenario == "real" else None,
        "seed": SEED,
        "fixture": {
            "generator": "synthetic",
            "seed": SEED,
            "charges_available": available,
            "charges_used": available - len(queue),
        },
        "think_seconds": list(THINK_SECONDS),
        "warmup_seconds": WARMUP_SECONDS,
        "measure_seconds": measure,
        "criterion": {"p95_factor": P95_FACTOR, "max_error_rate": MAX_ERROR_RATE},
        "capacity": capacity,
        "stopped": stopped,
        "steps": judged,
        "llm_calls": llm_calls,
        "llm_failures": failures,
        "llm_usage": usage,
        "cost_usd": round(cost, 4),
        "rate_limits": limits,
        "machine": machine(),
    }
    RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RUNS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def source_state() -> dict[str, Any]:
    """The commit the run starts from, and whether the tree has other changes.

    Changes to the outputs of make load do not count; any other would make the run not
    reproducible from the commit.
    """
    git = ["git", "status", "--porcelain", "--", ".", ":!eval/load", ":!docs/reports/carga.md"]
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    status = subprocess.run(git, capture_output=True, text=True, check=True)
    return {"commit": head.stdout.strip(), "dirty": bool(status.stdout.strip())}


def machine() -> dict[str, Any]:
    """CPU, memory and Docker limits of the machine the run used."""
    info = json.loads(
        subprocess.run(
            ["docker", "info", "--format", "{{json .}}"], capture_output=True, text=True, check=True
        ).stdout
    )
    mem = None
    if platform.system() == "Darwin":
        mem = int(
            subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout
        )
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "memory_gib": round(mem / (1 << 30), 1) if mem else None,
        "docker_cpus": info.get("NCPU"),
        "docker_memory_gib": round(info.get("MemTotal", 0) / (1 << 30), 1),
    }


def ceiling(limits: dict[str, str], usage: dict[str, int], cases: int) -> dict[str, float] | None:
    """Cases per minute each rate limit of the key allows, from the use per case of a run.

    Args:
        limits: The anthropic-ratelimit-*-limit headers.
        usage: Audit rows with LLM statistics and their tokens.
        cases: Cases the run finished, warm-up included, as `usage` counts them.

    Returns:
        Cases per minute by limit, or None without limits or cases.
    """
    if not limits or not cases or not usage.get("rows"):
        return None
    per_case = {
        "requests": usage["rows"] / cases,
        "input-tokens": usage["input_tokens"] / cases,
        "output-tokens": usage["output_tokens"] / cases,
    }
    out = {}
    for name, used in per_case.items():
        limit = limits.get(f"anthropic-ratelimit-{name}-limit")
        if limit and used:
            out[name] = round(float(limit) / used, 1)
    return out


def _cell(x: Any) -> str:
    return "n/a" if x is None else str(x)


def report(path: Path = RUNS_PATH, out: Path = REPORT_PATH) -> str:
    """Writes the load report from the last recorded run of each scenario.

    Args:
        path: Recorded runs.
        out: Report to write.

    Returns:
        The report text.
    """
    runs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    last = {r["scenario"]: r for r in runs}
    lines = [
        "# Load test: simultaneous cases before the p95 of a turn degrades",
        "",
        "Generated by `make report-load` (story TRZ-40 CA7; design 11.7) from the runs recorded in "
        "`eval/load/runs.jsonl`. Aggregates only: no message or row is shown.",
        "",
        "## How it was measured",
        "",
        "- **Where.** An isolated local stack (`docker compose -p load` with "
        "`docker-compose.load.yml`): one API process (uvicorn, one worker) and Postgres 16 in "
        "Docker on the machine below, never the public URL. Each run starts from an empty "
        "database seeded with the synthetic fixture and removes the stack at the end.",
        "- **Login limits lifted in that stack only.** Every virtual customer logs in from the "
        "same address, so the per-address limit (30 per 15 minutes in the public URL) and the "
        "per-document limit are raised to 1,000,000 there. The public URL keeps both.",
        f"- **Data.** The synthetic fixture (seed 42) with {CUSTOMERS:,} customers, as many as "
        "the cohort of design 11.7, and 25 transactions each; not the cohort itself, see the "
        "note on the fixture below. Each case is a customer of its own: a customer with a case "
        "open has any new dispute escalated (`open_dispute_last_90d`), so reusing customers "
        "would turn every later case into an escalation and change the work of a turn.",
        f"- **A case.** Log in with the demo code, report a charge of one's own, press \"still not "
        f'recognized", confirm each pending action (register, block the card) or pick the first '
        f"option, then log out. Think time between turns: {THINK_SECONDS[0]} to "
        f"{THINK_SECONDS[1]} s. The concurrency is the number of cases in progress at once.",
        f"- **Steps.** {WARMUP_SECONDS:.0f} s of warm-up, then a measured window; levels double "
        "from 1 until one does not hold.",
        f"- **Criterion, fixed before the first run.** A level holds when the p95 of `/chat` "
        f"measured by the client is at most {P95_FACTOR} times the p95 at concurrency 1 and fewer "
        f"than {MAX_ERROR_RATE:.0%} of the requests fail (status 4xx or 5xx, or no answer in "
        f"{REQUEST_TIMEOUT_SECONDS:.0f} s). The capacity is the highest level that holds.",
        "- **Limits of the machine.** A level does not count when the host load per CPU passes "
        f"{HOST_LOAD_PER_CPU}, Docker memory passes {DOCKER_MEMORY_FRACTION:.0%} of its limit, the "
        f"memory macOS reports free falls under {HOST_MEMORY_FREE_PCT}%, or the event loop of "
        f"the load generator wakes up more than {GENERATOR_LAG_MS:.0f} ms late at the p95 "
        "(the generator, not the service, would be the bottleneck).",
        "",
        "Scenarios:",
        "",
        "- `rules`: LLM off, every turn on the deterministic path. The ceiling of the service "
        "without any wait on the LLM.",
        f"- `simulated`: a local simulated Anthropic API answering with a lognormal latency of "
        f"median {MOCK_MEDIAN_SECONDS} s and p95 2.4 s, the latency of Haiku measured in TRZ-12. "
        "The capacity of the service with realistic LLM waits.",
        "- `real`: Haiku, a short sample within a budget. Shows whether the quota of the provider "
        "binds before the service does.",
        "",
    ]
    for scenario in SCENARIOS:
        r = last.get(scenario)
        if r is None:
            continue
        lines += _scenario_section(r)
    lines += [
        "## Note on the fixture",
        "",
        "The fixture is synthetic, not the cohort, which matters little here: the work of "
        "a turn depends on the rows of one customer, not on the size of the tables. Each query "
        "of a turn filters by the customer of the session (row level security), and the tables "
        "it reads have an index on the customer (`ix_transactions_customer_date`, "
        "`ix_products_customer`, `ix_cases_customer`, `ix_audit_log_customer`). Per customer "
        "the two are close: 25 transactions each in the fixture; 144,475 transactions of 5,000 "
        "customers in the cohort, about 29 each, or 32 among the 4,451 with any "
        "(`docs/reports/cohorte.md`). What the fixture does not show is a cold cache of a larger "
        "database.",
        "",
    ]
    text_out = "\n".join(lines)
    out.write_text(text_out, encoding="utf-8")
    return text_out


def _scenario_section(r: dict[str, Any]) -> list[str]:
    m = r["machine"]
    lines = [
        f"## Scenario `{r['scenario']}`",
        "",
        f"- Run {r['started_at']}, commit `{r['commit'][:9]}`"
        + (" with uncommitted changes" if r.get("dirty") else "")
        + f", seed {r['seed']}"
        + (f", model `{r['model']}`" if r["model"] else ""),
        f"- Machine: {m['system']} {m['machine']}, {m['cpus']} CPUs, {m['memory_gib']} GiB; "
        f"Docker {m['docker_cpus']} CPUs, {m['docker_memory_gib']} GiB",
        f"- Measured window per level: {r['measure_seconds']:.0f} s. Charges used: "
        f"{r['fixture']['charges_used']} of {r['fixture']['charges_available']}.",
        f"- **Capacity: {_cell(r['capacity'])} simultaneous cases.** Stopped: {r['stopped']}.",
        "",
        "| Cases at once | Cases done | /chat turns | /chat p50 ms | /chat p95 ms | /chat p99 ms "
        "| p95 / p95 at 1 | Server p95 ms | Error rate | LLM fallback "
        "| API CPU % mean / max | DB CPU % max "
        "| Docker mem | Host load per CPU | Generator lag p95 ms | Holds |",
        "| " + " | ".join(["---"] * 16) + " |",
    ]
    for s in r["steps"]:
        chat = s["latency_ms"]["chat"]
        holds = "yes" if s["holds"] else "no"
        if s["mac_limits"]:
            holds += f" (Mac: {', '.join(s['mac_limits'])})"
        lines.append(
            "| "
            + " | ".join(
                _cell(x)
                for x in (
                    s["level"],
                    s["cases"],
                    s["chat_turns"],
                    chat["p50"],
                    chat["p95"],
                    chat["p99"],
                    s["p95_ratio"],
                    s["chat_server_ms"]["p95"],
                    s["error_rate"],
                    "off" if r["scenario"] == "rules" else s["llm_fallback_rate"],
                    f"{_cell(s['api_cpu_pct']['mean'])} / {_cell(s['api_cpu_pct']['max'])}",
                    s["db_cpu_pct"]["max"],
                    s["docker_memory_peak_fraction"],
                    s["host_load_per_cpu_max"],
                    s["generator_lag_ms_p95"],
                    holds,
                )
            )
            + " |"
        )
    lines.append("")
    errors = Counter()
    for s in r["steps"]:
        errors.update(s["errors"])
    if errors:
        lines.append(f"- Failed requests by kind, all levels: {dict(errors)}")
    if r["llm_failures"]:
        lines.append(f"- Failed LLM attempts in the API log, by error: {r['llm_failures']}")
    if r["llm_calls"]:
        lines.append(f"- Calls the simulated LLM answered: {r['llm_calls']}")
    if r["scenario"] == "real":
        cases = sum(s["cases_total"] for s in r["steps"])
        lines.append(f"- LLM spend recorded in the audit log: {r['cost_usd']} USD")
        if r["rate_limits"]:
            lines.append(f"- Rate limits of the key: {r['rate_limits']}")
            cap = ceiling(r["rate_limits"], r["llm_usage"], cases)
            if cap:
                lines.append(
                    "- Cases per minute each limit allows at the use per case of this run "
                    f"(warm-up included, {cases} cases): {cap}"
                )
    lines.append("")
    return lines


def main(argv: list[str] | None = None) -> int:
    """Runs a scenario, serves the simulated LLM or writes the report.

    Args:
        argv: Command line arguments; the process arguments when omitted.

    Returns:
        Process exit code: 0 on success.
    """
    parser = argparse.ArgumentParser(prog="python -m pipeline.load")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--scenario", choices=SCENARIOS, required=True)
    run_p.add_argument("--levels", help="comma separated, starting at 1")
    run_p.add_argument("--budget", type=float, default=BUDGET_USD)
    mock_p = sub.add_parser("mock-llm")
    mock_p.add_argument("--port", type=int, default=MOCK_PORT)
    sub.add_parser("report")
    args = parser.parse_args(argv)
    configure_logging("INFO")
    # One line per request would cost the generator CPU it needs to keep pace.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "mock-llm":
        import uvicorn

        uvicorn.run(mock_app(), host="0.0.0.0", port=args.port, log_level="warning")
        return 0
    if args.command == "report":
        report()
        return 0
    default = REAL_LEVELS if args.scenario == "real" else LEVELS
    levels = tuple(int(x) for x in args.levels.split(",")) if args.levels else default
    if levels[0] != 1:
        raise SystemExit("--levels must start at 1: the criterion compares against it")
    record = run(args.scenario, levels, args.budget)
    log.info(
        "load_run", scenario=args.scenario, capacity=record["capacity"], stopped=record["stopped"]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

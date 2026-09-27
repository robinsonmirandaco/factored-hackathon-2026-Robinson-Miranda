"""LLM runs of the comprehension harness (TRZ-12 CA8): cache, budget, usage and variability.

Each case is read by `LLMClient.read_clues`, the production call, on the redacted message. The
reading is scored before the faithfulness check, so an unfaithful clue counts against the model
as it does for the rules; when the LLM fails, the rules baseline answers, as in production, and
the case counts as a fallback.

Answers are cached in DATA_DIR/eval (outside git: they hold fragments of dataset messages), one
JSON object per line keyed by the hash of the request: model, prompt version and text, output
schema, message, context, temperature and run number. A rerun costs nothing and reports the
tokens, latency and cost first paid. The cache is also the spend ledger: a run stops before it
takes the total past the budget.
"""

import hashlib
import json
import statistics
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.adapters.llm import READING_SCHEMA, LLMClient
from app.core.logging import get_logger
from app.domain.comprehension_rules import comprehend_rules
from app.domain.pii import redact
from app.schemas.comprehension import Comprehension, ComprehensionContext
from pipeline.cases.schema import CaseRecord

log = get_logger("pipeline.comprehension_llm")

CACHE_FILE = "comprehension_cache.jsonl"
# The production call: temperature 0 and a 600-token cap (LLMClient.read_clues).
TEMPERATURE = 0.0
MAX_TOKENS = 600


class BudgetExceeded(RuntimeError):
    """The cached spend reached the budget before every case was read."""


class PromptLeak(RuntimeError):
    """The prompt cites or contains a case outside the development split."""


@dataclass(frozen=True)
class CaseRun:
    """Outcome of one case in one run.

    Attributes:
        reading: What the system answered: the LLM reading, or the rules on a fallback.
        fallback: The rules answered.
        error: Reason of the fallback.
        calls: Requests made, retries included.
        input_tokens: Uncached input tokens.
        output_tokens: Output tokens.
        cache_write_tokens: Input tokens written to the prompt cache.
        cache_read_tokens: Input tokens read from the prompt cache.
        latency_ms: Wall time of the calls of the case.
        cost_usd: Cost of the calls of the case.
    """

    reading: Comprehension
    fallback: bool
    error: str | None
    calls: int
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    latency_ms: int
    cost_usd: float


class ReadingCache:
    """Cached answers of the LLM, one JSON object per line, keyed by the request hash."""

    def __init__(self, path: Path) -> None:
        """Loads the cache file if it exists.

        Args:
            path: DATA_DIR/eval/comprehension_cache.jsonl.
        """
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text("utf-8").splitlines():
                if line:
                    entry = json.loads(line)
                    self.entries[entry["key"]] = entry

    def spent(self) -> float:
        """Total cost of every answer in the cache.

        Returns:
            USD.
        """
        return sum(float(e["cost_usd"]) for e in self.entries.values())

    def put(self, entry: dict[str, Any]) -> None:
        """Stores an answer and appends it to the file.

        Args:
            entry: Answer with its key, reading and usage.
        """
        self.entries[entry["key"]] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_key(client: LLMClient, message: str, context: ComprehensionContext, run: int) -> str:
    """Hash of everything that decides an answer.

    Args:
        client: The LLM client, for the model and the prompt.
        message: Redacted message.
        context: Context of the case.
        run: Run number; repeated runs of the same request are separate answers.

    Returns:
        Hex SHA-256.
    """
    prompt = client.comprehension_prompt
    parts = [
        client.model,
        prompt.version,
        _sha(prompt.system),
        _sha(json.dumps(READING_SCHEMA, sort_keys=True)),
        message,
        context.model_dump(mode="json"),
        TEMPERATURE,
        MAX_TOKENS,
        run,
    ]
    return _sha(json.dumps(parts, sort_keys=True, ensure_ascii=False))


def check_prompt_sources(
    client: LLMClient, dev: list[CaseRecord], others: Iterable[CaseRecord]
) -> None:
    """Checks that the prompt comes from the development split alone (TRZ-12 CA7).

    Args:
        client: The LLM client, for its prompt.
        dev: Cases of the development split.
        others: Cases of the calibration split (and of the test split once frozen).

    Raises:
        PromptLeak: An example cites a case outside the development split, or the prompt
            holds the id or the message of such a case.
    """
    prompt = client.comprehension_prompt
    dev_ids = {c.case_id for c in dev}
    outside = [s for s in prompt.example_sources if s not in dev_ids]
    if outside:
        raise PromptLeak(f"examples not from the development split: {outside}")
    for case in others:
        if case.case_id in prompt.system or case.base_id in prompt.system:
            raise PromptLeak(f"the prompt names the case {case.case_id}")
        if " ".join(case.message.split()) in " ".join(prompt.system.split()):
            raise PromptLeak(f"the prompt holds the message of {case.case_id}")


def _context(case: CaseRecord) -> ComprehensionContext:
    return ComprehensionContext(
        now=case.now,
        country_code=case.truth.country_code,
        local_currency=case.truth.local_currency,
    )


class LLMRuns:
    """Reads cases with the LLM through the cache, within a budget."""

    def __init__(
        self, client: LLMClient, cache: ReadingCache, max_cost_usd: float, workers: int = 8
    ) -> None:
        """Keeps the client, the cache and the budget.

        Args:
            client: LLM client; without a provider only cached cases can be read.
            cache: Answer cache and spend ledger.
            max_cost_usd: Most total spend in the cache.
            workers: Cases read at the same time.
        """
        self.client = client
        self.cache = cache
        self.max_cost_usd = max_cost_usd
        self.workers = workers

    def _read(self, case: CaseRecord, run: int, key: str) -> dict[str, Any]:
        message = redact(case.message)[0]
        reading, stats = self.client.read_clues(message, _context(case))
        return {
            "key": key,
            "run": run,
            "model": stats.model,
            "prompt_version": stats.prompt_version,
            "reading": None if reading is None else reading.model_dump(mode="json"),
            "error": stats.error,
            "calls": stats.calls,
            "input_tokens": stats.input_tokens,
            "output_tokens": stats.output_tokens,
            "cache_write_tokens": stats.cache_write_tokens,
            "cache_read_tokens": stats.cache_read_tokens,
            "latency_ms": stats.latency_ms,
            "cost_usd": stats.cost_usd,
        }

    def run(self, cases: list[CaseRecord], run: int) -> dict[str, CaseRun]:
        """Reads every case once, from the cache when possible.

        Args:
            cases: Cases to read.
            run: Run number.

        Returns:
            Case id to outcome.

        Raises:
            BudgetExceeded: The spend reached the budget with cases still unread.
        """
        keys = {
            c.case_id: request_key(self.client, redact(c.message)[0], _context(c), run)
            for c in cases
        }
        # Cases that make the same request share one answer, paid once.
        unique = {keys[c.case_id]: c for c in cases}
        pending = [c for k, c in unique.items() if k not in self.cache.entries]
        if pending and not self.client.available:
            raise SystemExit("LLM not available: set ANTHROPIC_API_KEY to read uncached cases")
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for start in range(0, len(pending), self.workers):
                if self.cache.spent() >= self.max_cost_usd:
                    raise BudgetExceeded(
                        f"{self.cache.spent():.4f} USD spent, budget {self.max_cost_usd} USD; "
                        f"{len(pending) - start} cases unread"
                    )
                batch = pending[start : start + self.workers]
                for entry in pool.map(lambda c: self._read(c, run, keys[c.case_id]), batch):
                    self.cache.put(entry)
        return {c.case_id: self._outcome(c, self.cache.entries[keys[c.case_id]]) for c in cases}

    @staticmethod
    def _outcome(case: CaseRecord, entry: dict[str, Any]) -> CaseRun:
        if entry["reading"] is None:
            reading = comprehend_rules(redact(case.message)[0], _context(case))
        else:
            reading = Comprehension.model_validate(entry["reading"])
        return CaseRun(
            reading=reading,
            fallback=entry["reading"] is None,
            error=entry["error"],
            calls=int(entry["calls"]),
            input_tokens=int(entry["input_tokens"]),
            output_tokens=int(entry["output_tokens"]),
            cache_write_tokens=int(entry["cache_write_tokens"]),
            cache_read_tokens=int(entry["cache_read_tokens"]),
            latency_ms=int(entry["latency_ms"]),
            cost_usd=float(entry["cost_usd"]),
        )


def as_system(
    cases: list[CaseRecord], outcomes: dict[str, CaseRun]
) -> Callable[[str, ComprehensionContext], Comprehension]:
    """Wraps the outcomes of a run as a harness system.

    Args:
        cases: Cases of the run.
        outcomes: Case id to outcome.

    Returns:
        A function from message and context to the reading of that case.
    """
    by_request = {(c.message, _context(c)): outcomes[c.case_id].reading for c in cases}
    return lambda message, context: by_request[(message, context)]


def _percentile(values: list[int], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, int(q * len(ordered)))])


def usage_summary(outcomes: dict[str, CaseRun]) -> dict[str, Any]:
    """Calls, fallbacks, tokens, cost and latency of one run.

    Args:
        outcomes: Case id to outcome.

    Returns:
        Totals and per-case figures; latency p50 and p95 per case in ms.
    """
    runs = list(outcomes.values())
    cases = len(runs)
    totals = {
        k: sum(asdict(r)[k] for r in runs)
        for k in (
            "calls",
            "input_tokens",
            "output_tokens",
            "cache_write_tokens",
            "cache_read_tokens",
            "cost_usd",
        )
    }
    latencies = [r.latency_ms for r in runs]
    return {
        "cases": cases,
        "fallbacks": sum(r.fallback for r in runs),
        **totals,
        "cost_per_case": totals["cost_usd"] / cases if cases else None,
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "latency_mean_ms": statistics.fmean(latencies) if latencies else None,
    }


def combine_runs(runs: list[Any]) -> Any:
    """Merges the results of repeated runs into their mean, min and max.

    Rates become {"hits", "n", "rate", "min", "max"} with the mean rate; other floats become
    {"mean", "min", "max"}; counts that do not change between runs are kept.

    Args:
        runs: The same structure from each run.

    Returns:
        The merged structure.
    """
    first = runs[0]
    if isinstance(first, dict):
        if set(first) == {"hits", "n", "rate"}:
            rates = [r["rate"] for r in runs if r["rate"] is not None]
            return {
                "hits": round(statistics.fmean(r["hits"] for r in runs), 1),
                "n": round(statistics.fmean(r["n"] for r in runs)),
                "rate": statistics.fmean(rates) if rates else None,
                "min": min(rates) if rates else None,
                "max": max(rates) if rates else None,
            }
        # An intent may be missing from the F1 of one run; later keys follow the first run's.
        extra = sorted({k for r in runs for k in r} - set(first))
        return {k: combine_runs([r[k] for r in runs if k in r]) for k in [*first, *extra]}
    if isinstance(first, float):
        values = [float(r) for r in runs if r is not None]
        return {"mean": statistics.fmean(values), "min": min(values), "max": max(values)}
    return first

"""LLM runs of the comprehension harness (TRZ-12 CA7, CA8): cache, budget, leaks and variability."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.adapters.llm import LLMCallStats, load_comprehension_prompt
from app.schemas.comprehension import Comprehension, ComprehensionContext
from pipeline.comprehension_eval import evaluate
from pipeline.comprehension_llm import (
    BudgetExceeded,
    LLMRuns,
    PromptLeak,
    ReadingCache,
    as_system,
    check_prompt_sources,
    combine_runs,
    usage_summary,
)
from tests.unit.test_comprehension_eval import MESSAGE, _case, _output

PROMPT = load_comprehension_prompt(Path("config/prompts/comprehension.yaml"))


@dataclass
class FakeClient:
    """Stands in for LLMClient: counts calls and answers a fixed reading or nothing."""

    answer: Comprehension | None = field(default_factory=_output)
    cost: float = 0.001
    available: bool = True
    model: str = "test-model"
    comprehension_prompt: object = PROMPT
    seen: list[str] = field(default_factory=list)

    def read_clues(
        self, message: str, context: ComprehensionContext
    ) -> tuple[Comprehension | None, LLMCallStats]:
        self.seen.append(message)
        stats = LLMCallStats(
            input_tokens=40,
            output_tokens=120,
            cache_read_tokens=4000,
            latency_ms=1500,
            cost_usd=self.cost,
            calls=1,
            model=self.model,
            prompt_version=PROMPT.version,
            error=None if self.answer else "ReadTimeout",
        )
        return self.answer, stats


def _cases(n: int) -> list:
    return [
        _case(case_id=f"dev-b{i}-es-mx", base_id=f"dev-b{i}", message=f"{MESSAGE} ({i})")
        for i in range(n)
    ]


def test_a_cached_request_is_not_paid_twice_and_reports_the_first_figures(tmp_path):
    cache_path = tmp_path / "cache.jsonl"
    cases = _cases(3)
    client = FakeClient()
    first = LLMRuns(client, ReadingCache(cache_path), 1.0).run(cases, 0)
    again = LLMRuns(client, ReadingCache(cache_path), 1.0).run(cases, 0)
    assert len(client.seen) == 3
    assert usage_summary(first) == usage_summary(again)
    # Another run number is another request: variability is measured, not read from the cache.
    LLMRuns(client, ReadingCache(cache_path), 1.0).run(cases, 1)
    assert len(client.seen) == 6


def test_the_run_stops_at_the_budget(tmp_path):
    client = FakeClient(cost=0.5)
    with pytest.raises(BudgetExceeded):
        LLMRuns(client, ReadingCache(tmp_path / "c.jsonl"), 0.9, workers=1).run(_cases(4), 0)
    assert len(client.seen) == 2


def test_cases_with_the_same_request_are_paid_once(tmp_path):
    same = [_case(case_id=f"dev-b{i}-es-mx", base_id=f"dev-b{i}") for i in range(3)]
    client = FakeClient()
    outcomes = LLMRuns(client, ReadingCache(tmp_path / "c.jsonl"), 1.0).run(same, 0)
    assert len(client.seen) == 1 and len(outcomes) == 3


def test_messages_are_redacted_before_the_call(tmp_path):
    case = _case(message=MESSAGE + " mi correo es ana.perez@example.com")
    client = FakeClient()
    LLMRuns(client, ReadingCache(tmp_path / "c.jsonl"), 1.0).run([case], 0)
    assert "ana.perez@example.com" not in client.seen[0]


def test_a_failed_reading_falls_back_to_the_rules_and_is_counted(tmp_path):
    cases = _cases(2)
    outcomes = LLMRuns(FakeClient(answer=None), ReadingCache(tmp_path / "c.jsonl"), 1.0).run(
        cases, 0
    )
    assert all(o.fallback for o in outcomes.values())
    assert usage_summary(outcomes)["fallbacks"] == 2
    result = evaluate(cases, as_system(cases, outcomes))
    assert result["overall"]["intent_macro_f1"] == 1.0


def test_the_prompt_must_come_from_the_development_split():
    client = FakeClient()
    sources = [_case(case_id=s, base_id=s.rsplit("-", 2)[0]) for s in PROMPT.example_sources]
    check_prompt_sources(client, sources, [_case(case_id="cal-x-es-mx", base_id="cal-x")])
    with pytest.raises(PromptLeak, match="not from the development split"):
        check_prompt_sources(client, sources[1:], [])
    held_out = _case(case_id="cal-y-es-mx", base_id="cal-y", message=PROMPT.system[40:200])
    with pytest.raises(PromptLeak, match="holds the message"):
        check_prompt_sources(client, sources, [held_out])


def test_repeated_runs_become_mean_and_range():
    runs = [
        {"f1": 0.9, "cases": 10, "acc": {"hits": 9, "n": 10, "rate": 0.9}},
        {"f1": 0.7, "cases": 10, "acc": {"hits": 7, "n": 10, "rate": 0.7}},
    ]
    merged = combine_runs(runs)
    assert merged["cases"] == 10
    assert merged["f1"] == {"mean": pytest.approx(0.8), "min": 0.7, "max": 0.9}
    assert merged["acc"]["rate"] == pytest.approx(0.8)
    assert (merged["acc"]["min"], merged["acc"]["max"], merged["acc"]["n"]) == (0.7, 0.9, 10)

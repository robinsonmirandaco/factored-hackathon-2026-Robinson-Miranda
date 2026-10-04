"""The pure parts of the load test (TRZ-40 CA7): percentiles, the simulated LLM, the next turn of a
virtual customer, the criterion, the limits of the Mac and the report."""

import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.adapters.ingest.synthetic import generate
from app.core.config import Settings
from app.schemas.comprehension import ComprehensionReading, evidence_is_faithful
from pipeline import load


def _step(level: int, p95: float, error_rate: float = 0.0, **resources: Any) -> dict[str, Any]:
    values = {
        "host_load_per_cpu_max": 0.3,
        "docker_memory_peak_fraction": 0.1,
        "host_memory_free_pct_min": 30.0,
        "generator_lag_ms_p95": 2.0,
    }
    values.update(resources)
    return {
        "level": level,
        "cases": 10,
        "cases_total": 12,
        "chat_turns": 30,
        "errors": {},
        "error_rate": error_rate,
        "latency_ms": {"chat": {"p50": p95 / 2, "p95": p95, "p99": p95 * 1.2}},
        "chat_server_ms": {"p50": 5.0, "p95": 9.0},
        "llm_fallback_rate": 0.0,
        "api_cpu_pct": {"mean": 10.0, "max": 20.0},
        "db_cpu_pct": {"mean": 1.0, "max": 2.0},
        **values,
    }


def test_percentile_interpolates_and_is_none_without_samples() -> None:
    assert load.percentile([], 95) is None
    assert load.percentile([7.0], 95) == 7.0
    assert load.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0
    assert load.percentile(list(map(float, range(1, 101))), 95) == pytest.approx(95.05)


def test_the_simulated_reading_is_valid_and_every_clue_is_literal() -> None:
    message = load.message_for(23.45, "COP", "Mercado Libre")
    reading = ComprehensionReading.model_validate(load.mock_reading(message))
    assert reading.intent == "unrecognized_charge"
    assert reading.amount is not None and reading.amount.value == 23.45
    assert reading.amount.currency == "COP"
    assert reading.merchant_hint is not None and reading.merchant_hint.value == "Mercado Libre"
    assert evidence_is_faithful(reading.amount.evidence, message)
    assert evidence_is_faithful(reading.merchant_hint.evidence, message)


def test_any_other_message_reads_as_out_of_scope_with_no_clue() -> None:
    reading = ComprehensionReading.model_validate(load.mock_reading("Hola"))
    assert reading.intent == "out_of_scope"
    assert reading.amount is None and reading.merchant_hint is None


def test_the_simulated_api_answers_comprehension_and_replies_and_counts_them() -> None:
    client = TestClient(load.mock_app(median_seconds=0.001))
    message = load.message_for(10.0, "USD", "Uber")
    asked = {
        "model": "m",
        "messages": [{"role": "user", "content": f"Country: CO\nMessage: {message}"}],
        "output_config": {"format": {"type": "json_schema", "schema": {}}},
    }
    r = client.post("/v1/messages", json=asked)
    assert r.status_code == 200
    reading = json.loads(r.json()["content"][0]["text"])
    assert reading["merchant_hint"]["value"] == "Uber"
    r = client.post(
        "/v1/messages", json={"model": "m", "messages": [{"role": "user", "content": "x"}]}
    )
    assert not any(ch.isdigit() for ch in r.json()["content"][0]["text"])
    assert client.get("/calls").json() == {"comprehension": 1, "other": 1}


@pytest.mark.parametrize(
    ("out", "expected"),
    [
        (
            {"case_id": "K", "outcome": "recognizing"},
            {"recognition": "not_recognized"},
        ),
        (
            {
                "case_id": "K",
                "outcome": "awaiting_confirmation",
                "pending_action": {"action_id": "ACT-1"},
            },
            {"confirm_action_id": "ACT-1"},
        ),
        (
            {"case_id": "K", "outcome": "choosing", "options": [{"transaction_id": "T1"}]},
            {"option": "T1"},
        ),
    ],
)
def test_the_customer_answers_each_step(out: dict[str, Any], expected: dict[str, str]) -> None:
    body = load.next_turn(out)
    assert body is not None and body["case_id"] == "K"
    assert expected.items() <= body.items()


def test_the_case_ends_when_nothing_is_asked() -> None:
    assert load.next_turn({"case_id": "K", "outcome": "escalated", "options": []}) is None


def test_the_capacity_is_the_last_level_that_holds_from_the_first() -> None:
    steps = [_step(1, 100.0), _step(2, 140.0), _step(4, 160.0), _step(8, 120.0)]
    capacity, judged = load.verdicts(steps)
    assert capacity == 2
    assert [s["holds"] for s in judged] == [True, True, False, True]
    assert judged[2]["p95_ratio"] == 1.6


def test_errors_break_a_level_even_with_a_good_p95() -> None:
    capacity, judged = load.verdicts([_step(1, 100.0), _step(2, 100.0, error_rate=0.01)])
    assert capacity == 1 and not judged[1]["holds"]


def test_a_level_past_a_limit_of_the_mac_does_not_count() -> None:
    capacity, judged = load.verdicts([_step(1, 100.0), _step(2, 100.0, generator_lag_ms_p95=80.0)])
    assert capacity == 1
    assert judged[1]["mac_limits"] == ["generator_lag"]


@pytest.mark.parametrize(
    ("resource", "value", "limit"),
    [
        ("host_load_per_cpu_max", 0.95, "host_load"),
        ("docker_memory_peak_fraction", 0.9, "docker_memory"),
        ("host_memory_free_pct_min", 8.0, "host_memory"),
        ("generator_lag_ms_p95", 51.0, "generator_lag"),
    ],
)
def test_each_limit_of_the_mac(resource: str, value: float, limit: str) -> None:
    assert load.mac_limited(_step(1, 100.0)) == []
    assert load.mac_limited(_step(1, 100.0, **{resource: value})) == [limit]


def test_no_capacity_without_steps() -> None:
    assert load.verdicts([]) == (None, [])


def test_the_ceiling_of_each_rate_limit() -> None:
    limits = {
        "anthropic-ratelimit-requests-limit": "50",
        "anthropic-ratelimit-input-tokens-limit": "50000",
        "anthropic-ratelimit-output-tokens-limit": "10000",
    }
    usage = {"rows": 20, "input_tokens": 10000, "output_tokens": 2000, "cases": 10}
    assert load.ceiling(limits, usage, 10) == {
        "requests": 25.0,
        "input-tokens": 50.0,
        "output-tokens": 50.0,
    }
    assert load.ceiling({}, usage, 10) is None
    assert load.ceiling(limits, usage, 0) is None


def test_each_charge_belongs_to_a_customer_of_its_own() -> None:
    settings = Settings(database_url="postgresql+psycopg://unused@localhost:1/x")
    charges = load.charges(settings)
    customers, _products, _transactions = generate(
        settings.trazo_now, seed=load.SEED, n_customers=load.CUSTOMERS, dirty=False
    )
    active = [c for c in customers if c["customer_status"] == "Active"]
    assert len({c.document_number for c in charges}) == len(charges)
    # Every active customer of the fixture has an approved purchase.
    assert len(charges) == len(active)
    assert all(c.message.startswith("No reconozco un cargo de ") for c in charges)


def test_the_report_shows_the_capacity_and_every_level(tmp_path: Path) -> None:
    _capacity, judged = load.verdicts([_step(1, 100.0), _step(2, 200.0)])
    run = {
        "scenario": "simulated",
        "started_at": "2026-10-03T00:00:00+00:00",
        "commit": "abcdef1234",
        "model": None,
        "seed": 42,
        "fixture": {"charges_available": 100, "charges_used": 20},
        "measure_seconds": 60.0,
        "capacity": 1,
        "stopped": "level 2 did not hold",
        "steps": judged,
        "llm_calls": {"comprehension": 5, "other": 5},
        "llm_failures": {"TimeoutError": 2},
        "llm_usage": {},
        "cost_usd": 0.0,
        "rate_limits": {},
        "machine": {
            "system": "Darwin",
            "machine": "arm64",
            "cpus": 8,
            "memory_gib": 8.0,
            "docker_cpus": 8,
            "docker_memory_gib": 3.8,
        },
    }
    runs = tmp_path / "runs.jsonl"
    runs.write_text(json.dumps(run) + "\n", encoding="utf-8")
    text = load.report(runs, tmp_path / "carga.md")
    assert "**Capacity: 1 simultaneous cases.** Stopped: level 2 did not hold." in text
    assert "| 1 | 10 | 30 | 50.0 | 100.0 | 120.0 | 1.0 |" in text
    assert "| 2 | 10 | 30 | 100.0 | 200.0 | 240.0 | 2.0 |" in text
    assert "{'TimeoutError': 2}" in text
    assert "## Scenario `rules`" not in text
    assert (tmp_path / "carga.md").read_text(encoding="utf-8") == text


def test_no_new_case_starts_once_the_budget_is_reached() -> None:
    queue = [load.Charge("CC", "SYN0000001", load.message_for(5.0, "COP", "Uber"))]
    step = load.Step(client=None, queue=queue, demo_code="0")  # type: ignore[arg-type]
    until = time.perf_counter() + 10

    asyncio.run(load._watch_spend(step, lambda: 0.26, 0.25, until))
    assert step.closed
    asyncio.run(step.customer(random.Random(0), until))
    assert step.cases == 0 and len(queue) == 1


def test_the_spend_is_watched_while_under_the_budget() -> None:
    step = load.Step(client=None, queue=[], demo_code="0")  # type: ignore[arg-type]
    reads: list[int] = []

    def spend() -> float:
        reads.append(1)
        return 0.1

    asyncio.run(load._watch_spend(step, spend, 0.25, time.perf_counter() + 0.01))
    assert not step.closed and reads == [1]

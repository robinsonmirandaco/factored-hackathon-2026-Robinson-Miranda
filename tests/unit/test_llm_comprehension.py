"""LLM comprehension (TRZ-12): versioned prompt, reading contract, clock dates and call records."""

import json
from datetime import datetime
from pathlib import Path

import httpx2 as httpx
import pytest
import yaml

from app.adapters import llm as llm_module
from app.adapters.llm import LLMClient, load_comprehension_prompt
from app.schemas.comprehension import ComprehensionContext
from tests.llm_support import anthropic_http, llm_test_settings, message, request_parts

PROMPT_PATH = Path("config/prompts/comprehension.yaml")
# Wednesday: "last_week" is Monday 8 to Sunday 14 of June, 3 to 9 days back.
CONTEXT = ComprehensionContext(
    now=datetime(2026, 6, 17, 10, 0), country_code="CO", local_currency="COP"
)
MESSAGE = "Me cobraron como 250 lucas en Tienda Faro la semana pasada y no fui yo"
READING = {
    "intent": "unrecognized_charge",
    "amount": {
        "value": 250000,
        "currency": "COP",
        "approximate": True,
        "evidence": "como 250 lucas",
    },
    "date": {
        "expression": "la semana pasada",
        "kind": "last_week",
        "count": None,
        "day": None,
        "month": None,
        "year": None,
        "evidence": "la semana pasada",
    },
    "merchant_hint": {"value": "Tienda Faro", "evidence": "en Tienda Faro"},
    "channel_hint": None,
    "card_in_possession": None,
    "language": "es-CO",
}


def _client(outputs: list[str], calls: list[dict[str, object]]) -> LLMClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request_parts(request)[2])
        content = outputs[min(len(calls), len(outputs)) - 1]
        return message(content, 2000, 100)

    return LLMClient(llm_test_settings(), http_client=anthropic_http(handler))


def _date(**changes: object) -> str:
    date = {**READING["date"], **changes}  # type: ignore[dict-item]
    return json.dumps({**READING, "date": date})


def test_reading_follows_the_design_schema_with_the_date_from_the_simulated_clock():
    result, stats = _client([json.dumps(READING)], []).comprehend(MESSAGE, CONTEXT)
    assert not stats.fallback
    assert result.intent == "unrecognized_charge"
    assert result.amount is not None and result.amount.value == 250000
    assert result.date is not None
    assert result.date.resolved_from == CONTEXT.now.date()
    assert result.date.window_days == (3, 9)
    assert result.language == "es-CO"


@pytest.mark.parametrize(
    ("changes", "window"),
    [
        ({"kind": "calendar", "day": 5, "month": 6}, (12, 12)),
        ({"kind": "days_ago", "count": 3}, (2, 4)),
        ({"kind": "yesterday"}, (1, 1)),
    ],
)
def test_every_date_kind_is_resolved_by_code(changes, window):
    result, _ = _client([_date(**changes)], []).comprehend(MESSAGE, CONTEXT)
    assert result.date is not None and result.date.window_days == window


def test_a_date_the_clock_cannot_place_is_dropped():
    # The 30th of June is after the simulated today, and no earlier year is asked for.
    future = _date(kind="calendar", day=30, month=6, year=2026)
    result, stats = _client([future], []).comprehend(MESSAGE, CONTEXT)
    assert not stats.fallback
    assert result.date is None
    assert result.amount is not None


def test_the_llm_cannot_choose_the_reference_date():
    # A reading carrying its own "today" breaks the contract, so it is retried, then ruled out.
    own_today = _date(resolved_from="2020-01-01")
    calls: list[dict[str, object]] = []
    result, stats = _client([own_today], calls).comprehend(MESSAGE, CONTEXT)
    assert stats.fallback and len(calls) == 2
    assert result.date is None or result.date.resolved_from == CONTEXT.now.date()


def test_invented_fragments_are_dropped_and_counted():
    invented = {**READING, "channel_hint": {"value": "Web", "evidence": "por internet"}}
    result, stats = _client([json.dumps(invented)], []).comprehend(MESSAGE, CONTEXT)
    assert stats.dropped_clues == ["channel_hint"]
    assert result.channel_hint is None
    assert result.merchant_hint is not None


def test_valid_json_outside_the_schema_gets_one_retry_then_valid_output_is_used():
    wrong = json.dumps({**READING, "intent": "lost_or_stolen_card"})
    calls: list[dict[str, object]] = []
    result, stats = _client([wrong, json.dumps(READING)], calls).comprehend(MESSAGE, CONTEXT)
    assert not stats.fallback
    assert len(calls) == 2
    assert "not valid" in calls[1]["system"][0]["text"]  # type: ignore[index]
    assert stats.calls == 2
    assert result.intent == "unrecognized_charge"


class _Recorder:
    """Stands in for the module logger; structlog caches loggers, so capture_logs can miss."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def _record(self, event: str, **fields: object) -> None:
        self.events.append({"event": event, **fields})

    info = warning = error = _record


def test_every_call_records_model_prompt_version_tokens_latency_and_cost(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(llm_module, "log", recorder)
    client = _client([json.dumps(READING)], [])
    _, stats = client.comprehend(MESSAGE, CONTEXT)
    logs = recorder.events
    version = client.comprehension_prompt.version
    assert (stats.model, stats.prompt_version) == ("test-model", version)
    assert (stats.input_tokens, stats.output_tokens, stats.calls) == (2000, 100, 1)
    # 2000 input tokens at 1 USD and 100 output tokens at 5 USD per million.
    assert stats.cost_usd == pytest.approx(0.0025)
    call = next(e for e in logs if e["event"] == "llm_call")
    assert call["model"] == "test-model"
    assert call["prompt_version"] == version
    assert call["input_tokens"] == 2000 and call["output_tokens"] == 100
    assert call["cost_usd"] == pytest.approx(0.0025)
    assert "latency_ms" in call


def test_the_prompt_is_versioned_and_its_examples_come_only_from_the_development_split():
    prompt = load_comprehension_prompt(PROMPT_PATH)
    assert prompt.version
    assert prompt.example_sources
    assert all(source.startswith("dev-") for source in prompt.example_sources)
    for held_out in ("cal-", "test-"):
        assert held_out not in prompt.system
        assert not any(held_out in source for source in prompt.example_sources)


def test_an_example_citing_text_outside_its_message_is_refused(tmp_path):
    data = yaml.safe_load(PROMPT_PATH.read_text(encoding="utf-8"))
    example = data["examples"][0]
    example["output"]["merchant_hint"] = {"value": "Oxxo", "evidence": "en Oxxo"}
    path = tmp_path / "prompt.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="evidence not in message"):
        load_comprehension_prompt(path)

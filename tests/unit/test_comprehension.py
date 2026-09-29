"""Comprehension contract (TRZ-13 CA1) and the LLM path with the rules as fallback (CA6)."""

import json
from datetime import date, datetime

import httpx2 as httpx
import pytest
from pydantic import ValidationError

from app.adapters.llm import LLMClient
from app.schemas.comprehension import (
    Comprehension,
    ComprehensionContext,
    DateClue,
    evidence_is_faithful,
)
from tests.llm_support import anthropic_http, llm_test_settings, message, request_parts

CONTEXT = ComprehensionContext(
    now=datetime(2026, 6, 17, 23, 59), country_code="MX", local_currency="MXN"
)
MESSAGE = "No reconozco un cargo de como 1,800 pesos en MercaYa. Tengo mi tarjeta conmigo"
LLM_OUTPUT = {
    "intent": "unrecognized_charge",
    "amount": {
        "value": 1800,
        "currency": "MXN",
        "approximate": True,
        "evidence": "como 1,800 pesos",
    },
    "date": None,
    "merchant_hint": {"value": "MercaYa", "evidence": "en MercaYa"},
    "channel_hint": None,
    "card_in_possession": {"value": True, "evidence": "Tengo mi tarjeta conmigo"},
    "language": "es-MX",
}


def _client(handler, calls: list[str]) -> LLMClient:
    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(request_parts(request)[0])
        return handler(request)

    return LLMClient(llm_test_settings(), http_client=anthropic_http(counting))


def _completion(content: str) -> httpx.Response:
    return message(content, 80, 40)


@pytest.mark.parametrize(
    ("evidence", "faithful"),
    [
        ("como 1,800 pesos", True),
        ("COMO   1,800 PESOS", True),
        ("como 1.800 pesos", False),
        ("MercaYa ayer", False),
    ],
)
def test_evidence_must_be_a_literal_fragment(evidence, faithful):
    assert evidence_is_faithful(evidence, MESSAGE) is faithful


def test_every_clue_requires_evidence():
    with pytest.raises(ValidationError):
        Comprehension.model_validate(
            {**LLM_OUTPUT, "merchant_hint": {"value": "MercaYa", "evidence": ""}}
        )


def test_intents_outside_the_design_are_rejected():
    with pytest.raises(ValidationError):
        Comprehension.model_validate({**LLM_OUTPUT, "intent": "lost_or_stolen_card"})


def test_date_window_must_be_ordered_and_maps_to_dates():
    clue = DateClue(
        expression="la semana pasada",
        resolved_from=date(2026, 6, 17),
        window_days=(3, 9),
        evidence="la semana pasada",
    )
    assert clue.window() == (date(2026, 6, 8), date(2026, 6, 14))
    with pytest.raises(ValidationError):
        DateClue(expression="x", resolved_from=date(2026, 6, 17), window_days=(9, 3), evidence="x")


def test_unfaithful_clues_are_dropped_and_named():
    invented = {**LLM_OUTPUT, "channel_hint": {"value": "Web", "evidence": "por internet"}}
    result, dropped = Comprehension.model_validate(invented).faithful(MESSAGE)
    assert dropped == ["channel_hint"]
    assert result.channel_hint is None
    assert result.merchant_hint is not None


def test_comprehend_success_keeps_faithful_clues():
    calls: list[str] = []
    result, stats = _client(lambda _r: _completion(json.dumps(LLM_OUTPUT)), calls).comprehend(
        MESSAGE, CONTEXT
    )
    assert not stats.fallback
    assert result.amount is not None and result.amount.value == 1800
    assert (stats.input_tokens, stats.output_tokens) == (80, 40)
    assert len(calls) == 1


def test_comprehend_drops_an_invented_fragment():
    invented = {**LLM_OUTPUT, "merchant_hint": {"value": "Oxxo", "evidence": "en Oxxo"}}
    result, stats = _client(lambda _r: _completion(json.dumps(invented)), []).comprehend(
        MESSAGE, CONTEXT
    )
    assert not stats.fallback
    assert result.merchant_hint is None


def test_comprehend_invalid_json_retries_once_then_uses_the_rules():
    calls: list[str] = []
    result, stats = _client(lambda _r: _completion("not json"), calls).comprehend(MESSAGE, CONTEXT)
    assert stats.fallback
    assert stats.error is not None and stats.error.startswith("invalid_json")
    assert len(calls) == 2
    assert "not valid" in calls[1]
    # The rules baseline answers with the same schema.
    assert result.intent == "unrecognized_charge"
    assert result.card_in_possession is not None and result.card_in_possession.value is True


def test_comprehend_timeout_uses_the_rules():
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    calls: list[str] = []
    result, stats = _client(handler, calls).comprehend(
        "Me robaron la tarjeta, necesito bloquearla ya", CONTEXT
    )
    assert stats.fallback
    assert stats.error == "APITimeoutError"
    assert len(calls) == 2  # one bounded retry inside the call, then the rules
    assert result.card_in_possession is not None and result.card_in_possession.value is False

"""Audit rows (TRZ-26): the fields of CA1 and the refusal of model reasoning (CA5)."""

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.db.audit import REASONING_KEYS, ReasoningInAuditError, write_audit
from app.adapters.llm import LLMCallStats, LLMClient, prompt_version_of
from app.core.config import Settings
from app.core.logging import trace_id_var
from app.schemas.comprehension import ComprehensionContext, ComprehensionReading


class FakeSession:
    """Collects what write_audit adds; the audit writer needs no more of a session."""

    def __init__(self, **info: Any) -> None:
        self.info: dict[str, Any] = info
        self.added: list[Any] = []

    def add(self, rec: Any) -> None:
        self.added.append(rec)

    def flush(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _trace() -> Any:
    token = trace_id_var.set("t-audit")
    yield
    trace_id_var.reset(token)


def test_a_step_with_llm_calls_records_tokens_cost_model_and_versions() -> None:
    session = FakeSession(customer_id="C1", policy_version="1")
    stats = LLMCallStats(
        input_tokens=40,
        output_tokens=10,
        cache_write_tokens=5,
        cache_read_tokens=3,
        cost_usd=0.0001234567,
        model="m",
        prompt_version="p1",
    )

    rec = write_audit(session, "agent", "extract", "K1", {"x": 1}, {"y": 2}, 12, llm=stats)  # type: ignore[arg-type]

    assert session.added == [rec]
    assert (rec.trace_id, rec.customer_id, rec.actor, rec.action) == (
        "t-audit",
        "C1",
        "agent",
        "extract",
    )
    assert (rec.payload, rec.result, rec.latency_ms) == ({"x": 1}, {"y": 2}, 12)
    assert (rec.input_tokens, rec.output_tokens) == (48, 10)
    assert rec.cost_usd == pytest.approx(0.000123)
    assert (rec.model, rec.prompt_version, rec.policy_version) == ("m", "p1", "1")
    assert rec.verified is None  # filled by the read-back of TRZ-19


def test_a_step_without_llm_calls_leaves_model_prompt_and_tokens_empty() -> None:
    rec = write_audit(FakeSession(policy_version="1"), "tool", "lookup_transaction")  # type: ignore[arg-type]

    assert (rec.model, rec.prompt_version, rec.input_tokens, rec.cost_usd) == (
        None,
        None,
        None,
        None,
    )
    assert rec.policy_version == "1"


@pytest.mark.parametrize(
    ("payload", "result"),
    [
        ({"reasoning": "because"}, None),
        (None, {"facts": [{"ok": 1}, {"Thinking": "hmm"}]}),
        ({"a": {"b": {"chain_of_thought": "..."}}}, None),
        (None, {"rationale": "x"}),
        (None, {"explanation": "x"}),
    ],
)
def test_a_row_carrying_model_reasoning_is_refused(payload: Any, result: Any) -> None:
    session = FakeSession()
    with pytest.raises(ReasoningInAuditError):
        write_audit(session, "agent", "compose", "K1", payload, result)  # type: ignore[arg-type]
    assert session.added == []


def test_the_llm_output_schemas_have_no_field_for_reasoning() -> None:
    def fields(model: Any) -> set[str]:
        names: set[str] = set()
        for name, info in model.model_fields.items():
            names.add(name.lower())
            for arg in getattr(info.annotation, "__args__", (info.annotation,)):
                if hasattr(arg, "model_fields"):
                    names |= fields(arg)
        return names

    assert not fields(ComprehensionReading) & REASONING_KEYS


def test_an_unversioned_prompt_is_named_by_its_content() -> None:
    assert prompt_version_of("a") == prompt_version_of("a")
    assert prompt_version_of("a") != prompt_version_of("b")
    assert prompt_version_of("a").startswith("sha256:")


def test_a_step_with_two_prompts_cites_both() -> None:
    stats = LLMCallStats(prompt_version="compose")
    stats.add(LLMCallStats(prompt_version="validate"))
    assert stats.prompt_version == "compose+validate"
    stats.add(LLMCallStats(prompt_version="compose+validate"))
    assert stats.prompt_version == "compose+validate"


def _anthropic_client(outputs: list[str], requests: list[dict[str, Any]]) -> LLMClient:
    llm = LLMClient(
        Settings(
            database_url="postgresql+psycopg://unused@localhost:1/unused",
            llm_enabled=False,
            llm_provider="anthropic",
            llm_model_primary="test-model",
        )
    )

    def create(**kwargs: Any) -> Any:
        requests.append(kwargs)
        usage = SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=outputs.pop(0))], usage=usage
        )

    llm._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return llm


def test_no_request_asks_the_model_for_its_reasoning() -> None:
    requests: list[dict[str, Any]] = []
    reading = json.dumps(
        {
            "intent": "out_of_scope",
            "amount": None,
            "date": None,
            "merchant_hint": None,
            "channel_hint": None,
            "card_in_possession": None,
            "language": "es-MX",
        }
    )
    llm = _anthropic_client([reading, "Te ayudo con eso.", '{"ok": true}'], requests)
    context = ComprehensionContext(
        now=datetime(2026, 6, 17, 10, 0), country_code="MX", local_currency="MXN"
    )

    llm.comprehend("quiero un préstamo", context)
    llm.compose("quiero un préstamo", {"outcome": "abstained"}, "es")

    assert len(requests) == 3
    for request in requests:
        assert "thinking" not in request
        assert "thinking" not in json.dumps(request.get("extra_body", {}))


def test_a_rejected_reply_keeps_the_outcome_not_the_validators_words() -> None:
    requests: list[dict[str, Any]] = []
    verdict = json.dumps({"ok": False, "reason": "the reply promises a refund"})
    llm = _anthropic_client(["We will refund you.", verdict], requests)

    _, stats = llm.compose("charge", {"outcome": "informed"}, "es")

    assert stats.fallback
    assert stats.error == "validator_rejected"

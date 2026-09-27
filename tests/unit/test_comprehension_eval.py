"""Comprehension harness (TRZ-13 CA5): scoring, aggregation and the report."""

from datetime import datetime
from typing import Any

from app.schemas.comprehension import Comprehension, ComprehensionContext
from pipeline.cases.schema import CaseRecord
from pipeline.comprehension_eval import (
    SYSTEMS,
    evaluate,
    merchant_matches,
    score_case,
    summarize,
    write_report,
)

NOW = datetime(2026, 6, 17, 23, 59)
MESSAGE = "No reconozco un cargo de como 1,800 pesos en MercaYa ayer. Tengo mi tarjeta conmigo"


def _case(**overrides: Any) -> CaseRecord:
    data: dict[str, Any] = {
        "case_id": "b1-es-MX",
        "base_id": "b1",
        "split": "dev",
        "provenance": "generator_a",
        "category": "normal",
        "intent": "unrecognized_charge",
        "customer_id": "C1",
        "now": NOW,
        "truth": {
            "transaction_id": "T1",
            "timestamp": datetime(2026, 6, 16, 12, 0),
            "country_code": "MX",
            "segment": "Basic",
            "product_type": "credit_card",
            "transaction_type": "Purchase",
            "status": "Approved",
            "channel": "POS",
            "amount": 1790.0,
            "currency": "MXN",
            "amount_usd": 100.0,
            "local_amount": 1790.0,
            "local_currency": "MXN",
            "merchant_name": "MercaYa",
            "merchant_category": "Food",
            "city": "X",
            "transaction_country": "MX",
            "card_in_possession": True,
        },
        "noise": {
            "amount": {
                "mentioned": True,
                "form": "approximate",
                "value": 1800,
                "currency": "MXN",
                "qualifier": "about",
                "low": 1188,
                "high": 2700,
            },
            "date": {
                "mentioned": True,
                "form": "relative",
                "expression": "yesterday",
                "window_start": "2026-06-16",
                "window_end": "2026-06-16",
            },
            "merchant": {"mentioned": True, "form": "complete", "value": "MercaYa"},
            "channel": {"mentioned": False},
            "product": {"mentioned": False},
            "card_possession": {"mentioned": True},
        },
        "scenario": {},
        "expected": {
            "action": "register",
            "rule": "r",
            "first_step": "any",
            "amount_band": "up_to_500",
        },
        "variant": "es-MX",
        "language": "es",
        "message": MESSAGE,
        "message_source": "template",
        "versions": {},
    }
    data.update(overrides)
    return CaseRecord.model_validate(data)


def _output(**overrides: Any) -> Comprehension:
    data: dict[str, Any] = {
        "intent": "unrecognized_charge",
        "amount": {"value": 1800, "approximate": True, "evidence": "como 1,800 pesos"},
        "date": {
            "expression": "ayer",
            "resolved_from": "2026-06-17",
            "window_days": [1, 1],
            "evidence": "ayer",
        },
        "merchant_hint": {"value": "MercaYa", "evidence": "MercaYa"},
        "card_in_possession": {"value": True, "evidence": "Tengo mi tarjeta conmigo"},
        "language": "es-MX",
    }
    data.update(overrides)
    return Comprehension.model_validate(data)


def test_a_right_reading_scores_every_mentioned_field():
    score = score_case(_case(), _output())
    assert score.correct == {
        "amount": True,
        "date": True,
        "merchant": True,
        "card_in_possession": True,
        "language": True,
    }
    assert score.spurious == {"channel": False}
    assert (score.emitted, score.faithful) == (4, 4)


def test_amount_outside_the_label_tolerance_is_wrong():
    out = _output(amount={"value": 18000, "evidence": "1,800"})
    assert score_case(_case(), out).correct["amount"] is False


def test_date_window_must_contain_the_true_date():
    out = _output(
        date={
            "expression": "hoy",
            "resolved_from": "2026-06-17",
            "window_days": [0, 0],
            "evidence": "ayer",
        }
    )
    assert score_case(_case(), out).correct["date"] is False


def test_an_invented_fragment_counts_against_faithfulness_and_is_not_scored():
    out = _output(channel_hint={"value": "Web", "evidence": "por internet"})
    score = score_case(_case(), out)
    assert (score.emitted, score.faithful) == (5, 4)
    assert score.spurious["channel"] is False  # dropped before scoring, as in production


def test_merchant_named_by_category_is_not_scored():
    case = _case(
        noise={
            **_case().noise.model_dump(),
            "merchant": {"mentioned": True, "form": "category", "value": "Food"},
        }
    )
    score = score_case(case, _output())
    assert "merchant" not in score.correct
    assert score.merchant_category


def test_merchant_match_ignores_case_accents_and_partial_names():
    assert merchant_matches("mercaya", "MercaYa")
    assert merchant_matches("Merca", "MercaYa")
    assert merchant_matches("Café Olé", "cafe ole")
    assert not merchant_matches("Ya", "MercaYa")


def test_macro_f1_and_out_of_scope_rates():
    scores = [
        score_case(_case(), _output()),
        score_case(_case(intent="out_of_scope"), _output(intent="out_of_scope")),
        score_case(_case(intent="out_of_scope"), _output()),
    ]
    summary = summarize(scores)
    # unrecognized: tp 1, fp 1 -> 2/3; out_of_scope: tp 1, fn 1 -> 2/3.
    assert summary["intent_macro_f1"] == 2 / 3
    assert summary["out_of_scope_recall"] == {"hits": 1, "n": 2, "rate": 0.5}
    assert summary["in_scope_sent_out"]["rate"] == 0.0


def test_results_split_by_language_and_variant():
    cases = [_case(), _case(case_id="b1-pt-BR", variant="pt-BR", language="pt")]
    result = evaluate(cases, lambda _m, _c: _output())
    assert set(result["by_language"]) == {"es", "pt"}
    assert set(result["by_variant"]) == {"es-MX", "pt-BR"}
    assert result["by_variant"]["pt-BR"]["fields"]["language"]["accuracy"]["rate"] == 0.0


def test_system_receives_the_case_now_and_never_the_customer_id():
    seen: list[ComprehensionContext] = []

    def spy(_message: str, context: ComprehensionContext) -> Comprehension:
        seen.append(context)
        return _output()

    evaluate([_case()], spy)
    assert seen[0].now == NOW
    assert "customer_id" not in seen[0].model_dump()


def test_report_is_deterministic_labelled_and_holds_no_message(tmp_path):
    specs = [SYSTEMS["rules"]]
    meta = {
        "split": "dev",
        "cases": 1,
        "sha256": "abc",
        "seed": 42,
        "config_version": "1",
        "policy_version": 1,
    }
    paths = [tmp_path / "a.md", tmp_path / "b.md"]
    for path in paths:
        results = {"rules": evaluate([_case()], SYSTEMS["rules"].run)}
        write_report(results, meta, specs, path)
    text = paths[0].read_text(encoding="utf-8")
    assert paths[0].read_bytes() == paths[1].read_bytes()
    assert "Development split" in text and "held-out" in text
    assert MESSAGE not in text and "MercaYa" not in text

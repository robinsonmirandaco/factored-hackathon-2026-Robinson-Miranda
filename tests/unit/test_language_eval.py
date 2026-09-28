"""The language report (TRZ-11 CA5): Wilson intervals, scores by variant and a report that
holds counts, never a message."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline.language_eval import detector, profile, score, turn, wilson, write_report

ES = "No reconozco un cargo de 300 pesos en mi tarjeta"
PT = "Não reconheço uma cobrança de 300 reais no meu cartão"


def _case(message: str, variant: str, country: str = "CO") -> Any:
    return SimpleNamespace(
        case_id=f"K-{variant}",
        base_id="K",
        message=message,
        variant=variant,
        language="pt" if variant == "pt-BR" else "es",
        truth=SimpleNamespace(country_code=country),
    )


def test_wilson_reference_values() -> None:
    low, high = wilson(10, 20)
    assert (round(low, 3), round(high, 3)) == (0.299, 0.701)
    assert wilson(0, 0) == (0.0, 1.0)
    assert wilson(20, 20)[1] == 1.0


def test_scores_count_language_and_variant_by_group() -> None:
    cases = [_case(ES, "es-CO"), _case(ES, "es-AR"), _case(PT, "pt-BR")]
    result = score(cases, lambda c: turn(c, None))
    assert result["all"] == {"n": 3, "language": 3, "variant": 2}
    assert result["es-AR"] == {"n": 1, "language": 1, "variant": 0}


def test_a_reading_without_variant_scores_no_variant() -> None:
    result = score([_case(ES, "es-CO")], detector)
    assert result["all"]["variant"] is None


def test_the_llm_variant_is_used_when_it_agrees() -> None:
    assert turn(_case(ES, "es-AR"), "es-AR") == ("es", "es-AR")


def test_profile_counts_ties_mixed_and_short_messages() -> None:
    portunol = "Não reconozco nada no meu cartão"
    cases = [_case(ES, "es-CO"), _case("ok", "es-CO"), _case(portunol, "pt-BR")]
    assert profile(cases) == {"cases": 3, "ties": 1, "mixed": 1, "short": 1}


def test_the_report_holds_no_message(tmp_path: Path) -> None:
    marker = "Mensaje único 7Q9Z de prueba en mi tarjeta"
    cases = [_case(marker, v) for v in ("es-MX", "es-CO", "es-AR")] + [_case(PT, "pt-BR")]
    single = {
        "detector": score(cases, detector),
        "turn_rules": score(cases, lambda c: turn(c, None)),
    }
    meta = {
        "split": "dev",
        "cases": 4,
        "bases": 1,
        "sha256": "abc",
        "seed": 42,
        "config_version": "1",
        "model": "m",
        "prompt_version": "p",
    }
    out = tmp_path / "idioma.md"
    write_report(meta, single, [single["turn_rules"]] * 3, profile(cases), out)
    text = out.read_text()
    assert "7Q9Z" not in text and "reconheço" not in text
    assert "Result 100.0%: met." in text


@pytest.mark.parametrize("hits", [0, 5])
def test_wilson_bounds_stay_in_the_unit_interval(hits: int) -> None:
    low, high = wilson(hits, 5)
    assert 0.0 <= low <= high <= 1.0

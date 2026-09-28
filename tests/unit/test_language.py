"""Language of each turn (TRZ-11): Spanish or Portuguese by rules, the variant from the LLM or
the country, short messages, and mixed messages."""

import pytest

from app.domain.language import decide, signals

ES = "No reconozco un cargo de 300 pesos en mi tarjeta, yo no hice esa compra"
PT = "Não reconheço uma cobrança de 300 reais no meu cartão, eu não fiz essa compra"
PORTUNOL = "Não reconozco esta cobrança no meu cartão, yo no hice"


# ---- CA1: every message is classified as Spanish or Portuguese ----------------------------


@pytest.mark.parametrize(("message", "language"), [(ES, "es"), (PT, "pt")])
def test_a_full_message_is_classified_by_the_detector(message: str, language: str) -> None:
    d = decide(message, None, None, "CO")
    assert (d.language, d.source, d.mixed) == (language, "detector", False)


def test_letters_of_one_language_count_as_its_signals() -> None:
    assert signals("ação").pt == 2 and signals("ação").es == 0
    assert signals("¿año?").es == 2 and signals("¿año?").pt == 0


def test_the_detector_wins_over_the_llm_on_a_full_message() -> None:
    d = decide(PT, "es-MX", "es", "MX")
    assert (d.language, d.variant, d.source) == ("pt", "pt-BR", "detector")


# ---- CA2: the variant ----------------------------------------------------------------------


def test_the_variant_is_the_llm_one_when_it_agrees_with_the_language() -> None:
    assert decide(ES, "es-AR", None, "MX").variant == "es-AR"


@pytest.mark.parametrize(
    ("country", "variant"), [("MX", "es-MX"), ("CO", "es-CO"), ("AR", "es-AR")]
)
def test_without_the_llm_spanish_takes_the_country_variant(country: str, variant: str) -> None:
    assert decide(ES, None, None, country).variant == variant


def test_without_the_llm_portuguese_is_brazilian() -> None:
    assert decide(PT, None, None, "CO").variant == "pt-BR"


def test_an_llm_variant_of_the_other_language_is_not_used() -> None:
    d = decide(ES, "pt-BR", None, "CO")
    assert (d.language, d.variant) == ("es", "es-CO")


# ---- CA3: short messages ---------------------------------------------------------------------


def test_a_short_message_takes_the_llm_language() -> None:
    d = decide("sí, eso", "pt-BR", "es", "MX")
    assert (d.language, d.source) == ("pt", "llm")


def test_a_short_message_without_the_llm_keeps_the_previous_language() -> None:
    d = decide("sim", None, "es", "MX")
    assert (d.language, d.source) == ("es", "previous")
    d = decide("sí", None, "pt", "MX")
    assert (d.language, d.source) == ("pt", "previous")


def test_a_short_first_message_uses_its_signals_or_spanish() -> None:
    assert decide("não reconheço", None, None, "MX").language == "pt"
    d = decide("ok", None, None, "MX")
    assert (d.language, d.source) == ("es", "default")


def test_a_tie_on_a_full_message_falls_back_like_a_short_one() -> None:
    tie = "Hola [NAME] hoy 300 [CARD]"
    assert signals(tie).predominant is None
    assert decide(tie, "pt-BR", None, "MX").source == "llm"
    assert decide(tie, None, "pt", "MX").language == "pt"


# ---- CA4: mixed messages ---------------------------------------------------------------------


def test_portunol_is_mixed_and_answered_in_the_predominant_language() -> None:
    d = decide(PORTUNOL, None, None, "CO")
    assert d.mixed is True
    assert (d.language, d.variant) == ("pt", "pt-BR")


@pytest.mark.parametrize("message", [ES, PT])
def test_a_native_message_is_not_mixed(message: str) -> None:
    assert signals(message).mixed is False

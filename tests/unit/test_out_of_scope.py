"""Abstention out of scope (TRZ-23): the reply says what is not served here and where to go, in
the customer's language (CA1), for at least ten requests in Spanish and in Portuguese (CA3)."""

import pytest

from app.adapters.llm import template_reply
from app.domain.fact_check import VerifiedFacts, unsupported
from app.domain.out_of_scope import topic
from tests.out_of_scope_data import PHRASES

# What each reply must name: what is not served, and where to go instead.
SAYS = {
    ("es", "loan"): ("no podemos tramitar préstamos", "sección de préstamos de la app"),
    ("es", "branch"): ("no tenemos información de sucursales", "sitio oficial"),
    ("es", "app"): ("no podemos resolver problemas de la app", "línea de atención"),
    ("es", "personal_data"): ("no podemos cambiar tus datos personales", "Actualízalos en la app"),
    ("es", "investment"): ("no podemos asesorarte sobre inversiones", "habla con un asesor"),
    ("pt", "loan"): ("não conseguimos contratar empréstimos", "área de empréstimos do app"),
    ("pt", "branch"): ("não temos informações de agências", "site oficial"),
    ("pt", "app"): ("não conseguimos resolver problemas do app", "central de atendimento"),
    ("pt", "personal_data"): ("não conseguimos alterar os seus dados", "Atualize-os no app"),
    ("pt", "investment"): ("não conseguimos orientar sobre investimentos", "fale com um assessor"),
}
SCOPE = {
    "es": "Por aquí atendemos aclaraciones de cargos y te informamos su estado.",
    "pt": "Por aqui atendemos contestações de cobranças e informamos o status delas.",
}


def test_ten_requests_in_each_language_cover_every_topic() -> None:
    for language in ("es", "pt"):
        mine = [t for lang, t, _ in PHRASES if lang == language]
        assert len(mine) >= 10
        assert set(mine) == {"loan", "branch", "app", "personal_data", "investment"}


@pytest.mark.parametrize(("language", "expected", "message"), PHRASES)
def test_the_reply_says_what_is_not_served_and_where_to_go(
    language: str, expected: str, message: str
) -> None:
    assert topic(message) == expected
    reply = template_reply({"outcome": "abstained", "topic": expected}, language)
    not_here, go_to = SAYS[(language, expected)]
    assert not_here in reply and go_to in reply
    assert reply.endswith(SCOPE[language])
    # Nothing in it needs a fact: no number, channel number, amount or action claim.
    assert unsupported(reply, VerifiedFacts()) == []


@pytest.mark.parametrize("language", ["es", "pt"])
def test_a_request_with_no_known_topic_gets_the_general_redirect(language: str) -> None:
    assert topic("Quiero saber mi saldo") == "other"
    general = template_reply({"outcome": "abstained", "topic": "other"}, language)
    assert general == template_reply({"outcome": "abstained"}, language)
    assert "app" in general

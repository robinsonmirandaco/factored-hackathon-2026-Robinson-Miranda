"""Deterministic fact checker of replies (TRZ-20): what it reads (CA2), what it blocks (CA3, CA5),
the closed lists of forbidden requests and action claims in Spanish and Portuguese, and the
deadline note written by code (CA7)."""

from datetime import date

import pytest

from app.adapters.llm import _REPLIES
from app.domain.fact_check import VerifiedFacts, amount_fact, extract, unsupported
from app.domain.policy_passages import Passage
from app.services.replies import deadline_note, long_date

FOLIO = "DSP-2026-00042"
FACTS = VerifiedFacts(
    amounts=frozenset({amount_fact(120.0), amount_fact(2400.5)}),
    dates=frozenset({date(2026, 6, 16), date(2026, 6, 17), date(2026, 7, 9)}),
    folios=frozenset({FOLIO}),
    passages=frozenset({"§2.1"}),
    deadlines=frozenset({15}),
    last4=frozenset({"4821"}),
    merchants=frozenset({"netflix"}),
    known_merchants=frozenset({"netflix", "cinépolis", "7-eleven"}),
    actions=frozenset({"register_dispute"}),
)
PASSAGE = Passage(
    id="§2.1",
    rule="response_deadline",
    label={"es": "política de demostración, no del banco", "pt": "política de demonstração"},
    text={"es": "...", "pt": "..."},
    business_days=15,
)


def _kinds(text: str, facts: VerifiedFacts = FACTS) -> list[tuple[str, str]]:
    return [(c.kind, c.value) for c in unsupported(text, facts)]


# ---- CA2: what is read ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("120 USD", "120.00"),
        ("US$ 120", "120.00"),
        ("$1,234.56", "1234.56"),
        ("1.234,56 MXN", "1234.56"),
        ("R$ 2.400,50", "2400.50"),
        ("2.500 pesos", "2500.00"),
        ("120 dólares", "120.00"),
        ("MXN 2,400.5", "2400.50"),
    ],
)
def test_amounts_are_read_in_several_formats(text: str, value: str) -> None:
    assert [(c.kind, c.value) for c in extract(text)] == [("amount", value)]


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("el 2026-06-16", "2026-06-16"),
        ("el 16/06/2026", "2026-06-16"),
        ("el 16/06", "--06-16"),
        ("el 16 de junio de 2026", "2026-06-16"),
        ("el 16 de junio", "--06-16"),
        ("em 9 de julho de 2026", "2026-07-09"),
        ("el 16 jun", "--06-16"),
    ],
)
def test_dates_are_read_in_several_formats(text: str, value: str) -> None:
    assert [(c.kind, c.value) for c in extract(text)] == [("date", value)]


def test_folios_deadlines_passages_and_card_digits_are_read() -> None:
    text = f"Folio {FOLIO}, 15 días hábiles según el §2.1, tarjeta terminada en 4821."
    assert [(c.kind, c.value) for c in extract(text)] == [
        ("folio", FOLIO),
        ("passage", "§2.1"),
        ("card_digits", "4821"),
        ("deadline", "15 d"),
    ]


# ---- CA3, CA5: blocked and passed ---------------------------------------------------------


def test_a_correct_reply_passes() -> None:
    es = (
        f"Registramos tu aclaración del cargo de 120.00 USD (2,400.50 MXN) en Netflix del "
        f"16 de junio con el folio {FOLIO}. Plazo de respuesta: a más tardar el 9 de julio de "
        f"2026, 15 días hábiles según el §2.1."
    )
    pt = f"Registramos a sua contestação de R$ 2.400,50 com o protocolo {FOLIO}."
    assert _kinds(es) == [] and _kinds(pt) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Te responderemos en 10 días hábiles.", [("deadline", "10 d")]),
        ("Te responderemos antes del 30 de junio.", [("date", "--06-30")]),
        ("Vamos responder em 48 horas.", [("deadline", "48 h")]),
        ("El cargo de 1,200.00 USD quedó registrado.", [("amount", "1200.00")]),
        ("A cobrança de R$ 12,00 foi registrada.", [("amount", "12.00")]),
        (f"Tu folio es DSP-2026-00043, no {FOLIO}.", [("folio", "DSP-2026-00043")]),
        ("La tarjeta terminada en 1234.", [("card_digits", "1234")]),
        ("Según el §4.2 no aplica.", [("passage", "§4.2")]),
        ("El cargo de Cinépolis.", [("merchant", "cinépolis")]),
        ("Te escribo en 3 minutos.", [("number", "3")]),
        ("El 31/02 no existe.", [("date", "")]),
    ],
    ids=[
        "invented deadline",
        "invented date",
        "invented duration pt",
        "other amount es",
        "other amount pt",
        "invented folio",
        "other card digits",
        "uncited passage",
        "merchant not in the facts",
        "bare number",
        "impossible date",
    ],
)
def test_an_unsupported_element_is_named(text: str, expected: list[tuple[str, str]]) -> None:
    assert _kinds(text) == expected


def test_a_merchant_not_on_the_customers_list_is_not_detected() -> None:
    # Declared limit: an invented merchant name is not on the closed list.
    assert _kinds("El cargo de Tienda Inventada.") == []


# ---- forbidden requests and action claims, in both languages ------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Envíame tu contraseña para continuar.",
        "Confírmame el PIN de tu tarjeta.",
        "Necesito el CVV.",
        "Dime el número completo de tu tarjeta.",
        "Envíanos una foto de tu documento.",
        "Me envie a sua senha.",
        "Informe o código de segurança.",
        "Preciso do número do seu cartão.",
        "Mande o seu CPF.",
    ],
)
def test_a_forbidden_request_is_blocked(text: str) -> None:
    assert [k for k, _ in _kinds(text)] == ["forbidden_request"]


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("Bloqueamos tu tarjeta.", "block_card"),
        ("Tu tarjeta quedó bloqueada.", "block_card"),
        ("Bloqueamos o seu cartão.", "block_card"),
        ("O seu cartão foi bloqueado.", "block_card"),
        ("Te devolveremos el dinero.", "refund_or_cancel"),
        ("Vamos estornar a cobrança.", "refund_or_cancel"),
        ("Cancelamos el cargo.", "refund_or_cancel"),
    ],
)
def test_an_action_that_was_not_done_is_blocked(text: str, action: str) -> None:
    assert _kinds(text) == [("action_claim", action)]


@pytest.mark.parametrize(
    "text",
    ["Registramos tu aclaración.", "Registramos a sua contestação.", "Quedó registrada."],
)
def test_an_action_that_was_done_passes(text: str) -> None:
    assert _kinds(text) == []


def test_offering_an_action_is_not_claiming_it() -> None:
    no_actions = VerifiedFacts()
    assert (
        _kinds("Responde sí para registrar la aclaración y bloquear tu tarjeta.", no_actions) == []
    )
    assert (
        _kinds("Responda sim para registrar a contestação e bloquear o seu cartão.", no_actions)
        == []
    )


# ---- the fixed replies and the deadline note pass the checker ------------------------------


@pytest.mark.parametrize("language", ["es", "pt"])
@pytest.mark.parametrize("key", sorted(_REPLIES["es"]))
def test_every_fixed_reply_passes_the_checker(language: str, key: str) -> None:
    done = {"register_dispute", "block_card"} if key.startswith("registered") else set()
    facts = VerifiedFacts(folios=frozenset({FOLIO}), actions=frozenset(done))
    assert _kinds(_REPLIES[language][key].format(folio=FOLIO), facts) == []


@pytest.mark.parametrize("language", ["es", "pt"])
def test_the_deadline_note_cites_the_passage_and_passes_the_checker(language: str) -> None:
    facts = {
        "outcome": "registered_verified",
        "dispute": {"folio": FOLIO, "due_date": "2026-07-09", "passage": "§2.1"},
    }
    note = deadline_note(facts, {"response_deadline": PASSAGE}, language)
    assert long_date(date(2026, 7, 9), language) in note
    assert "§2.1" in note and PASSAGE.label[language] in note
    assert _kinds(note) == []


@pytest.mark.parametrize(("language", "person"), [("es", "una persona"), ("pt", "uma pessoa")])
def test_without_a_backing_passage_no_deadline_is_stated_and_a_person_is_offered(
    language: str, person: str
) -> None:
    facts = {
        "outcome": "registered_verified",
        "dispute": {"folio": FOLIO, "due_date": None, "passage": None},
    }
    note = deadline_note(facts, {}, language)
    assert person in note
    assert not [c for c in extract(note) if c.kind in ("date", "deadline", "passage")]
    assert _kinds(note, VerifiedFacts()) == []


def test_no_note_when_nothing_was_registered() -> None:
    assert deadline_note({"outcome": "escalated"}, {"response_deadline": PASSAGE}, "es") == ""

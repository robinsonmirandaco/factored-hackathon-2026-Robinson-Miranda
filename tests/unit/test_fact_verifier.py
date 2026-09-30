"""Deterministic fact checker of replies (TRZ-20): what it reads (CA2), what it blocks (CA3, CA5),
the closed lists of forbidden requests and action claims in Spanish and Portuguese, and the
deadline note written by code (CA7)."""

from datetime import date

import pytest

from app.adapters.llm import _REPLIES
from app.domain.fact_check import VerifiedFacts, amount_fact, extract, unsupported
from app.domain.policy_passages import Passage
from app.services.replies import STATUS_WORDS, deadline_note, long_date

FOLIO = "DSP-2026-00042"
CLAIM = "CMP-TEST000000000000001"
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


def test_the_number_of_options_shown_is_a_fact() -> None:
    shown = VerifiedFacts(counts=frozenset({2}))
    assert _kinds("Encontré 2 cargos que coinciden.", shown) == []
    assert _kinds("Encontrei 3 cobranças.", shown) == [("number", "3")]


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
    if key == "card_blocked":
        done = {"block_card"}
    facts = VerifiedFacts(folios=frozenset({FOLIO}), actions=frozenset(done))
    assert _kinds(_REPLIES[language][key].format(folio=FOLIO), facts) == []


def _claim(**extra: object) -> dict:
    return {
        "outcome": "informed",
        "action": "report_claim_status",
        "claim": {
            "claim_id": CLAIM,
            "opened_on": "2026-06-12",
            "status": "received",
            "last_step": {"step": "created", "on": "2026-06-12"},
            "due_date": "2026-07-06",
            "passage_id": "§2.1",
            "overdue": False,
            **extra,
        },
    }


CLAIM_FACTS = VerifiedFacts(
    folios=frozenset({CLAIM}),
    dates=frozenset({date(2026, 6, 12), date(2026, 7, 6)}),
    passages=frozenset({"§2.1"}),
    deadlines=frozenset({15}),
)


@pytest.mark.parametrize("language", ["es", "pt"])
def test_the_claim_note_cites_its_source_and_deadline_and_passes_the_checker(
    language: str,
) -> None:
    note = deadline_note(_claim(), {"response_deadline": PASSAGE}, language)
    assert CLAIM in note and "§2.1" in note and long_date(date(2026, 7, 6), language) in note
    assert _kinds(note, CLAIM_FACTS) == []


@pytest.mark.parametrize(("language", "word"), [("es", "venció"), ("pt", "venceu")])
def test_an_overdue_claim_note_says_the_deadline_passed(language: str, word: str) -> None:
    note = deadline_note(_claim(overdue=True), {"response_deadline": PASSAGE}, language)
    assert word in note and "§2.1" in note
    assert _kinds(note, CLAIM_FACTS) == []


@pytest.mark.parametrize(("language", "person"), [("es", "una persona"), ("pt", "uma pessoa")])
def test_a_claim_without_a_backing_passage_gets_no_deadline_and_a_person(
    language: str, person: str
) -> None:
    note = deadline_note(_claim(due_date=None, passage_id=None), {}, language)
    assert CLAIM in note and person in note
    assert not [c for c in extract(note) if c.kind in ("deadline", "passage")]
    # The only date is the opening of the claim, in its status sentence; no due date.
    assert {c.value for c in extract(note) if c.kind == "date"} == {"2026-06-12"}
    opened = VerifiedFacts(folios=frozenset({CLAIM}), dates=frozenset({date(2026, 6, 12)}))
    assert _kinds(note, opened) == []


def test_no_claim_note_without_a_claim_and_a_person_when_none_of_them_is_the_one() -> None:
    none = {"outcome": "informed", "action": "report_claim_status", "claim": None}
    assert deadline_note(none, {}, "es") == ""
    assert deadline_note({**none, "other_claim": True}, {}, "es").endswith("una persona.")


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


@pytest.mark.parametrize("language", ["es", "pt"])
@pytest.mark.parametrize(
    ("status", "step"),
    [
        ("received", "created"),
        ("in_review", "assigned"),
        ("answered", "first_response"),
        ("received", "registered"),
    ],
)
def test_the_claim_status_is_written_by_code_in_a_closed_vocabulary(
    language: str, status: str, step: str
) -> None:
    facts = _claim(status=status, last_step={"step": step, "on": "2026-06-13"})
    note = deadline_note(facts, {"response_deadline": PASSAGE}, language)
    words = STATUS_WORDS[language]
    assert f"{words[status]}." in note and words[step] in note
    assert long_date(date(2026, 6, 13), language) in note
    backed = VerifiedFacts(
        folios=frozenset({CLAIM}),
        dates=frozenset({date(2026, 6, 12), date(2026, 6, 13), date(2026, 7, 6)}),
        passages=frozenset({"§2.1"}),
        deadlines=frozenset({15}),
    )
    assert _kinds(note, backed) == []


@pytest.mark.parametrize(
    "text",
    [
        "Revisaremos tu reclamo y te contactaremos en breve.",
        "Pronto te contactaremos con novedades.",
        "Te llamaremos pronto.",
        "Sua reclamação está em análise e em breve teremos novidades.",
        "Você receberá notícias sobre o andamento.",
        "Você receberá novidades por e-mail.",
    ],
)
def test_a_contact_promise_is_never_backed(text: str) -> None:
    assert [k for k, _ in _kinds(text)] == ["contact_promise"]


@pytest.mark.parametrize(
    "text",
    [
        "Si quieres, te comunico con una persona.",
        "Se quiser, eu coloco você em contato com uma pessoa.",
        "Si quieres, te comunicamos con una persona.",
        "Se quiser, colocamos você em contato com uma pessoa.",
        "Una analista revisará tu aclaración antes de registrarla.",
    ],
)
def test_offering_a_person_is_not_a_contact_promise(text: str) -> None:
    assert _kinds(text, VerifiedFacts()) == []


# ---- wider lexicon of contact promises and vague deadlines (QA of TRZ-34) ------------------


@pytest.mark.parametrize(
    "text",
    [
        "Nos pondremos en contacto contigo.",
        "Te contactaremos cuando haya novedades.",
        "Nuestro equipo se pondrá en contacto contigo.",
        "La analista te contactará.",
        "Nos comunicaremos contigo.",
        "Te escribiremos por correo.",
        "Nossa equipe entrará em contato com você.",
        "Eles entrarão em contato para ajudar.",
        "Vamos entrar em contato com você.",
        "Entraremos em contato.",
        "A analista vai entrar em contato.",
    ],
)
def test_more_contact_promises_are_never_backed(text: str) -> None:
    assert [k for k, _ in _kinds(text)] == ["contact_promise"]


@pytest.mark.parametrize(
    "text",
    [
        "Lo revisaremos en los próximos días.",
        "Tendrás una respuesta en unos días.",
        "Se resuelve en pocos días.",
        "Lo verán en las próximas horas.",
        "Te responderemos a la brevedad.",
        "Próximamente tendrás una respuesta.",
        "Vamos analisar nos próximos dias.",
        "Você terá resposta em alguns dias.",
        "Isso se resolve em poucos dias.",
        "A analista vai revisar nas próximas horas.",
    ],
)
def test_a_vague_deadline_is_never_backed(text: str) -> None:
    assert [k for k, _ in _kinds(text)] == ["vague_deadline"]


@pytest.mark.parametrize(
    "text",
    [
        "¿Quieres hablar con una persona?",
        "Si quieres, te comunicamos con una persona.",
        "Você quer falar com uma pessoa?",
        "Se quiser, colocamos você em contato com uma pessoa.",
        "Plazo de respuesta: a más tardar el 8 de julio de 2026 (15 días hábiles).",
    ],
)
def test_offering_a_person_or_a_backed_deadline_still_passes(text: str) -> None:
    facts = VerifiedFacts(dates=frozenset({date(2026, 7, 8)}), deadlines=frozenset({15}))
    assert _kinds(text, facts) == []


# ---- relative dates must match the charge's real date (QA of TRZ-34) -----------------------

TODAY = date(2026, 6, 17)  # a Wednesday


@pytest.mark.parametrize(
    "text",
    [
        "Encontramos el cargo de Netflix de la semana pasada.",
        "Veja a cobrança da semana passada.",
        "El cargo de ayer ya aparece.",
        "O cargo de ontem já aparece.",
    ],
)
def test_a_relative_date_that_misses_the_charge_is_unsupported(text: str) -> None:
    # The only charge is from 2026-06-05: neither yesterday nor last week.
    facts = VerifiedFacts(dates=frozenset({date(2026, 6, 5)}), today=TODAY)
    assert "relative_date" in [k for k, _ in _kinds(text, facts)]


@pytest.mark.parametrize(
    ("text", "charge"),
    [
        ("Encontramos el cargo de Netflix de la semana pasada.", date(2026, 6, 10)),
        ("Veja a cobrança da semana passada.", date(2026, 6, 8)),
        ("El cargo de ayer ya aparece.", date(2026, 6, 16)),
        ("O cargo de ontem já aparece.", date(2026, 6, 16)),
        ("Es un cargo de esta semana.", date(2026, 6, 16)),
    ],
)
def test_a_relative_date_that_fits_the_charge_passes(text: str, charge: date) -> None:
    facts = VerifiedFacts(dates=frozenset({charge}), today=TODAY)
    assert [k for k, _ in _kinds(text, facts)] == []


def test_last_week_for_a_charge_of_this_week_is_caught() -> None:
    # The trace of the QA: a charge of 2026-06-16 called "de la semana pasada" on 2026-06-17.
    facts = VerifiedFacts(dates=frozenset({date(2026, 6, 16)}), today=TODAY)
    assert "relative_date" in [k for k, _ in _kinds("el cargo de la semana pasada", facts)]

"""Rules baseline of comprehension (TRZ-13): intents, amounts, relative dates and clues."""

from datetime import date, datetime

import pytest

from app.domain.comprehension_rules import comprehend_rules
from app.schemas.comprehension import Comprehension, ComprehensionContext, evidence_is_faithful

# Wednesday 17 June 2026, the default TRAZO_NOW.
NOW = datetime(2026, 6, 17, 23, 59)
MX = ComprehensionContext(now=NOW, country_code="MX", local_currency="MXN")
CO = ComprehensionContext(now=NOW, country_code="CO", local_currency="COP")
AR = ComprehensionContext(now=NOW, country_code="AR", local_currency="ARS")


def _read(message: str, context: ComprehensionContext = MX) -> Comprehension:
    result = comprehend_rules(message, context)
    # CA1: every clue the rules produce is a literal fragment of the message.
    for clue in result.clues().values():
        assert evidence_is_faithful(clue.evidence, message)
    return result


@pytest.mark.parametrize(
    ("message", "intent"),
    [
        ("No reconozco un cargo en mi tarjeta", "unrecognized_charge"),
        ("Ese cobro no fui yo, ayúdenme", "unrecognized_charge"),
        ("Não fui eu que fiz essa compra", "unrecognized_charge"),
        ("Não reconheço uma cobrança no meu cartão", "unrecognized_charge"),
        ("Alguien usó mi tarjeta sin mi autorización", "unrecognized_charge"),
        ("Creo que es un fraude, yo no compré nada", "unrecognized_charge"),
        ("Hay un débito que no es mío", "unrecognized_charge"),
        ("Me cobraron dos veces la misma compra", "billing_error_duplicate"),
        ("Tengo una cobrança duplicada no cartão", "billing_error_duplicate"),
        ("El cargo aparece repetido en mi estado de cuenta", "billing_error_duplicate"),
        ("Cobraram duas vezes o mesmo valor", "billing_error_duplicate"),
        ("Me cobraron de más en el supermercado", "billing_error_amount"),
        ("La compra era de 300 pero me cobraron 450", "billing_error_amount"),
        ("Fui cobrado errado, o valor certo era 90 reais", "billing_error_amount"),
        ("El monto es distinto al precio acordado", "billing_error_amount"),
        ("¿Cómo va mi reclamo del cargo que no reconozco?", "claim_status"),
        ("Quería saber el estado de la disputa que abrí", "claim_status"),
        ("Qual o andamento da minha contestação?", "claim_status"),
        ("Quiero pedir un préstamo personal", "out_of_scope"),
        ("¿Cuál es el horario de la sucursal del centro?", "out_of_scope"),
        ("Esqueci a senha do aplicativo", "out_of_scope"),
        ("Hola, buenas tardes", "out_of_scope"),
    ],
)
def test_intent_keywords_in_spanish_and_portuguese(message, intent):
    assert _read(message).intent == intent


@pytest.mark.parametrize(
    ("message", "value", "currency", "approximate"),
    [
        ("un cargo de 1,800 pesos que no hice", 1800, "MXN", False),
        ("un cargo de 1.800 que no hice", 1800, None, False),
        ("un cargo de 1800 que no hice", 1800, None, False),
        ("me cobraron 1.8 mil y no fui yo", 1800, None, False),
        ("un cargo de como 390 lucas que no reconozco", 390000, "MXN", True),
        ("uma compra de R$ 500 que não fiz", 500, "BRL", False),
        ("un débito de 1.800,50 pesos colombianos", 1800.5, "COP", False),
        ("me cobraron US$ 45.90 que no reconozco", 45.9, "USD", False),
        ("fueron unos 2 palos que no reconozco", 2_000_000, "MXN", True),
        ("uma cobrança de mais de 300 reais", 300, "BRL", True),
    ],
)
def test_amount_formats(message, value, currency, approximate):
    amount = _read(message).amount
    assert amount is not None
    assert amount.value == pytest.approx(value)
    assert amount.currency == currency
    assert amount.approximate is approximate


@pytest.mark.parametrize(
    "message",
    [
        "no reconozco un cargo de hace 3 días",
        "el cargo es del 15 de mayo y no lo hice",
        "me cobraron dos veces, 2 cargos iguales",
    ],
)
def test_counts_days_and_calendar_dates_are_not_amounts(message):
    assert _read(message).amount is None


@pytest.mark.parametrize(
    ("message", "first", "last"),
    [
        ("un cargo de hoy que no hice", date(2026, 6, 17), date(2026, 6, 17)),
        ("un cargo de ayer que no hice", date(2026, 6, 16), date(2026, 6, 16)),
        ("un cargo de anteayer que no hice", date(2026, 6, 15), date(2026, 6, 15)),
        ("uma compra de ontem que não fiz", date(2026, 6, 16), date(2026, 6, 16)),
        ("el lunes me llegó un cargo raro", date(2026, 6, 15), date(2026, 6, 15)),
        ("el viernes pasado vi un cargo raro", date(2026, 6, 5), date(2026, 6, 12)),
        ("el domingo apareció un cobro", date(2026, 6, 14), date(2026, 6, 14)),
        ("na sexta-feira passada apareceu uma cobrança", date(2026, 6, 5), date(2026, 6, 12)),
        ("la semana pasada vi un cargo", date(2026, 6, 8), date(2026, 6, 14)),
        ("na semana passada apareceu uma compra", date(2026, 6, 8), date(2026, 6, 14)),
        ("esta semana apareció un cobro", date(2026, 6, 15), date(2026, 6, 17)),
        ("hace 10 días me cobraron algo", date(2026, 6, 6), date(2026, 6, 8)),
        ("há umas duas semanas apareceu uma compra", date(2026, 5, 31), date(2026, 6, 6)),
        ("el mes pasado me cobraron algo raro", date(2026, 5, 1), date(2026, 5, 31)),
    ],
)
def test_relative_dates_resolve_to_a_window_against_the_case_now(message, first, last):
    clue = _read(message).date
    assert clue is not None
    assert clue.resolved_from == NOW.date()
    assert clue.window() == (first, last)


def test_same_expression_resolves_against_each_case_own_now():
    earlier = ComprehensionContext(
        now=datetime(2024, 3, 1, 9, 0), country_code="MX", local_currency="MXN"
    )
    clue = _read("un cargo de ayer que no hice", earlier).date
    assert clue is not None and clue.window() == (date(2024, 2, 29), date(2024, 2, 29))


def test_segunda_as_an_ordinal_is_not_a_weekday():
    assert _read("cobraram pela segunda vez a mesma compra").date is None


def test_stolen_card_wins_over_block():
    # CA7: the wish to block the card does not hide that the customer no longer has it.
    result = _read("Me robaron la tarjeta, necesito bloquearla ya")
    assert result.card_in_possession is not None
    assert result.card_in_possession.value is False
    assert result.card_in_possession.evidence == "Me robaron"
    # A theft with no charge mentioned is a blocking request the policy redirects (TRZ-17 CA9).
    assert result.intent == "out_of_scope"


@pytest.mark.parametrize(
    ("message", "value"),
    [
        ("Perdí la tarjeta y quiero bloquearla, además hay un cargo raro", False),
        ("Roubaram meu cartão, preciso bloquear, tem uma compra que não fiz", False),
        ("Ya no tengo la tarjeta, se me perdió, y veo un cargo que no hice", False),
        ("No reconozco el cargo, tengo mi tarjeta conmigo", True),
        ("La tarjeta la tengo acá, pero hay un consumo que no hice", True),
        ("O cartão tá comigo e não reconheço essa compra", True),
        ("No la perdí, pero hay un cargo que no reconozco", True),
    ],
)
def test_card_possession(message, value):
    result = _read(message)
    assert result.card_in_possession is not None
    assert result.card_in_possession.value is value


@pytest.mark.parametrize(
    ("message", "channel"),
    [
        ("retiraron dinero en un cajero y no fui yo", "ATM"),
        ("um saque no caixa eletrônico que não fiz", "ATM"),
        ("una compra por internet que no reconozco", "Web"),
        ("uma compra pelo site que não fiz", "Web"),
        ("un pago desde la app que no hice", "App"),
        ("pagaron con posnet y no fui yo", "POS"),
        ("un cargo en una tienda que no reconozco", "POS"),
    ],
)
def test_channel(message, channel):
    clue = _read(message).channel_hint
    assert clue is not None and clue.value == channel


@pytest.mark.parametrize(
    ("message", "merchant"),
    [
        ("No reconozco un cargo en MercaYa de ayer", "MercaYa"),
        ("uma compra na Loja Azul que não fiz", "Loja Azul"),
        ("me cobraron dos veces el Uber", "Uber"),
    ],
)
def test_merchant(message, merchant):
    clue = _read(message).merchant_hint
    assert clue is not None and clue.value == merchant


def test_placeholders_and_months_are_not_merchants():
    assert _read("un cargo en [CARD] del 5 de Mayo que no hice").merchant_hint is None


@pytest.mark.parametrize(
    ("message", "context", "language"),
    [
        ("No reconozco este cargo en mi tarjeta", MX, "es-MX"),
        ("No reconozco este cargo en mi tarjeta", CO, "es-CO"),
        ("Che, vos sabés que tenés un cargo raro? No lo hice", MX, "es-AR"),
        ("Não reconheço essa cobrança no meu cartão", CO, "pt-BR"),
    ],
)
def test_language_variant(message, context, language):
    assert _read(message, context).language == language


def test_design_example_reads_every_clue():
    result = _read(
        "No reconozco un cargo de como 1,800 pesos en MercaYa la semana pasada. "
        "Tengo mi tarjeta conmigo"
    )
    assert result.intent == "unrecognized_charge"
    assert result.amount is not None and result.amount.evidence == "como 1,800 pesos"
    assert result.date is not None and result.date.evidence == "la semana pasada"
    assert result.merchant_hint is not None and result.merchant_hint.value == "MercaYa"
    assert result.card_in_possession is not None and result.card_in_possession.value is True


def test_same_message_gives_the_same_output():
    message = "Me cobraron dos veces unos 1.500 pesos en Farmacia Sol el martes pasado"
    assert _read(message) == _read(message)


@pytest.mark.parametrize(
    ("message", "day"),
    [
        ("un cargo del 19 de marzo que no hice", date(2026, 3, 19)),
        ("uma compra de 19 de março que não fiz", date(2026, 3, 19)),
        ("el cargo del 19/03 no lo reconozco", date(2026, 3, 19)),
        ("uma compra em 19/03/2026 que não fiz", date(2026, 3, 19)),
        ("un cargo del 19/03/26 que no hice", date(2026, 3, 19)),
        ("un cargo con fecha 2026-03-19 que no hice", date(2026, 3, 19)),
        ("no dia 5 de junho de 2026 apareceu uma cobrança", date(2026, 6, 5)),
        ("uma compra de 1º de maio que não reconheço", date(2026, 5, 1)),
        # Without a year, a date after today is last year's.
        ("el 20 de diciembre me cobraron algo raro", date(2025, 12, 20)),
        # A calendar date is more precise than a relative expression.
        ("ayer vi un cargo del 10 de junio que no hice", date(2026, 6, 10)),
    ],
)
def test_calendar_dates_resolve_to_one_day(message, day):
    clue = _read(message).date
    assert clue is not None
    assert clue.resolved_from == NOW.date()
    assert clue.window() == (day, day)


@pytest.mark.parametrize(
    "message",
    [
        "un cargo del 31/02 que no hice",
        "uma compra de 2026-12-01 que não fiz",
    ],
)
def test_impossible_or_future_calendar_dates_are_dropped(message):
    assert _read(message).date is None


def test_calendar_date_without_year_resolves_against_each_case_own_now():
    earlier = ComprehensionContext(
        now=datetime(2024, 3, 1, 9, 0), country_code="MX", local_currency="MXN"
    )
    clue = _read("el 20 de diciembre me cobraron algo raro", earlier).date
    assert clue is not None and clue.window() == (date(2023, 12, 20), date(2023, 12, 20))


@pytest.mark.parametrize(
    ("message", "value"),
    [
        ("un cargo del 19/03 de 500 pesos que no hice", 500),
        ("un cargo con fecha 2026-03-19 que no hice", None),
    ],
)
def test_calendar_dates_are_not_amounts(message, value):
    amount = _read(message).amount
    assert (amount.value if amount else None) == value

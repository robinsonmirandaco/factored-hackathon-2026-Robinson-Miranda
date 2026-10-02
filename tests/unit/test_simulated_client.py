"""Simulated client of the evaluation harness (TRZ-43 CA1 to CA6)."""

from datetime import date, timedelta
from typing import Any

import pytest

from pipeline.cases.noise import relative_windows
from pipeline.cases.schema import VARIANTS
from pipeline.simulated_client import (
    MAX_TURNS,
    SimulatedClient,
    SystemTurn,
    load_templates,
)
from tests.case_support import make_case

TEMPLATES = load_templates()


def test_the_first_turn_is_the_message_of_the_case() -> None:
    client = SimulatedClient(make_case(), TEMPLATES)

    first = client.first()

    assert first.message == "Hola, no reconozco un cargo de MercaYa."
    assert client.turns == 1


def test_asked_for_a_detail_it_gives_amount_and_date_with_the_drawn_form() -> None:
    client = SimulatedClient(make_case(), TEMPLATES)
    client.first()

    answer = client.answer(SystemTurn("ask_detail"))

    assert answer is not None
    assert answer.message == "Fueron como 1,800 pesos, la semana pasada."
    assert answer.clues == ("amount", "date")


def test_a_clue_already_given_is_not_repeated_and_then_it_has_nothing_more() -> None:
    case = make_case(amount_mentioned=True, date_mentioned=True)
    client = SimulatedClient(case, TEMPLATES)
    client.first()

    second = client.answer(SystemTurn("ask_detail"))
    third = client.answer(SystemTurn("ask_detail"))
    fourth = client.answer(SystemTurn("ask_detail"))

    assert second is not None and second.clues == ("channel",)
    assert third is not None and third.clues == ("product",)
    assert fourth is not None and fourth.message == "No recuerdo más detalles."


def test_an_exact_amount_keeps_its_decimals_in_portuguese() -> None:
    client = SimulatedClient(make_case("pt-BR", amount_form="exact"), TEMPLATES)
    client.first()

    answer = client.answer(SystemTurn("ask_detail"))

    assert answer is not None
    assert answer.message == "Foram 1.834,50 pesos mexicanos, semana passada."


def test_among_options_it_picks_the_true_charge() -> None:
    client = SimulatedClient(make_case(), TEMPLATES)
    client.first()

    answer = client.answer(SystemTurn("show_options", options=("TX-9", "TX-1")))

    assert answer is not None and answer.option == "TX-1"


def test_without_the_true_charge_among_options_it_says_none() -> None:
    client = SimulatedClient(make_case(), TEMPLATES)
    client.first()

    answer = client.answer(SystemTurn("show_options", options=("TX-9",)))

    assert answer is not None
    assert (answer.option, answer.message) == ("none", "Ninguno de esos.")


@pytest.mark.parametrize(
    ("recognizes", "button", "text"),
    [(False, "not_recognized", "Sigo sin reconocerlo."), (True, "recognized", "Ya lo reconozco.")],
)
def test_after_the_detail_it_answers_by_the_label(recognizes: bool, button: str, text: str) -> None:
    client = SimulatedClient(make_case(recognizes=recognizes), TEMPLATES)
    client.first()

    answer = client.answer(SystemTurn("recognition"))

    assert answer is not None
    assert (answer.recognition, answer.message) == (button, text)


def test_it_confirms_the_action_it_is_asked_to_confirm() -> None:
    client = SimulatedClient(make_case("es-AR"), TEMPLATES)
    client.first()

    answer = client.answer(SystemTurn("confirm", action_id="ACT-0123456789"))

    assert answer is not None
    assert (answer.confirm_action_id, answer.message) == ("ACT-0123456789", "Sí, confirmo.")


def test_a_question_in_plain_text_gets_every_clue_and_the_card() -> None:
    client = SimulatedClient(make_case(card_in_possession=False), TEMPLATES)
    client.first()

    answer = client.answer(SystemTurn("text_question"))

    assert answer is not None
    assert answer.message == (
        "Esto es lo que recuerdo: fueron como 1,800 pesos, la semana pasada, en MercaYa, "
        "con terminal, con la tarjeta de crédito. Ya no tengo la tarjeta."
    )


def test_nothing_asked_ends_the_conversation() -> None:
    client = SimulatedClient(make_case(), TEMPLATES)
    client.first()

    assert client.answer(SystemTurn("done")) is None


def test_a_conversation_has_at_most_eight_customer_turns() -> None:
    client = SimulatedClient(make_case(), TEMPLATES)
    client.first()
    answers = [client.answer(SystemTurn("confirm", action_id="ACT-0123456789")) for _ in range(9)]

    assert sum(a is not None for a in answers) == MAX_TURNS - 1
    assert answers[-1] is None


def test_two_clients_of_the_same_case_answer_alike() -> None:
    asks = [
        SystemTurn("ask_detail"),
        SystemTurn("show_options", options=("TX-1",)),
        SystemTurn("recognition"),
        SystemTurn("confirm", action_id="ACT-0123456789"),
        SystemTurn("ask_detail"),
    ]

    def converse() -> list[Any]:
        client = SimulatedClient(make_case("es-CO"), TEMPLATES)
        return [client.first(), *(client.answer(a) for a in asks)]

    assert converse() == converse()


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_expression_the_noise_model_draws_has_a_template(variant: str) -> None:
    keys: set[str] = set()
    for back in range(40):
        keys |= set(relative_windows(date(2026, 6, 30) - timedelta(days=back)))
    for key in sorted(keys):
        client = SimulatedClient(make_case(variant, expression=key), TEMPLATES)
        client.first()
        answer = client.answer(SystemTurn("ask_detail"))
        assert answer is not None and "date" in answer.clues, key


def test_every_variant_has_the_same_template_keys() -> None:
    variants = TEMPLATES["variants"]
    shared = {k for k in variants["es-MX"] if k != "date"}
    for v in VARIANTS:
        assert {k for k in variants[v] if k != "date"} == shared, v

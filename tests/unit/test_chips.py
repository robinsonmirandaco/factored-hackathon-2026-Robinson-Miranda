"""The chips of the chat (design 10.1): what was read in the message, sent to the screen."""

from app.schemas.comprehension import Comprehension
from app.services.agent import chips


def _amount(value: float, currency: str | None) -> Comprehension:
    return Comprehension.model_validate(
        {
            "intent": "unrecognized_charge",
            "language": "pt-BR",
            "amount": {"value": value, "currency": currency, "evidence": "2.300.000 pesos"},
        }
    )


def test_an_amount_chip_carries_the_number_and_currency_for_the_screen_to_format() -> None:
    # Seen in the demo rehearsal: the chip said "2.3e+06 COP".
    (chip,) = chips(_amount(2300000.0, "COP"))
    assert (chip.field, chip.amount, chip.currency) == ("amount", 2300000.0, "COP")
    assert "e+" not in chip.value


def test_an_amount_chip_without_currency_carries_none() -> None:
    (chip,) = chips(_amount(2300000.0, None))
    assert (chip.amount, chip.currency) == (2300000.0, None)
    assert "e+" not in chip.value

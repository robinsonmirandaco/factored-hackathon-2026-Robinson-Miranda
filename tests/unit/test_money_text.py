"""Amounts written by the server in the format the web gives them (money() of web/assets/i18n.js).

The expected strings are what Intl.NumberFormat writes in the browser for es-MX and pt-BR, with
two decimals and the no-break space between the code and the number.
"""

import pytest

from app.domain.money import money_text

NBSP = " "


@pytest.mark.parametrize(
    ("lang", "value", "currency", "expected"),
    [
        ("es", 2300000, "COP", f"COP{NBSP}2,300,000.00"),
        ("pt", 2300000, "COP", f"COP{NBSP}2.300.000,00"),
        ("es", 1234.5, "USD", f"USD{NBSP}1,234.50"),
        ("pt", 1234.5, "USD", f"US${NBSP}1.234,50"),
        ("es", 1234.5, "MXN", "$1,234.50"),
        ("pt", 1234.5, "MXN", f"MX${NBSP}1.234,50"),
        ("es", 1179714.2, "ARS", f"ARS{NBSP}1,179,714.20"),
        ("pt", 1179714.2, "ARS", f"ARS{NBSP}1.179.714,20"),
        ("es", 0.5, "COP", f"COP{NBSP}0.50"),
    ],
)
def test_an_amount_reads_as_the_web_writes_it(
    lang: str, value: float, currency: str, expected: str
) -> None:
    assert money_text(lang, value, currency) == expected


@pytest.mark.parametrize(("lang", "expected"), [("es", "2,300,000"), ("pt", "2.300.000")])
def test_an_amount_without_currency_is_a_plain_number(lang: str, expected: str) -> None:
    assert money_text(lang, 2300000, None) == expected


def test_never_in_scientific_notation() -> None:
    assert "e+" not in money_text("es", 2.3e6, "COP")

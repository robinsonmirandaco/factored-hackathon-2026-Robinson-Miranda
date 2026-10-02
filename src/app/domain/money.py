"""Amounts written by the server as the web writes them (money() of web/assets/i18n.js).

The web formats with Intl.NumberFormat for es-MX and pt-BR, two decimals; text the server
writes for the screens, such as the request summary of a dossier, uses the same format so an
amount reads the same everywhere and never in scientific notation.
"""

from typing import Literal

Lang = Literal["es", "pt"]
NBSP = " "
# The symbols Intl uses where they are not the ISO code; any other currency is its code.
_SYMBOL: dict[str, dict[str, str]] = {"es": {"MXN": "$"}, "pt": {"USD": "US$", "MXN": "MX$"}}


def money_text(lang: Lang, value: float, currency: str | None) -> str:
    """Writes an amount in the format of the web for a language.

    Args:
        lang: es or pt.
        value: The amount.
        currency: ISO 4217 code, or None for a plain number.

    Returns:
        "COP 2,300,000.00" in Spanish, "COP 2.300.000,00" in Portuguese, with a no-break space
        after a code; without currency, the number alone with at most three decimals.
    """
    text = f"{value:,.2f}" if currency else f"{value:,.3f}".rstrip("0").rstrip(".")
    if lang == "pt":
        text = text.translate(str.maketrans(",.", ".,"))
    if not currency:
        return text
    symbol = _SYMBOL[lang].get(currency)
    # Intl writes the peso sign of Mexico in es-MX right before the number.
    return f"{symbol}{text}" if symbol == "$" else f"{symbol or currency}{NBSP}{text}"

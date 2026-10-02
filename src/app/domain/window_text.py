"""Date windows written by the server as the customer's chip writes them (chipParts of view.js).

A date the customer gave is read as a window of days; the screens show those days, not the
customer's words, which stay as evidence. Text the server writes for the screens, such as the
request summary of a dossier, says the window the same way.
"""

from datetime import date
from typing import Literal

Lang = Literal["es", "pt"]
# Short months as Intl writes them for es-MX and pt-BR, without the trailing point.
_MONTHS: dict[str, tuple[str, ...]] = {
    "es": ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"),
    "pt": ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"),
}
_NEAR = {"es": "{window} y días cercanos", "pt": "{window} e dias próximos"}


def window_text(lang: Lang, first: date, last: date) -> str:
    """Writes the days a date clue means, as the customer's chip does.

    Args:
        lang: es or pt.
        first: First day of the window.
        last: Last day of the window.

    Returns:
        "16 jun y días cercanos", or "8 de jun a 14 de jun e dias próximos" in Portuguese.
    """

    def day(d: date) -> str:
        month = _MONTHS[lang][d.month - 1]
        return f"{d.day} de {month}" if lang == "pt" else f"{d.day} {month}"

    window = day(first) if first == last else f"{day(first)} a {day(last)}"
    return _NEAR[lang].format(window=window)

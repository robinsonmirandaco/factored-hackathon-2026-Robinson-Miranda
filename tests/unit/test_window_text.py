"""Date windows written by the server as the customer's chip writes them (chipParts of view.js).

The expected strings are what the web gives for es-MX and pt-BR: dayMonth() of each end, joined
by dateRange and wrapped by nearDays.
"""

from datetime import date

import pytest

from app.domain.window_text import window_text


@pytest.mark.parametrize(
    ("lang", "first", "last", "expected"),
    [
        ("es", date(2026, 6, 16), date(2026, 6, 16), "16 jun y días cercanos"),
        ("es", date(2026, 6, 8), date(2026, 6, 14), "8 jun a 14 jun y días cercanos"),
        ("pt", date(2026, 6, 16), date(2026, 6, 16), "16 de jun e dias próximos"),
        ("pt", date(2026, 6, 8), date(2026, 6, 14), "8 de jun a 14 de jun e dias próximos"),
        ("es", date(2025, 12, 28), date(2026, 1, 3), "28 dic a 3 ene y días cercanos"),
        ("pt", date(2026, 9, 5), date(2026, 9, 5), "5 de set e dias próximos"),
    ],
)
def test_a_window_reads_as_the_customers_chip(
    lang: str, first: date, last: date, expected: str
) -> None:
    assert window_text(lang, first, last) == expected

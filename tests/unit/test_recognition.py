"""The text of the recognition step (TRZ-16): what it shows, and that it informs without arguing."""

import itertools
from dataclasses import replace
from datetime import date, datetime

import pytest

from app.domain.fx import AmountDisplay
from app.domain.recognition import ChargeDetail, Twin, choices, recognition_text

AT = datetime(2026, 6, 16, 22, 41)

BASE = ChargeDetail(
    transaction_id="TX1",
    transaction_type="Purchase",
    merchant="MercaYa Polanco",
    amount=AmountDisplay(90.0, "USD", 1_650.5, "MXN", "aprox., tasa del día", True),
    at=AT,
    channel="Web",
    city="Ciudad de México",
    product_type="credit_card",
    last4="4821",
    status="Approved",
    twin=None,
    earlier_months=(),
)

PENDING = replace(BASE, status="Pending")
TWIN_PENDING = replace(BASE, twin=Twin("TX0", datetime(2026, 6, 16, 22, 40), "Pending"))
TWIN_APPROVED = replace(BASE, twin=Twin("TX0", datetime(2026, 6, 10, 9, 5), "Approved"))
RECURRING = replace(BASE, earlier_months=(date(2026, 4, 1), date(2026, 5, 1)))
EVERYTHING = replace(PENDING, twin=TWIN_PENDING.twin, earlier_months=RECURRING.earlier_months)

# Closed list of phrases that push the customer to drop the dispute (CA6).
DISSUASION = {
    "es": [
        "¿estás seguro",
        "¿está seguro",
        "estás seguro",
        "probablemente sí lo hiciste",
        "probablemente fuiste tú",
        "seguramente lo hiciste",
        "revisa bien",
        "piénsalo",
        "piénsalo bien",
        "recuerda que",
        "no es necesario",
        "no vale la pena",
    ],
    "pt": [
        "tem certeza",
        "provavelmente foi você",
        "provavelmente você fez",
        "com certeza você",
        "pense bem",
        "verifique bem",
        "lembre-se de que",
        "não é necessário",
        "não vale a pena",
    ],
}


# The charge card of the screen (ChatOut.charge, read from the database) is the only place the
# detail is shown; the text only frames it and adds the notes (QA finding 8).


@pytest.mark.parametrize("language", ["es", "pt"])
def test_the_text_leaves_the_detail_to_the_card(language: str) -> None:
    text = recognition_text(BASE, language)
    for detail in ("MercaYa Polanco", "4821", "Ciudad de México", "90.00", "1,650.50", "22:41"):
        assert detail not in text, detail
    assert text.startswith({"es": "Este es el cargo", "pt": "Esta é a cobrança"}[language])
    assert text.endswith(
        {"es": "¿Reconoces este cargo?", "pt": "Você reconhece essa cobrança?"}[language]
    )


def test_a_pending_charge_is_explained_as_not_settled() -> None:
    assert "aún no se ha liquidado" in recognition_text(PENDING, "es")
    assert "ainda não foi liquidada" in recognition_text(PENDING, "pt")
    assert "liquidado" not in recognition_text(BASE, "es")


def test_twin_with_one_pending_is_explained_as_a_temporary_hold() -> None:
    text = recognition_text(TWIN_PENDING, "es")
    assert "Hay otro cargo igual de este comercio el 16 jun 2026, 22:40, pendiente" in text
    assert "retención temporal" in text


def test_twin_with_both_approved_is_shown_as_a_possible_duplicate() -> None:
    text = recognition_text(TWIN_APPROVED, "es")
    assert "el 10 jun 2026, 09:05, también aprobado: puede ser un cobro duplicado" in text
    assert "retención" not in text


def test_earlier_months_of_the_same_merchant_are_named() -> None:
    assert "Tienes cargos de este comercio en abril y mayo de 2026." in recognition_text(
        RECURRING, "es"
    )
    assert "em abril e maio de 2026." in recognition_text(RECURRING, "pt")
    across = replace(BASE, earlier_months=(date(2026, 1, 1), date(2025, 12, 1)))
    assert "en diciembre de 2025 y enero de 2026." in recognition_text(across, "es")


@pytest.mark.parametrize("language", ["es", "pt"])
def test_still_not_recognized_is_always_the_first_choice(language: str) -> None:
    ids = [c["id"] for c in choices(language)]
    assert ids == ["not_recognized", "recognized"]


def test_choice_labels() -> None:
    assert [c["label"] for c in choices("es")] == ["Sigo sin reconocerlo", "Ya lo reconozco"]
    assert [c["label"] for c in choices("pt")] == ["Continuo sem reconhecer", "Já reconheço"]


@pytest.mark.parametrize(
    ("detail", "language"),
    list(
        itertools.product(
            [BASE, PENDING, TWIN_PENDING, TWIN_APPROVED, RECURRING, EVERYTHING], ["es", "pt"]
        )
    ),
)
def test_the_text_informs_and_never_argues(detail: ChargeDetail, language: str) -> None:
    text = recognition_text(detail, language).lower()
    labels = " ".join(c["label"] for c in choices(language)).lower()
    for phrase in DISSUASION[language]:
        assert phrase not in text, phrase
        assert phrase not in labels, phrase

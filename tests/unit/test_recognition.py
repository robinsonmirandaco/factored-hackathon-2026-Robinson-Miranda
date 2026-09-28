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


def test_the_detail_shows_merchant_city_channel_time_card_and_status() -> None:
    text = recognition_text(BASE, "es")
    for shown in (
        "Comercio: MercaYa Polanco",
        "Ciudad: Ciudad de México",
        "Canal: compra en línea",
        "Fecha y hora: 16/06/2026 a las 22:41",
        "Tarjeta: tarjeta de crédito terminada en 4821",
        "Estado: aprobado",
        "Monto: 90.00 USD (1,650.50 MXN, aprox., tasa del día)",
    ):
        assert shown in text


def test_the_detail_in_portuguese() -> None:
    text = recognition_text(BASE, "pt")
    assert "Estabelecimento: MercaYa Polanco" in text
    assert "Cartão: cartão de crédito com final 4821" in text
    assert "Data e hora: 16/06/2026 às 22:41" in text
    assert text.endswith("Você reconhece essa cobrança?")


def test_a_missing_city_or_card_number_is_left_out_not_invented() -> None:
    text = recognition_text(replace(BASE, city=None, last4=None), "es")
    assert "Ciudad" not in text and "Tarjeta" not in text


def test_a_product_that_is_not_a_card_is_named_as_a_product() -> None:
    text = recognition_text(replace(BASE, product_type="checking_account"), "es")
    assert "Producto: terminada en 4821" in text


def test_a_pending_charge_is_explained_as_not_settled() -> None:
    assert "aún no se ha liquidado" in recognition_text(PENDING, "es")
    assert "ainda não foi liquidada" in recognition_text(PENDING, "pt")
    assert "liquidado" not in recognition_text(BASE, "es")


def test_twin_with_one_pending_is_explained_as_a_temporary_hold() -> None:
    text = recognition_text(TWIN_PENDING, "es")
    assert "Hay otro cargo igual de este comercio el 16/06/2026 a las 22:40, pendiente" in text
    assert "retención temporal" in text


def test_twin_with_both_approved_is_shown_as_a_possible_duplicate() -> None:
    text = recognition_text(TWIN_APPROVED, "es")
    assert "el 10/06/2026 a las 09:05, también aprobado: puede ser un cobro duplicado" in text
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

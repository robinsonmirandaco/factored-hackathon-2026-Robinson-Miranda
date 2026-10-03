"""Evaluation cases built by hand for the harness tests (TRZ-43, TRZ-44)."""

from datetime import datetime, timedelta
from typing import Any

from pipeline.cases.noise import relative_windows
from pipeline.cases.schema import (
    AmountClue,
    CaseRecord,
    DateClue,
    Expected,
    Mention,
    MerchantClue,
    Noise,
    Scenario,
    Truth,
)
from tests.serving_data import card, customer, load, transaction

NOW = datetime(2026, 6, 17, 12, 0)


def make_case(
    variant: str = "es-MX",
    *,
    amount_mentioned: bool = False,
    date_mentioned: bool = False,
    merchant_mentioned: bool = True,
    recognizes: bool = False,
    card_in_possession: bool | None = True,
    expression: str = "last_week",
    amount_form: str = "approximate",
    expected_action: str = "register_and_offer_block",
    scenario: dict[str, Any] | None = None,
    message: str = "Hola, no reconozco un cargo de MercaYa.",
    **truth: Any,
) -> CaseRecord:
    """A case of customer C1 disputing transaction TX-1, with overridable truth and scenario."""
    true_values: dict[str, Any] = {
        "transaction_id": "TX-1",
        "timestamp": NOW - timedelta(days=9),
        "country_code": "MX",
        "segment": "Basic",
        "product_type": "credit_card",
        "transaction_type": "Purchase",
        "status": "Approved",
        "channel": "POS",
        "amount": 1834.5,
        "currency": "MXN",
        "amount_usd": 99.0,
        "local_amount": 1834.5,
        "local_currency": "MXN",
        "merchant_name": "MercaYa",
        "merchant_category": "Food",
        "city": "Puebla",
        "transaction_country": "MX",
        "card_in_possession": card_in_possession,
        "recognizes_after_detail": recognizes,
        **truth,
    }
    # Some expressions ("last monday", "early this month") are drawn only on some days.
    window = next(
        w[expression]
        for back in range(40)
        if expression in (w := relative_windows(NOW.date() - timedelta(days=back)))
    )
    noise = Noise(
        amount=AmountClue(
            mentioned=amount_mentioned,
            form=amount_form,  # type: ignore[arg-type]
            value=1800.0 if amount_form != "exact" else true_values["amount"],
            currency=true_values["currency"],
            qualifier="about" if amount_form == "approximate" else None,
        ),
        date=DateClue(
            mentioned=date_mentioned,
            form="relative",
            expression=expression,
            window_start=window[0],
            window_end=window[1],
        ),
        merchant=MerchantClue(
            mentioned=merchant_mentioned, form="complete", value=true_values["merchant_name"]
        ),
        channel=Mention(mentioned=False),
        product=Mention(mentioned=False),
        card_possession=Mention(mentioned=False),
    )
    return CaseRecord(
        base_id="B1",
        split="dev",
        provenance="generator_a",
        category="normal",
        intent="unrecognized_charge",
        customer_id="C1",
        now=NOW,
        truth=Truth(**true_values),
        noise=noise,
        scenario=Scenario(**(scenario or {})),
        expected=Expected(
            action=expected_action,  # type: ignore[arg-type]
            rule="design 4.2 test",
            first_step="any",
            amount_band="up_to_500",
        ),
        case_id=f"B1-{variant}",
        variant=variant,  # type: ignore[arg-type]
        language="pt" if variant == "pt-BR" else "es",
        message=message,
        message_source="template",
        versions={},
    )


class FixtureData:
    """Two customers of Mexico with USD cards; C1 has the disputed Netflix charge TX-1."""

    def __init__(self) -> None:
        mx = {"country": "México", "country_code": "MX", "timezone": "America/Mexico_City"}
        self.customers = [customer("C1", **mx), customer("C2", **mx)]
        self.products = [card("P1", "C1", currency="USD"), card("P2", "C2", currency="USD")]
        at = NOW - timedelta(hours=20)
        self.transactions = [
            transaction(
                "TX-1", "C1", "P1", at, amount=15.0, currency="USD", merchant_name="Netflix"
            ),
            transaction(
                "TX-2",
                "C1",
                "P1",
                at - timedelta(days=3),
                amount=48.0,
                currency="USD",
                merchant_name="Uber",
            ),
            transaction(
                "TX-9", "C2", "P2", at, amount=15.0, currency="USD", merchant_name="Netflix"
            ),
        ]

    def load(self, admin_url: str, customer_ids: set[str]) -> None:
        load(
            admin_url,
            [c for c in self.customers if c["customer_id"] in customer_ids],
            [p for p in self.products if p["customer_id"] in customer_ids],
            [t for t in self.transactions if t["customer_id"] in customer_ids],
        )

    def document(self, customer_id: str) -> dict[str, str]:
        c = next(c for c in self.customers if c["customer_id"] == customer_id)
        return {"document_type": c["document_type"], "document_number": c["document_number"]}


def netflix_case(**kwargs: Any) -> CaseRecord:
    values: dict[str, Any] = {
        "message": "No reconozco un cargo de Netflix por 15 dólares de ayer, tengo mi tarjeta.",
        "merchant_name": "Netflix",
        "amount": 15.0,
        "currency": "USD",
        "amount_usd": 15.0,
        "timestamp": NOW - timedelta(hours=20),
    }
    return make_case(**{**values, **kwargs})

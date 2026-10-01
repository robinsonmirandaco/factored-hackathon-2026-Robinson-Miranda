"""Texts of the in-app notifications (TRZ-32 CA4): written by code, checked by the fact checker."""

from datetime import date

import pytest

from app.domain.fact_check import VerifiedFacts, unsupported
from app.domain.policy import load_policy
from app.services.notifications import REASONS, compose

FOLIO = "DSP-2026-00042"
DUE = date(2026, 6, 24)


@pytest.mark.parametrize("language", ["es", "pt"])
@pytest.mark.parametrize(
    ("kind", "facts"),
    [
        ("approved", {"folio": FOLIO}),
        ("rejected", {"reason": "wrong_charge"}),
        ("info_requested", {"due": DUE}),
        ("audit_reversed", {}),
    ],
)
def test_every_notification_passes_the_fact_checker(
    language: str, kind: str, facts: dict[str, object]
) -> None:
    text, checked = compose(kind, language, **facts)  # type: ignore[arg-type]
    assert checked, text


def test_an_approval_states_the_folio_it_registered() -> None:
    text, _ = compose("approved", "es", folio=FOLIO)
    assert FOLIO in text


def test_a_request_for_information_states_the_day_to_answer() -> None:
    assert "24 de junio de 2026" in compose("info_requested", "es", due=DUE)[0]
    assert "24 de junho de 2026" in compose("info_requested", "pt", due=DUE)[0]


def test_a_rejection_tells_the_reason_in_plain_words() -> None:
    text, _ = compose("rejected", "es", reason="insufficient_data")
    assert REASONS["es"]["insufficient_data"] in text
    assert "insufficient_data" not in text


@pytest.mark.parametrize("language", ["es", "pt"])
def test_every_reason_of_the_policy_has_plain_words(language: str) -> None:
    reasons = load_policy("config/policy.yaml").autonomy.reversal_reasons
    assert set(reasons) == set(REASONS[language])


def test_a_text_the_checker_does_not_back_is_replaced_by_one_with_no_figures() -> None:
    # An approval without the verified folio would state a registration nothing backs.
    text, checked = compose("approved", "es", folio=None)
    assert not checked
    assert not unsupported(text, VerifiedFacts())
    assert text == "Hay novedades en tu aclaración. Revísala en Mis aclaraciones."


def test_a_reversed_audit_tells_the_review_without_promising_contact() -> None:
    for language in ("es", "pt"):
        text, checked = compose("audit_reversed", language)
        assert checked and not unsupported(text, VerifiedFacts())

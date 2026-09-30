"""One voice and one term for what the customer reads (QA finding 9): the bank speaks as "we",
a clarification is always an "aclaración" ("contestação"), and a policy citation is a
[simulado] label on its own line, not running text."""

import re
from pathlib import Path

import pytest

from app.adapters.llm import _REPLIES
from app.domain.policy_passages import Passage
from app.services.replies import _NOTES, deadline_note

# First person singular, which the bank never uses to speak to the customer.
SINGULAR = {
    "es": re.compile(
        r"\b(tengo|comunico|atiendo|escr[ií]beme|reviso|encontr[eé]|encuentro|puedo|veo|"
        r"me dices)\b",
        re.IGNORECASE,
    ),
    "pt": re.compile(
        r"\b(eu|me escreva|encontrei|consigo|vejo|coloco|verifico|pode me dizer|tenho)\b",
        re.IGNORECASE,
    ),
}
# Other words for a clarification.
OTHER_TERMS = {
    "es": re.compile(r"\b(reclamos?|disputas?)\b", re.IGNORECASE),
    "pt": re.compile(r"\b(reclama[çc](ão|ões)|disputas?)\b", re.IGNORECASE),
}
PASSAGE = Passage(
    id="§2.1",
    rule="response_deadline",
    label={"es": "política de demostración, no del banco", "pt": "política de demonstração"},
    text={"es": "...", "pt": "..."},
    business_days=15,
)


def _texts(language: str) -> dict[str, str]:
    return {**_REPLIES[language], **{f"note:{k}": v for k, v in _NOTES[language].items()}}


@pytest.mark.parametrize("language", ["es", "pt"])
def test_the_bank_speaks_as_we(language: str) -> None:
    found = {
        k: m.group(0) for k, v in _texts(language).items() if (m := SINGULAR[language].search(v))
    }
    assert not found, found


@pytest.mark.parametrize("language", ["es", "pt"])
def test_a_clarification_has_one_name(language: str) -> None:
    found = {
        k: m.group(0) for k, v in _texts(language).items() if (m := OTHER_TERMS[language].search(v))
    }
    assert not found, found


@pytest.mark.parametrize("language", ["es", "pt"])
def test_the_policy_citation_is_a_simulated_label_on_its_own_line(language: str) -> None:
    facts = {
        "outcome": "registered_verified",
        "dispute": {"folio": "DSP-2026-00001", "due_date": "2026-07-08", "passage": "§2.1"},
    }

    note = deadline_note(facts, {"response_deadline": PASSAGE}, language)

    first, label = note.split("\n")
    assert "§2.1" not in first
    assert label == f"[simulado] §2.1 · {PASSAGE.label[language]}"


def test_the_web_speaks_as_we_with_one_term() -> None:
    i18n = Path("web/assets/i18n.js").read_text(encoding="utf-8")
    es, pt = i18n.split("\n  pt: {", 1)
    for text, language in ((es, "es"), (pt, "pt")):
        values = re.findall(r':\s*"([^"]*)"', text)
        singular = [v for v in values if SINGULAR[language].search(v)]
        others = [v for v in values if OTHER_TERMS[language].search(v)]
        assert not singular and not others, (language, singular, others)
    assert "Cuéntame" not in es and 'Entendí"' not in es


def test_the_portuguese_web_is_gender_neutral() -> None:
    pt = Path("web/assets/i18n.js").read_text(encoding="utf-8").split("\n  pt: {", 1)[1]
    gendered = re.compile(r"\bobrigad[oa]\b|\bbem-vind[oa]\b|\bajud[aá]-l[oa]\b", re.IGNORECASE)
    assert not gendered.findall(pt)


@pytest.mark.parametrize("language", ["es", "pt"])
def test_no_reply_asks_to_type_yes_now_that_the_screen_asks_with_buttons(language: str) -> None:
    typed_yes = re.compile(r"\b(responde\s+s[ií]|responda\s+sim)\b", re.IGNORECASE)
    assert not [k for k, v in _REPLIES[language].items() if typed_yes.search(v)]
    assert "confirm_register" not in _REPLIES[language]
    assert "confirm_block" not in _REPLIES[language]
    # The screen asks with its question and buttons: the confirmation has no fixed line either.
    assert "confirm_context" not in _REPLIES[language]

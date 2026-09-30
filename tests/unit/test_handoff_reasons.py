"""Every rule that hands a case to a person has a reason in the customer's words, in Spanish and
Portuguese, that names no internal rule (QA of TRZ-34)."""

import re

import pytest

from app.adapters.llm import HANDOFF_REASONS
from app.domain.policy import PolicyEngine

POLICY = PolicyEngine.from_file("config/policy.yaml").config
RULES = [f"escalate.{r}" for r in POLICY.escalate_if] + [
    f"approval.{r}" for r in POLICY.require_analyst_approval_if
]
# Words of the policy and the system the customer must not read.
INTERNAL = re.compile(
    r"\b(regla|rule|pol[ií]tica|autonom\w*|conformal|umbral|threshold|90|500|1000|escalad\w*)\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize("language", ["es", "pt"])
@pytest.mark.parametrize("rule", RULES)
def test_every_handoff_rule_has_a_reason_in_plain_words(language: str, rule: str) -> None:
    reason = HANDOFF_REASONS[language][rule]
    assert reason.endswith(".") and not INTERNAL.search(reason), reason


@pytest.mark.parametrize("language", ["es", "pt"])
def test_the_portuguese_reasons_are_gender_neutral(language: str) -> None:
    gendered = re.compile(r"\b\w+-(lo|la)\b|\bobrigad[oa]\b|\bbem-vind[oa]\b", re.IGNORECASE)
    assert not [r for r in HANDOFF_REASONS[language].values() if gendered.search(r)]

"""Plain-language case history (TRZ-26 CA4): every audited step has a line in Spanish and
Portuguese, written by code and without personal data."""

import ast
from pathlib import Path

import pytest

from app.domain.history import LANGS, TEMPLATES, describe

SRC = Path(__file__).resolve().parents[2] / "src"


def _audited_steps() -> set[tuple[str, str]]:
    """Every (actor, action) pair passed as literals to write_audit in the source tree."""
    steps: set[tuple[str, str]] = set()
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "write_audit"
                and len(node.args) >= 3
            ):
                actor, action = node.args[1], node.args[2]
                assert isinstance(actor, ast.Constant) and isinstance(action, ast.Constant), path
                steps.add((actor.value, action.value))
    return steps


def test_every_audited_step_has_a_template() -> None:
    steps = _audited_steps()
    assert len(steps) >= 12
    assert steps <= set(TEMPLATES), steps - set(TEMPLATES)


@pytest.mark.parametrize("step", sorted(TEMPLATES))
@pytest.mark.parametrize("lang", LANGS)
def test_every_template_writes_a_full_sentence_with_or_without_data(
    step: tuple[str, str], lang: str
) -> None:
    for payload, result in [(None, None), ({}, {})]:
        line = describe(*step, payload, result, "1", lang)  # type: ignore[arg-type]
        assert line.endswith(".")
        assert "{" not in line


def test_the_languages_differ() -> None:
    result = {"intent": "unrecognized_charge", "fallback": False}
    es = describe("agent", "extract", None, result, "1", "es")
    pt = describe("agent", "extract", None, result, "1", "pt")
    assert es == "El sistema entendió el mensaje del cliente como «cargo no reconocido»."
    assert pt == "O sistema entendeu a mensagem do cliente como «cobrança não reconhecida»."


def test_a_policy_line_cites_rule_version_and_level() -> None:
    result = {"level": "L3", "escalate": True, "rule": "over_l2_amount"}
    assert describe("policy", "decide", None, result, "1", "es") == (
        "La política v1, regla «over_l2_amount» fija el nivel L3 y envía el caso a una analista."
    )
    assert describe("policy", "decide", None, result, "1", "pt") == (
        "A política v1, regra «over_l2_amount» define o nível L3 e envia o caso a uma analista."
    )


def test_an_identification_line_gives_the_size_of_the_set() -> None:
    result = {"candidates": 14, "conformal_set": ["T1", "T2"], "decision": "show_options"}
    assert describe("tool", "identify_transaction", None, result, "1", "es") == (
        "El sistema comparó 14 cargos candidatos: "
        "quedaron 2 cargos posibles para mostrar al cliente."
    )


def test_a_step_with_no_template_still_gets_a_line() -> None:
    assert describe("system", "new_step", None, None, None, "es") == "Paso «new_step» de system."
    assert describe("system", "new_step", None, None, None, "pt") == "Etapa «new_step» de system."


@pytest.mark.parametrize("step", sorted(TEMPLATES))
@pytest.mark.parametrize("lang", LANGS)
def test_no_line_repeats_free_text_or_product_numbers(step: tuple[str, str], lang: str) -> None:
    secret = "SECRET-TEXT"
    payload = {
        "redacted_text": secret,
        "note": secret,
        "reason": secret,
        "customer_id": secret,
        "decision": "approve",
    }
    result = {
        "reply": secret,
        "blocked_products": [secret],
        "tx_id": secret,
        "reason": secret,
        "customer_id": secret,
    }
    assert secret not in describe(*step, payload, result, "1", lang)  # type: ignore[arg-type]

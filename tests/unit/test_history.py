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
    es = describe("agent", "comprehend", None, result, "1", "es")
    pt = describe("agent", "comprehend", None, result, "1", "pt")
    assert es == "El sistema entendió el mensaje del cliente como «cargo no reconocido»."
    assert pt == "O sistema entendeu a mensagem do cliente como «cobrança não reconhecida»."


def test_a_policy_line_cites_rule_version_action_and_level() -> None:
    result = {
        "action": "escalate",
        "level": "L3",
        "rule": "escalate.amount_above_human_review",
        "version": "2026.09.1",
    }
    assert describe("policy", "decide", None, result, "1", "es") == (
        "La política v2026.09.1, regla «escalate.amount_above_human_review», decidió enviar el "
        "caso a una analista (nivel L3)."
    )
    assert describe("policy", "decide", None, result, "1", "pt") == (
        "A política v2026.09.1, regra «escalate.amount_above_human_review», decidiu enviar o "
        "caso a uma analista (nível L3)."
    )


def test_a_confirmation_line_names_the_pending_action() -> None:
    result = {"pending_action": "register_and_block"}
    assert describe("agent", "confirm", None, result, "1", "es") == (
        "El cliente confirmó la acción pendiente: registrar y bloquear la tarjeta."
    )
    assert describe("agent", "confirm", None, {}, "1", "pt") == (
        "O cliente confirmou, mas não havia nenhuma ação pendente."
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

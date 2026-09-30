"""Plain-language case history (TRZ-26 CA4): every audited step has a line in Spanish and
Portuguese, written by code and without personal data."""

import ast
from pathlib import Path

import pytest

from app.domain.history import LANGS, REVERSAL_REASONS, TEMPLATES, describe
from app.domain.policy import load_policy

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


def test_recognition_lines_say_what_was_shown_and_chosen() -> None:
    shown = {"status": "Pending", "twin": {"status": "Approved"}, "earlier_months": ["2026-04"]}
    assert describe("tool", "show_charge_detail", None, shown, "1", "es") == (
        "El sistema mostró al cliente el detalle del cargo para reconocerlo "
        "(pendiente, con un cargo gemelo, meses anteriores del comercio: 1)."
    )
    said = {"waiting": True, "choice": "recognized"}
    assert describe("agent", "recognize", None, said, "1", "pt") == (
        "O cliente disse: «Já reconheço»."
    )


def test_choice_lines_never_name_the_id_chosen() -> None:
    assert describe("agent", "choose", None, {"option": "TX1", "shown": 2}, "1", "es") == (
        "El cliente eligió una de las 2 opciones mostradas."
    )
    assert describe("agent", "choose", None, {"option": None, "shown": 2}, "1", "es") == (
        "El cliente eligió un cargo que no estaba entre las opciones mostradas."
    )
    event = {"reason": "option_not_shown"}
    assert describe("agent", "security_event", event, {}, "1", "pt") == (
        "O cliente escolheu uma cobrança que não estava entre as opções: evento de segurança."
    )


def test_a_stale_confirmation_says_why_nothing_ran() -> None:
    replaced = {"action_status": "replaced"}
    assert describe("agent", "confirm", None, replaced, "1", "es") == (
        "El cliente confirmó una acción que otra ya había reemplazado: no se ejecutó."
    )
    again = {"action_status": "executed", "pending_action": "register"}
    assert describe("agent", "confirm", None, again, "1", "es") == (
        "El cliente confirmó otra vez una acción ya ejecutada: registrar la aclaración."
    )


def test_registration_and_block_lines() -> None:
    dispute = {"folio": "DSP-2026-00001", "due_date": "2026-07-09"}
    assert describe("tool", "register_dispute", None, dispute, "1", "es") == (
        "El sistema registró la aclaración con el folio DSP-2026-00001, con plazo de respuesta "
        "al 2026-07-09."
    )
    assert describe("tool", "block_card", None, {"status_after": "Blocked"}, "1", "pt") == (
        "O sistema bloqueou o cartão da cobrança contestada."
    )
    assert describe("tool", "block_card", None, {"message": "not_a_card"}, "1", "es") == (
        "El producto del cargo no es una tarjeta: no se bloqueó nada."
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


def test_a_decision_line_names_the_analyst_and_the_reason() -> None:
    payload = {"decision": "reject", "reason": "wrong_charge"}
    result = {"analyst": "analista.demo", "status": "rejected"}
    es = describe("human", "decision", payload, result, None, "es")
    pt = describe("human", "decision", payload, result, None, "pt")
    assert es == "La analista analista.demo decidió rechazar: cargo equivocado."
    assert pt == "A analista analista.demo decidiu rejeitar: cobrança errada."


def test_an_approval_line_tells_the_folio_and_the_block_left_out() -> None:
    result = {"analyst": "a", "folio": "DSP-2026-00001", "block_not_executed": True}
    line = describe("human", "decision", {"decision": "approve"}, result, None, "es")
    assert line == (
        "La analista a decidió aprobar. Se registró DSP-2026-00001 y se verificó. El bloqueo de "
        "tarjeta no se ejecutó: requiere la confirmación del cliente."
    )


def test_every_reversal_reason_of_the_policy_has_words() -> None:
    reasons = set(load_policy(SRC.parent / "config" / "policy.yaml").autonomy.reversal_reasons)
    for lang in LANGS:
        assert set(REVERSAL_REASONS[lang]) == reasons

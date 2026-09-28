"""Plain-language lines for the analyst's case history, written by code from audit rows.

Each (actor, action) pair has one template per language. A template reads only non-personal
fields (intent, rule, level, counts, outcome): never the redacted customer text, the reply, a
product number or an operator note. The LLM takes no part in this text.
"""

from collections.abc import Callable
from typing import Any, Literal

Lang = Literal["es", "pt"]
LANGS: tuple[Lang, ...] = ("es", "pt")

Fields = dict[str, Any]

_INTENTS: dict[Lang, dict[str, str]] = {
    "es": {
        "unrecognized_charge": "cargo no reconocido",
        "billing_error_amount": "cobro de un monto distinto",
        "billing_error_duplicate": "cobro duplicado",
        "claim_status": "estado de un reclamo",
        "out_of_scope": "fuera de alcance",
    },
    "pt": {
        "unrecognized_charge": "cobrança não reconhecida",
        "billing_error_amount": "cobrança de valor diferente",
        "billing_error_duplicate": "cobrança duplicada",
        "claim_status": "status de uma reclamação",
        "out_of_scope": "fora do escopo",
    },
}

_OUTCOMES: dict[Lang, dict[str, str]] = {
    "es": {
        "identifying": "buscando el cargo con el cliente",
        "awaiting_confirmation": "esperando la confirmación del cliente",
        "registered": "aclaración registrada",
        "pending_analyst_approval": "esperando la aprobación de una analista",
        "escalated": "enviado a una analista",
        "security_blocked": "detenido por seguridad",
        "abstained": "fuera de alcance, cliente redirigido",
        "informed": "informado al cliente",
        "no_pending_action": "no había nada que confirmar",
    },
    "pt": {
        "identifying": "buscando a cobrança com o cliente",
        "awaiting_confirmation": "aguardando a confirmação do cliente",
        "registered": "contestação registrada",
        "pending_analyst_approval": "aguardando a aprovação de uma analista",
        "escalated": "enviado a uma analista",
        "security_blocked": "interrompido por segurança",
        "abstained": "fora do escopo, cliente redirecionado",
        "informed": "informado ao cliente",
        "no_pending_action": "não havia nada para confirmar",
    },
}

_ACTIONS: dict[Lang, dict[str, str]] = {
    "es": {
        "register_and_offer_block": "registrar y ofrecer el bloqueo de la tarjeta",
        "register_and_block": "registrar y bloquear la tarjeta",
        "register": "registrar la aclaración",
        "explain_and_watch": "explicar la retención y esperar",
        "report_claim_status": "informar el estado del reclamo",
        "abstain_and_redirect": "abstenerse y redirigir",
        "analyst_approval": "pedir la aprobación de una analista",
        "escalate": "enviar el caso a una analista",
        "security_blocked": "detener el caso por seguridad",
    },
    "pt": {
        "register_and_offer_block": "registrar e oferecer o bloqueio do cartão",
        "register_and_block": "registrar e bloquear o cartão",
        "register": "registrar a contestação",
        "explain_and_watch": "explicar a retenção e aguardar",
        "report_claim_status": "informar o status da reclamação",
        "abstain_and_redirect": "abster-se e redirecionar",
        "analyst_approval": "pedir a aprovação de uma analista",
        "escalate": "enviar o caso a uma analista",
        "security_blocked": "interromper o caso por segurança",
    },
}

_DECISIONS: dict[Lang, dict[str, str]] = {
    "es": {"approve": "aprobar", "reject": "rechazar", "need_info": "pedir información"},
    "pt": {"approve": "aprovar", "reject": "rejeitar", "need_info": "pedir informações"},
}

_IDENTIFICATION: dict[Lang, dict[str, str]] = {
    "es": {
        "identified": "quedó un solo cargo posible",
        "show_options": "quedaron {n} cargos posibles para mostrar al cliente",
        "ask_for_detail": "quedaron {n} cargos posibles; hay que pedir más detalle",
        "not_found": "ningún cargo encaja con las pistas",
    },
    "pt": {
        "identified": "restou uma única cobrança possível",
        "show_options": "restaram {n} cobranças possíveis para mostrar ao cliente",
        "ask_for_detail": "restaram {n} cobranças possíveis; é preciso pedir mais detalhes",
        "not_found": "nenhuma cobrança corresponde às pistas",
    },
}


def _label(table: dict[Lang, dict[str, str]], lang: Lang, code: Any) -> str:
    return table[lang].get(str(code), str(code))


def _extract(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    intent = _label(_INTENTS, lang, r.get("intent"))
    if lang == "es":
        how = " (con reglas, sin LLM)" if r.get("fallback") else ""
        return f"El sistema entendió el mensaje del cliente como «{intent}»{how}."
    how = " (com regras, sem LLM)" if r.get("fallback") else ""
    return f"O sistema entendeu a mensagem do cliente como «{intent}»{how}."


def _compose(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if lang == "es":
        how = " con la plantilla fija" if r.get("fallback") else ""
        return f"El sistema redactó la respuesta al cliente{how}."
    how = " com o modelo fixo" if r.get("fallback") else ""
    return f"O sistema redigiu a resposta ao cliente{how}."


def _turn_complete(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    outcome = _label(_OUTCOMES, lang, r.get("outcome", "informed"))
    if lang == "es":
        return f"Terminó el turno: {outcome}."
    return f"Turno concluído: {outcome}."


def _decide(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    version = r.get("version") or policy or "?"
    rule, level = r.get("rule"), r.get("level")
    action = _label(_ACTIONS, lang, r.get("action"))
    if lang == "es":
        cited = f", regla «{rule}»," if rule else ""
        return f"La política v{version}{cited} decidió {action} (nivel {level})."
    cited = f", regra «{rule}»," if rule else ""
    return f"A política v{version}{cited} decidiu {action} (nível {level})."


def _confirm(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    pending = r.get("pending_action")
    if lang == "es":
        if not pending:
            return "El cliente confirmó, pero no había ninguna acción pendiente."
        return f"El cliente confirmó la acción pendiente: {_label(_ACTIONS, lang, pending)}."
    if not pending:
        return "O cliente confirmou, mas não havia nenhuma ação pendente."
    return f"O cliente confirmou a ação pendente: {_label(_ACTIONS, lang, pending)}."


def _human_decision(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    decision = _label(_DECISIONS, lang, p.get("decision"))
    if lang == "es":
        return f"Una analista decidió {decision}."
    return f"Uma analista decidiu {decision}."


def _identify(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    n = len(r.get("conformal_set") or [])
    outcome = _label(_IDENTIFICATION, lang, r.get("decision")).format(n=n)
    total = r.get("candidates", 0)
    if lang == "es":
        return f"El sistema comparó {total} cargos candidatos: {outcome}."
    return f"O sistema comparou {total} cobranças candidatas: {outcome}."


def _profile(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if lang == "es":
        return "El sistema consultó el perfil del cliente."
    return "O sistema consultou o perfil do cliente."


def _recent(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if lang == "es":
        return f"El sistema consultó {r.get('count', 0)} movimientos recientes."
    return f"O sistema consultou {r.get('count', 0)} movimentações recentes."


def _lookup(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if lang == "es":
        return f"El sistema buscó el cargo y encontró {r.get('count', 0)} coincidencias."
    return f"O sistema buscou a cobrança e encontrou {r.get('count', 0)} correspondências."


def _freeze(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    done = r.get("status_after") == "Blocked"
    if lang == "es":
        return (
            "El sistema bloqueó la tarjeta del cliente."
            if done
            else "El sistema no encontró una tarjeta activa para bloquear."
        )
    return (
        "O sistema bloqueou o cartão do cliente."
        if done
        else "O sistema não encontrou um cartão ativo para bloquear."
    )


def _dispute(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    done = r.get("dispute_status") == "opened"
    if lang == "es":
        return (
            "El sistema abrió una disputa sobre el cargo."
            if done
            else "El sistema no pudo abrir la disputa: no encontró el cargo."
        )
    return (
        "O sistema abriu uma contestação da cobrança."
        if done
        else "O sistema não conseguiu abrir a contestação: não encontrou a cobrança."
    )


def _escalate(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if lang == "es":
        return "El sistema envió el caso a una analista."
    return "O sistema enviou o caso a uma analista."


Template = Callable[[Lang, Fields, Fields, str | None], str]

# ("agent", "extract") and ("tool", "lookup_transaction") are no longer written, but the audit
# log is append-only and still holds rows of both, which the history must keep telling.
TEMPLATES: dict[tuple[str, str], Template] = {
    ("agent", "extract"): _extract,
    ("agent", "comprehend"): _extract,
    ("agent", "confirm"): _confirm,
    ("agent", "compose"): _compose,
    ("agent", "turn_complete"): _turn_complete,
    ("policy", "decide"): _decide,
    ("human", "decision"): _human_decision,
    ("tool", "identify_transaction"): _identify,
    ("tool", "get_customer_profile"): _profile,
    ("tool", "list_recent_transactions"): _recent,
    ("tool", "lookup_transaction"): _lookup,
    ("tool", "freeze_card"): _freeze,
    ("tool", "open_dispute"): _dispute,
    ("tool", "escalate_to_human"): _escalate,
}


def describe(
    actor: str,
    action: str,
    payload: Fields | None,
    result: Fields | None,
    policy_version: str | None,
    lang: Lang,
) -> str:
    """Writes one audit row as a sentence the analyst can read.

    Args:
        actor: Actor of the row.
        action: Action of the row.
        payload: Redacted inputs of the row.
        result: Outputs of the row.
        policy_version: Policy version the row cites, if any.
        lang: Language of the sentence.

    Returns:
        The sentence. A pair with no template gets a generic line, never an error, so a new
        action cannot hide the rest of the history.
    """
    template = TEMPLATES.get((actor, action))
    if template is None:
        if lang == "es":
            return f"Paso «{action}» de {actor}."
        return f"Etapa «{action}» de {actor}."
    return template(lang, payload or {}, result or {}, policy_version)

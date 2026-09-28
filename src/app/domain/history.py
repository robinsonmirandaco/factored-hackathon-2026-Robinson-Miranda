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
        "blocked_purchase": "compra bloqueada",
        "unrecognized_charge": "cargo no reconocido",
        "duplicate_charge": "cargo duplicado",
        "lost_or_stolen_card": "tarjeta perdida o robada",
        "general_inquiry": "consulta general",
        "unknown": "fuera de alcance",
    },
    "pt": {
        "blocked_purchase": "compra bloqueada",
        "unrecognized_charge": "cobrança não reconhecida",
        "duplicate_charge": "cobrança duplicada",
        "lost_or_stolen_card": "cartão perdido ou roubado",
        "general_inquiry": "consulta geral",
        "unknown": "fora do escopo",
    },
}

_OUTCOMES: dict[Lang, dict[str, str]] = {
    "es": {
        "auto_resolved": "resuelto por el sistema",
        "awaiting_customer": "esperando al cliente",
        "escalated": "enviado a una analista",
        "inform": "informado al cliente",
    },
    "pt": {
        "auto_resolved": "resolvido pelo sistema",
        "awaiting_customer": "aguardando o cliente",
        "escalated": "enviado a uma analista",
        "inform": "informado ao cliente",
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
    outcome = _label(_OUTCOMES, lang, r.get("outcome", "inform"))
    if lang == "es":
        return f"Terminó el turno: {outcome}."
    return f"Turno concluído: {outcome}."


def _decide(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    version = policy or "?"
    rule, level = r.get("rule"), r.get("level")
    if lang == "es":
        cited = f", regla «{rule}»" if rule else ""
        tail = " y envía el caso a una analista" if r.get("escalate") else ""
        return f"La política v{version}{cited} fija el nivel {level}{tail}."
    cited = f", regra «{rule}»" if rule else ""
    tail = " e envia o caso a uma analista" if r.get("escalate") else ""
    return f"A política v{version}{cited} define o nível {level}{tail}."


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

TEMPLATES: dict[tuple[str, str], Template] = {
    ("agent", "extract"): _extract,
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

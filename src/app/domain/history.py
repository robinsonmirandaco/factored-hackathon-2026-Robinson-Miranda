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
        "unread": "mensaje no leído",
    },
    "pt": {
        "unrecognized_charge": "cobrança não reconhecida",
        "billing_error_amount": "cobrança de valor diferente",
        "billing_error_duplicate": "cobrança duplicada",
        "claim_status": "status de uma reclamação",
        "out_of_scope": "fora do escopo",
        "unread": "mensagem não lida",
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
        "recognizing": "mostrando el detalle del cargo al cliente",
        "recognized_closed": "el cliente reconoció el cargo, caso cerrado sin acción",
        "no_pending_recognition": "no había ningún cargo esperando respuesta",
        "no_pending_choice": "no había opciones esperando elección",
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
        "recognizing": "mostrando o detalhe da cobrança ao cliente",
        "recognized_closed": "o cliente reconheceu a cobrança, caso encerrado sem ação",
        "no_pending_recognition": "não havia nenhuma cobrança aguardando resposta",
        "no_pending_choice": "não havia opções aguardando escolha",
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


_STALE: dict[Lang, dict[str, str]] = {
    "es": {
        "replaced": "El cliente confirmó una acción que otra ya había reemplazado: no se ejecutó.",
        "canceled": "El cliente confirmó una acción ya cancelada: no se ejecutó.",
    },
    "pt": {
        "replaced": "O cliente confirmou uma ação já substituída por outra: não foi executada.",
        "canceled": "O cliente confirmou uma ação já cancelada: não foi executada.",
    },
}


def _confirm(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    pending, status = r.get("pending_action"), r.get("action_status")
    if status in _STALE[lang]:
        return _STALE[lang][status]
    action = _label(_ACTIONS, lang, pending)
    if lang == "es":
        if not pending:
            return "El cliente confirmó, pero no había ninguna acción pendiente."
        if status == "executed":
            return f"El cliente confirmó otra vez una acción ya ejecutada: {action}."
        return f"El cliente confirmó la acción pendiente: {action}."
    if not pending:
        return "O cliente confirmou, mas não havia nenhuma ação pendente."
    if status == "executed":
        return f"O cliente confirmou de novo uma ação já executada: {action}."
    return f"O cliente confirmou a ação pendente: {action}."


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


def _register(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    folio, due = r.get("folio", "?"), r.get("due_date")
    if lang == "es":
        deadline = f", con plazo de respuesta al {due}" if due else ", sin plazo respaldado"
        return f"El sistema registró la aclaración con el folio {folio}{deadline}."
    deadline = f", com prazo de resposta até {due}" if due else ", sem prazo respaldado"
    return f"O sistema registrou a contestação com o protocolo {folio}{deadline}."


def _block(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    why = r.get("message")
    if lang == "es":
        if r.get("status_after") == "Blocked":
            return "El sistema bloqueó la tarjeta del cargo disputado."
        if why == "not_a_card":
            return "El producto del cargo no es una tarjeta: no se bloqueó nada."
        return "La tarjeta del cargo no estaba activa: no se bloqueó nada."
    if r.get("status_after") == "Blocked":
        return "O sistema bloqueou o cartão da cobrança contestada."
    if why == "not_a_card":
        return "O produto da cobrança não é um cartão: nada foi bloqueado."
    return "O cartão da cobrança não estava ativo: nada foi bloqueado."


def _escalate(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if lang == "es":
        return "El sistema envió el caso a una analista."
    return "O sistema enviou o caso a uma analista."


def _security_event(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if p.get("reason") == "option_not_shown":
        if lang == "es":
            return (
                "El cliente eligió un cargo que no estaba entre las opciones: evento de seguridad."
            )
        return (
            "O cliente escolheu uma cobrança que não estava entre as opções: evento de segurança."
        )
    if lang == "es":
        return "La petición intentó llegar a datos de otro cliente: evento de seguridad."
    return "A solicitação tentou acessar dados de outro cliente: evento de segurança."


def _show_charge(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    notes = []
    if r.get("status") == "Pending":
        notes.append("pendiente" if lang == "es" else "pendente")
    if r.get("twin"):
        notes.append("con un cargo gemelo" if lang == "es" else "com uma cobrança gêmea")
    if r.get("earlier_months"):
        n = len(r["earlier_months"])
        notes.append(
            f"meses anteriores del comercio: {n}"
            if lang == "es"
            else f"meses anteriores do estabelecimento: {n}"
        )
    extra = f" ({', '.join(notes)})" if notes else ""
    if lang == "es":
        return f"El sistema mostró al cliente el detalle del cargo para reconocerlo{extra}."
    return f"O sistema mostrou ao cliente o detalhe da cobrança para reconhecê-la{extra}."


def _recognize(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if not r.get("waiting"):
        if lang == "es":
            return "El cliente respondió al reconocimiento, pero no había ningún cargo esperando."
        return "O cliente respondeu ao reconhecimento, mas não havia cobrança aguardando."
    recognized = r.get("choice") == "recognized"
    if lang == "es":
        return (
            "El cliente dijo: «Ya lo reconozco»."
            if recognized
            else ("El cliente dijo: «Sigo sin reconocerlo».")
        )
    return (
        "O cliente disse: «Já reconheço»."
        if recognized
        else ("O cliente disse: «Continuo sem reconhecer».")
    )


def _choose(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    option = r.get("option")
    if option == "none":
        if lang == "es":
            return "El cliente dijo que ninguna de las opciones es el cargo."
        return "O cliente disse que nenhuma das opções é a cobrança."
    if option is None:
        if lang == "es":
            return "El cliente eligió un cargo que no estaba entre las opciones mostradas."
        return "O cliente escolheu uma cobrança que não estava entre as opções mostradas."
    if lang == "es":
        return f"El cliente eligió una de las {r.get('shown', 0)} opciones mostradas."
    return f"O cliente escolheu uma das {r.get('shown', 0)} opções mostradas."


def _case_expired(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if lang == "es":
        return "La sesión del cliente terminó: el caso quedó vencido, sin acción pendiente."
    return "A sessão do cliente terminou: o caso expirou, sem ação pendente."


def _document_locked(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    n = r.get("failed_attempts", "?")
    if lang == "es":
        return f"{n} códigos incorrectos seguidos: el documento quedó bloqueado por un tiempo."
    return f"{n} códigos incorretos seguidos: o documento ficou bloqueado por um tempo."


Template = Callable[[Lang, Fields, Fields, str | None], str]

# ("agent", "extract"), ("tool", "lookup_transaction"), ("tool", "freeze_card") and
# ("tool", "open_dispute") are no longer written, but the audit log is append-only and still
# holds rows of them, which the history must keep telling.
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
    ("tool", "register_dispute"): _register,
    ("tool", "block_card"): _block,
    ("tool", "escalate_to_human"): _escalate,
    ("agent", "security_event"): _security_event,
    ("agent", "recognize"): _recognize,
    ("agent", "choose"): _choose,
    ("tool", "show_charge_detail"): _show_charge,
    ("auth", "case_expired"): _case_expired,
    ("auth", "document_locked"): _document_locked,
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

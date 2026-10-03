"""Plain-language lines for the analyst's case history, written by code from audit rows.

Each (actor, action) pair has one template per language. A template reads only non-personal
fields (intent, rule, level, counts, outcome, the analyst's user name and closed-list reason):
never the redacted customer text, the reply, a product number or an operator note. The one
exception is a request for information: its question and the customer's answer, both redacted
before they are stored, are told so the answer can be read against its question (TRZ-28).
The LLM takes no part in this text.
"""

from collections.abc import Callable
from datetime import date
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
        "registered_verified": "aclaración registrada y verificada en la base",
        "failed": "verificación de registro fallida, enviado a una analista",
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
        "registered_verified": "contestação registrada e verificada na base",
        "failed": "verificação de registro falhou, enviado a uma analista",
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
        "block": "bloquear la tarjeta",
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
        "block": "bloquear o cartão",
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

# Words of the closed list of reasons an analyst rejects with (policy autonomy.reversal_reasons).
REVERSAL_REASONS: dict[Lang, dict[str, str]] = {
    "es": {
        "wrong_charge": "cargo equivocado",
        "should_not_act": "no debía actuar",
        "should_have_escalated": "debía escalar",
        "misunderstanding_unresolved": "malentendido sin resolver",
        "insufficient_data": "datos insuficientes",
        "other": "otro",
    },
    "pt": {
        "wrong_charge": "cobrança errada",
        "should_not_act": "não devia agir",
        "should_have_escalated": "devia escalar",
        "misunderstanding_unresolved": "mal-entendido não resolvido",
        "insufficient_data": "dados insuficientes",
        "other": "outro",
    },
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


_MONTHS: dict[Lang, tuple[str, ...]] = {
    "es": ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"),
    "pt": ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"),
}


def _day(lang: Lang, iso: Any) -> str:
    """A date of the records as the screens write it: "24 jun 2026", "24 de jun de 2026"."""
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)
    month = _MONTHS[lang][d.month - 1]
    return f"{d.day} {month} {d.year}" if lang == "es" else f"{d.day} de {month} de {d.year}"


def _label(table: dict[Lang, dict[str, str]], lang: Lang, code: Any) -> str:
    return table[lang].get(str(code), str(code))


def intent_label(intent: str | None, lang: Lang) -> str:
    """The intent of a case in plain words, as the history tells it.

    Args:
        intent: Intent code, such as unrecognized_charge.
        lang: Language of the words.

    Returns:
        The words, or the code itself when it has none.
    """
    return _label(_INTENTS, lang, intent)


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


def _existing_case(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if lang == "es":
        return "El cargo ya tenía un caso: el cliente volvió a ese caso en lugar de abrir otro."
    return "A cobrança já tinha um caso: o cliente voltou a esse caso em vez de abrir outro."


def _decline(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    pending = r.get("pending_action")
    if not pending:
        if lang == "es":
            return "El cliente dijo que no, pero no había ninguna acción pendiente."
        return "O cliente disse que não, mas não havia nenhuma ação pendente."
    action = _label(_ACTIONS, lang, pending)
    if lang == "es":
        return f"El cliente no aceptó la acción ofrecida: {action}. No se hizo nada."
    return f"O cliente não aceitou a ação oferecida: {action}. Nada foi feito."


def _human_decision(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if p.get("kind") == "audit_sample":
        return _audit_decision(lang, p, r)
    decision = _label(_DECISIONS, lang, p.get("decision"))
    who = r.get("analyst") or "?"
    es = lang == "es"
    line = f"La analista {who} decidió {decision}" if es else f"A analista {who} decidiu {decision}"
    # Only a reason of the closed list is told: any other text is left out.
    if reason := REVERSAL_REASONS[lang].get(str(p.get("reason"))):
        line += ": " + reason
    # The question is told so the answer that follows it can be read against it; it was
    # redacted before it was stored.
    if p.get("decision") == "need_info" and p.get("question"):
        line += f": «{p['question']}»"
    if r.get("folio"):
        line += (
            f". Se registró {r['folio']} y se verificó"
            if es
            else f". {r['folio']} foi registrada e verificada"
        )
    elif r.get("verified") is False:
        line += (
            ". La relectura no coincidió: el caso volvió a la cola"
            if es
            else ". A releitura não coincidiu: o caso voltou para a fila"
        )
    if r.get("block_not_executed"):
        line += (
            ". El bloqueo de tarjeta no se ejecutó: requiere la confirmación del cliente"
            if es
            else ". O bloqueio do cartão não foi executado: requer a confirmação do cliente"
        )
    if r.get("due_on"):
        line += (
            f". El cliente puede responder hasta el {_day(lang, r['due_on'])}"
            if es
            else f". O cliente pode responder até {_day(lang, r['due_on'])}"
        )
    return line + "."


def _audit_decision(lang: Lang, p: Fields, r: Fields) -> str:
    who = r.get("analyst") or "?"
    if p.get("decision") != "reject":
        if lang == "es":
            return (
                f"La analista {who} confirmó la muestra de auditoría: nada cambia para el cliente."
            )
        return f"A analista {who} confirmou a amostra de auditoria: nada muda para o cliente."
    reason = REVERSAL_REASONS[lang].get(str(p.get("reason")), "?")
    if lang == "es":
        return (
            f"La analista {who} revirtió la muestra de auditoría: {reason}. La aclaración pasó a "
            "revisión por una analista; la disputa registrada no se anula."
        )
    return (
        f"A analista {who} reverteu a amostra de auditoria: {reason}. A contestação passou para "
        "revisão por uma analista; a contestação registrada não é anulada."
    )


def _audit_draw(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    rho = f"{p.get('rho', 0):.2f}".replace(".", ",")
    draw = (
        f"n = {p.get('n')}, semilla {p.get('seed')}"
        if lang == "es"
        else (f"n = {p.get('n')}, semente {p.get('seed')}")
    )
    if r.get("selected"):
        if lang == "es":
            return f"Sorteo de auditoría ({draw}): el caso salió en la muestra (ρ = {rho})."
        return f"Sorteio de auditoria ({draw}): o caso saiu na amostra (ρ = {rho})."
    if lang == "es":
        return f"Sorteo de auditoría ({draw}): el caso no salió en la muestra (ρ = {rho})."
    return f"Sorteio de auditoria ({draw}): o caso não saiu na amostra (ρ = {rho})."


_NOTICES: dict[Lang, dict[str, str]] = {
    "es": {
        "approved": "la aprobación con su folio",
        "rejected": "el rechazo con su motivo",
        "info_requested": "la pregunta de la analista con su plazo",
        "audit_reversed": "que su aclaración está en revisión",
    },
    "pt": {
        "approved": "a aprovação com o protocolo",
        "rejected": "a rejeição com o motivo",
        "info_requested": "a pergunta da analista com o prazo",
        "audit_reversed": "que a contestação está em revisão",
    },
}


def _notify(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    what = _label(_NOTICES, lang, p.get("kind"))
    passed = r.get("fact_check_passed")
    if lang == "es":
        check = "verificado" if passed else "sin respaldo, se envió el texto sin cifras"
        return f"Se notificó al cliente en la app {what} (texto {check})."
    check = "verificado" if passed else "sem respaldo, foi enviado o texto sem números"
    return f"O cliente foi notificado no app sobre {what} (texto {check})."


def _automation_disabled(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if lang == "es":
        return (
            "La automatización estaba desactivada: el bloqueo ofrecido no se ejecutó y se indicó "
            "al cliente bloquear la tarjeta por el canal del banco."
        )
    return (
        "A automação estava desativada: o bloqueio oferecido não foi executado e o cliente foi "
        "orientado a bloquear o cartão pelo canal do banco."
    )


def _automation_switch(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    who = r.get("analyst") or "?"
    if r.get("after"):
        if lang == "es":
            return f"La analista {who} mandó todo a humano: la automatización quedó desactivada."
        return f"A analista {who} mandou tudo para humanos: a automação ficou desativada."
    if lang == "es":
        return f"La analista {who} reactivó la automatización."
    return f"A analista {who} reativou a automação."


def _mark_simulated(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    version = r.get("demo") or "?"
    if lang == "es":
        return (
            f"[simulado] Caso creado por el estado del demo ({version}) con turnos guionados y "
            "sin LLM."
        )
    return (
        f"[simulado] Caso criado pelo estado da demo ({version}) com turnos roteirizados e sem LLM."
    )


def _demo_reset(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    who = p.get("analyst") or "?"
    cases = r.get("cases_created", "?")
    if lang == "es":
        return f"[simulado] {who} reinició el demo: se crearon {cases} casos simulados."
    return f"[simulado] {who} reiniciou a demo: foram criados {cases} casos simulados."


def _info_reply(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    # The one line that tells free text: the answer, stored redacted, next to its question.
    answer = p.get("redacted_text")
    if lang == "es":
        said = f": «{answer}»; el caso" if answer else ": el caso"
        return f"El cliente respondió a la pregunta de la analista{said} volvió a la cola."
    said = f": «{answer}»; o caso" if answer else ": o caso"
    return f"O cliente respondeu à pergunta da analista{said} voltou para a fila."


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
        deadline = (
            f", con plazo de respuesta al {_day(lang, due)}" if due else ", sin plazo respaldado"
        )
        return f"El sistema registró la aclaración con el folio {folio}{deadline}."
    deadline = f", com prazo de resposta até {_day(lang, due)}" if due else ", sem prazo respaldado"
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


_VERIFIED_ACTIONS: dict[Lang, dict[str, str]] = {
    "es": {"register_dispute": "el registro de la aclaración", "block_card": "el bloqueo"},
    "pt": {"register_dispute": "o registro da contestação", "block_card": "o bloqueio"},
}


def _verify(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    action = _label(_VERIFIED_ACTIONS, lang, p.get("action"))
    if lang == "es":
        if r.get("verified"):
            return f"El sistema releyó la base y confirmó {action}."
        return (
            f"El sistema releyó la base y {action} no coincide con lo esperado: verificación de "
            "registro fallida."
        )
    if r.get("verified"):
        return f"O sistema releu a base e confirmou {action}."
    return (
        f"O sistema releu a base e {action} não confere com o esperado: verificação de registro "
        "falhou."
    )


_CLAIM_KINDS: dict[Lang, dict[str, str]] = {
    "es": {
        "folio": "folio",
        "passage": "pasaje de política",
        "date": "fecha",
        "card_digits": "dígitos de tarjeta",
        "deadline": "plazo",
        "amount": "monto",
        "merchant": "comercio",
        "number": "número",
        "forbidden_request": "pedido prohibido",
        "contact_promise": "promesa de contacto",
        "action_claim": "acción no realizada",
    },
    "pt": {
        "folio": "protocolo",
        "passage": "trecho da política",
        "date": "data",
        "card_digits": "dígitos do cartão",
        "deadline": "prazo",
        "amount": "valor",
        "merchant": "estabelecimento",
        "number": "número",
        "forbidden_request": "pedido proibido",
        "contact_promise": "promessa de contato",
        "action_claim": "ação não realizada",
    },
}


def _fact_check(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    kinds = sorted({_label(_CLAIM_KINDS, lang, c.get("kind")) for c in r.get("unsupported", [])})
    if lang == "es":
        if not kinds:
            return "El verificador de hechos aprobó la respuesta: cada dato tiene respaldo."
        what = ", ".join(kinds)
        if r.get("sent"):
            return f"El verificador encontró datos sin respaldo ({what}), en modo de observación."
        return f"El verificador bloqueó la respuesta por datos sin respaldo ({what})."
    if not kinds:
        return "O verificador de fatos aprovou a resposta: cada dado tem respaldo."
    what = ", ".join(kinds)
    if r.get("sent"):
        return f"O verificador encontrou dados sem respaldo ({what}), em modo de observação."
    return f"O verificador bloqueou a resposta por dados sem respaldo ({what})."


def _verification_failed(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if lang == "es":
        return "El caso ya estaba en la cola: queda como verificación de registro fallida."
    return "O caso já estava na fila: fica como verificação de registro falhou."


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
    if p.get("reason") == "instruction_in_text":
        if lang == "es":
            return "El mensaje traía una instrucción dirigida al sistema: evento de seguridad."
        return "A mensagem trazia uma instrução dirigida ao sistema: evento de segurança."
    if lang == "es":
        return "La petición intentó llegar a datos de otro cliente: evento de seguridad."
    return "A solicitação tentou acessar dados de outro cliente: evento de segurança."


def _read_beside_instruction(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if lang == "es":
        return (
            "Se leyó el resto del mensaje con las reglas locales, sin la instrucción, para la "
            "analista."
        )
    return "O resto da mensagem foi lido pelas regras locais, sem a instrução, para a analista."


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


def _open_claims(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    n = r.get("count", 0)
    if lang == "es":
        return f"El sistema leyó los reclamos abiertos del cliente: {n}."
    return f"O sistema leu as reclamações abertas do cliente: {n}."


def _choose(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if r.get("kind") == "claim":
        return _choose_claim(lang, r)
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


def _choose_claim(lang: Lang, r: Fields) -> str:
    if r.get("option") == "none":
        if lang == "es":
            return "El cliente dijo que ninguno de los reclamos mostrados es el suyo."
        return "O cliente disse que nenhuma das reclamações mostradas é a dele."
    if r.get("option") is None:
        if lang == "es":
            return "El cliente eligió un reclamo que no estaba entre los mostrados."
        return "O cliente escolheu uma reclamação que não estava entre as mostradas."
    if lang == "es":
        return f"El cliente eligió uno de los {r.get('shown', 0)} reclamos mostrados."
    return f"O cliente escolheu uma das {r.get('shown', 0)} reclamações mostradas."


def _merge_clues(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if not r.get("merged"):
        if lang == "es":
            return "El mensaje respondía a una pregunta, pero no había pistas anteriores que sumar."
        return "A mensagem respondia a uma pergunta, mas não havia pistas anteriores para somar."
    if lang == "es":
        return "El mensaje respondía a una pregunta: sus pistas se sumaron a las anteriores."
    return "A mensagem respondia a uma pergunta: as pistas dela se somaram às anteriores."


def _translate(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if lang == "es":
        return "El sistema tradujo al español el mensaje del cliente (traducción automática)."
    return "O sistema traduziu para o espanhol a mensagem do cliente (tradução automática)."


def _customer_note(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if lang == "es":
        return (
            "El cliente escribió con el caso ya en manos de una persona: se agregó al expediente."
        )
    return "O cliente escreveu com o caso já nas mãos de uma pessoa: foi incluído no dossiê."


def _button_press(lang: Lang, p: Fields, r: Fields, policy: str | None) -> str:
    if lang == "es":
        return "El cliente abrió el caso con el botón de un movimiento: el cargo llegó elegido."
    return "O cliente abriu o caso pelo botão de uma movimentação: a cobrança chegou escolhida."


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
    ("agent", "decline"): _decline,
    ("agent", "existing_case"): _existing_case,
    ("agent", "compose"): _compose,
    ("agent", "turn_complete"): _turn_complete,
    ("policy", "decide"): _decide,
    ("human", "decision"): _human_decision,
    ("policy", "audit_draw"): _audit_draw,
    ("system", "notify"): _notify,
    ("system", "mark_simulated"): _mark_simulated,
    ("human", "demo_reset"): _demo_reset,
    ("agent", "automation_disabled"): _automation_disabled,
    ("human", "automation_switch"): _automation_switch,
    ("customer", "info_reply"): _info_reply,
    ("tool", "identify_transaction"): _identify,
    ("tool", "get_customer_profile"): _profile,
    ("tool", "list_recent_transactions"): _recent,
    ("tool", "get_open_claims"): _open_claims,
    ("tool", "lookup_transaction"): _lookup,
    ("tool", "freeze_card"): _freeze,
    ("tool", "open_dispute"): _dispute,
    ("tool", "register_dispute"): _register,
    ("tool", "block_card"): _block,
    ("tool", "escalate_to_human"): _escalate,
    ("agent", "verify_action"): _verify,
    ("agent", "fact_check"): _fact_check,
    ("agent", "verification_failed"): _verification_failed,
    ("agent", "security_event"): _security_event,
    ("agent", "read_beside_instruction"): _read_beside_instruction,
    ("agent", "recognize"): _recognize,
    ("agent", "choose"): _choose,
    ("agent", "merge_clues"): _merge_clues,
    ("agent", "translate"): _translate,
    ("agent", "customer_note"): _customer_note,
    ("agent", "button_press"): _button_press,
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

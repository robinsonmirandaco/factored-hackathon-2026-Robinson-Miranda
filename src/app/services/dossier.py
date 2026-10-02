"""The dossier of a case handed to a person, built from the records it already has (TRZ-25).

Every field comes from a row: the case, its audit log (comprehension, merged clues,
identification, policy decision, customer profile, charge detail, read-back), its pending
actions, and the records they name. Nothing is written by the LLM except the Spanish translation
of a Portuguese message, which is made once, kept in the audit log and labeled automatic.

A case stopped for security shows no customer data: no message, fact or evidence (TRZ-27 CA8).
"""

from collections.abc import Iterable
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import (
    AuditRecord,
    Case,
    CaseAction,
    Customer,
    Dispute,
    InfoRequest,
    Product,
    Transaction,
)
from app.adapters.db.rates import rates_near
from app.adapters.llm import LLMClient
from app.core.errors import AppError
from app.domain.history import Lang, intent_label
from app.domain.money import money_text
from app.schemas.comprehension import DateClue
from app.schemas.dossier import (
    ActionTaken,
    AuditDraw,
    Clue,
    Dossier,
    Fact,
    Identification,
    InfoExchange,
    LaterMessage,
    OpenQuestion,
    RuleTriggered,
    ScoredCandidate,
    Source,
    Translation,
)
from app.services.cases import DISPUTE_INTENTS, HANDOFF_STATUSES
from app.services.clues import CLUE_FIELDS, last_clues

# A dossier opens while the case is with a person, waits for the customer's answer, or was
# decided by an analyst, who reads there what the decision did; also for a case the audit sample
# selected (TRZ-29), and for one whose audit an analyst reversed.
DOSSIER_STATUSES = (*HANDOFF_STATUSES, "awaiting_customer", "approved", "rejected", "in_review")

# The closed list of what the system could not confirm, in the analyst's language.
_QUESTIONS: dict[Lang, dict[str, str]] = {
    "es": {
        "security_event": "La petición intentó llegar a datos de otro cliente.",
        "charge_not_identified": "No se pudo identificar el cargo del que habla el cliente.",
        "clarifications_exhausted": "Tras dos aclaraciones, el cargo sigue sin distinguirse.",
        "amount_not_convertible": "El monto del cargo no se pudo convertir a USD.",
        "verification_mismatch": "La relectura de la acción no coincidió con lo esperado.",
        "dropped_clue": "Se descartaron pistas cuyo fragmento no estaba en el mensaje: {fields}.",
        "card_possession_unknown": "El cliente no dijo si tiene la tarjeta.",
        "comprehension_unavailable": "El LLM no respondió y las reglas no entendieron el mensaje.",
    },
    "pt": {
        "security_event": "A solicitação tentou acessar dados de outro cliente.",
        "charge_not_identified": "Não foi possível identificar a cobrança citada pelo cliente.",
        "clarifications_exhausted": "Após dois esclarecimentos, a cobrança ainda não se distingue.",
        "amount_not_convertible": "O valor da cobrança não pôde ser convertido para USD.",
        "verification_mismatch": "A releitura da ação não coincidiu com o esperado.",
        "dropped_clue": "Pistas cujo trecho não estava na mensagem foram descartadas: {fields}.",
        "card_possession_unknown": "O cliente não disse se está com o cartão.",
        "comprehension_unavailable": "O LLM não respondeu e as regras não entenderam a mensagem.",
    },
}
_SECURITY_SUMMARY: dict[Lang, str] = {
    "es": "Evento de seguridad: el caso se detuvo sin leer el mensaje.",
    "pt": "Evento de segurança: o caso foi interrompido sem ler a mensagem.",
}
_CARD: dict[Lang, dict[bool, str]] = {
    "es": {True: "con la tarjeta", False: "sin la tarjeta"},
    "pt": {True: "com o cartão", False: "sem o cartão"},
}


def get_dossier(session: Session, llm: LLMClient, case_id: str, lang: Lang) -> Dossier:
    """Builds the dossier of a case handed to a person (design 12).

    Args:
        session: Open session with the analyst role.
        llm: LLM client, for the translation of a Portuguese message the first time it is read.
        case_id: The case.
        lang: Language of the summary and the open questions.

    Returns:
        The dossier.

    Raises:
        AppError: 404 case_not_found; 409 case_not_escalated when the case is not with a person;
            503 db_unavailable when the database fails.
    """
    try:
        return _build(session, llm, case_id, lang)
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc


def _build(session: Session, llm: LLMClient, case_id: str, lang: Lang) -> Dossier:
    """Builds the dossier of a case handed to a person (design 12).

    Args:
        session: Open session with the analyst role.
        llm: LLM client, for the translation of a Portuguese message the first time it is read.
        case_id: The case.
        lang: Language of the summary and the open questions.

    Returns:
        The dossier.

    Raises:
        AppError: 404 case_not_found; 409 case_not_escalated when no person has had the case.
    """
    case = session.get(Case, case_id)
    if case is None:
        raise AppError("case_not_found", f"Case {case_id} not found.", 404)
    rows = list(
        session.execute(
            select(AuditRecord).where(AuditRecord.case_id == case.id).order_by(AuditRecord.id)
        ).scalars()
    )
    drawn = _last(rows, "policy", "audit_draw")
    sampled = drawn is not None and bool((drawn.result or {}).get("selected"))
    # A case the audit sample selected is with a person although the system resolved it.
    if case.status not in DOSSIER_STATUSES and not sampled:
        raise AppError("case_not_escalated", f"Case is {case.status}, not with a person.", 409)
    decide = _last(rows, "policy", "decide")
    rule = (
        RuleTriggered(
            rule=decide.result["rule"],
            version=decide.result["version"],
            level=decide.result["level"],
            autonomy_level=decide.result.get("autonomy_level"),
        )
        if decide and decide.result
        else None
    )
    # Told by its rule, not its status: a security event an analyst closed is still one (CA8).
    if case.status == "security_blocked" or (rule and rule.rule.startswith("security.")):
        return Dossier(
            case_id=case.id,
            trace_id=case.trace_id,
            case_kind="security_event",
            language=case.language,
            original_message=None,
            machine_translation=None,
            request_summary=_SECURITY_SUMMARY[lang],
            verified_facts=[],
            extraction=[],
            identification=None,
            actions_taken=[],
            evidence=[],
            open_questions=[_question("security_event", lang)],
            policy_rule_triggered=rule,
            recommended_action=None,
            later_messages=[],
            info_exchanges=[],
            simulated=case.simulated,
            charge_identified=False,
        )
    read = [r for r in rows if (r.actor, r.action) == ("agent", "comprehend")]
    original = (read[0].payload or {}).get("redacted_text") if read else None
    clues = last_clues(session, case)
    extraction = [
        Clue(
            field=k,
            value=_value(getattr(clues.clues, k)),
            evidence=getattr(clues.clues, k).evidence,
            read_in=clues.sources[k],
        )
        for k in CLUE_FIELDS
        if clues and getattr(clues.clues, k) is not None and k in clues.sources
    ]
    # The same test as the open question charge_not_identified.
    charge = not (case.intent in DISPUTE_INTENTS and case.transaction_id is None)
    return Dossier(
        case_id=case.id,
        trace_id=case.trace_id,
        case_kind="audit_sample" if sampled else "escalation",
        simulated=case.simulated,
        language=case.language,
        original_message=original,
        machine_translation=(
            _translation(session, llm, case, rows, read[0].id, original)
            if case.language == "pt" and read and original
            else None
        ),
        request_summary=_summary(case, extraction, lang),
        verified_facts=_facts(session, case, decide),
        extraction=extraction,
        identification=_identification(_last(rows, "tool", "identify_transaction")),
        actions_taken=_actions(session, case, rows),
        evidence=_evidence(session, case, rows),
        open_questions=_open_questions(case, rule, read, lang),
        policy_rule_triggered=rule,
        # The policy names the routing of the intent even when no charge matched; without a
        # charge there is nothing to register (demo rehearsal).
        recommended_action=case.recommended_action if charge else None,
        charge_identified=charge,
        later_messages=[
            LaterMessage(
                text=(r.payload or {}).get("redacted_text", ""),
                source=Source(table="audit_log", id=str(r.id)),
            )
            for r in rows
            if (r.actor, r.action) == ("agent", "customer_note")
        ],
        info_exchanges=_info_exchanges(session, case),
        audit_draw=(
            AuditDraw(
                seed=drawn.payload["seed"],
                n=drawn.payload["n"],
                rho=drawn.payload["rho"],
                u=drawn.result["u"],
                source=Source(table="audit_log", id=str(drawn.id)),
            )
            if sampled and drawn is not None and drawn.payload and drawn.result
            else None
        ),
    )


def _info_exchanges(session: Session, case: Case) -> list[InfoExchange]:
    """Each question of an analyst with the customer's answer, in the order they were asked."""
    return [
        InfoExchange(
            question=r.question,
            asked_by=r.asked_by,
            asked_on=r.asked_on,
            due_on=r.due_on,
            status=r.status,
            answer=r.answer,
            source=Source(table="info_requests", id=str(r.id)),
        )
        for r in session.execute(
            select(InfoRequest).where(InfoRequest.case_id == case.id).order_by(InfoRequest.id)
        ).scalars()
    ]


def _last(rows: list[AuditRecord], actor: str, action: str) -> AuditRecord | None:
    return next((r for r in reversed(rows) if (r.actor, r.action) == (actor, action)), None)


def _value(clue: Any) -> Any:
    value = clue.model_dump(mode="json", exclude={"evidence"})
    if isinstance(clue, DateClue):
        # The days the words mean, as the customer's chip gives them.
        first, last = clue.window()
        value |= {"window_from": first.isoformat(), "window_to": last.isoformat()}
    return value


def _translation(
    session: Session,
    llm: LLMClient,
    case: Case,
    rows: list[AuditRecord],
    read_id: int,
    original: str,
) -> Translation:
    """The Spanish translation of the first message, made once and kept in the audit log."""
    done = next(
        (r for r in rows if (r.actor, r.action) == ("agent", "translate") and r.result), None
    )
    if done is not None:
        return Translation(
            text=done.result["text"], model=done.model, prompt_version=done.prompt_version
        )
    text, stats = llm.translate(original)
    if text is None:
        # Not kept: the next reading of the dossier tries again.
        return Translation(text=None, model=stats.model, error=stats.error or "no translation")
    write_audit(
        session,
        "agent",
        "translate",
        case.id,
        {"read_id": read_id},
        {"text": text},
        stats.latency_ms,
        customer_id=case.customer_id,
        llm=stats,
    )
    return Translation(text=text, model=stats.model, prompt_version=stats.prompt_version)


def _summary(case: Case, extraction: list[Clue], lang: Lang) -> str:
    """The request in one sentence, from the intent and the clues; code, not the LLM."""
    parts = []
    for clue in extraction:
        v = clue.value
        if clue.field == "amount":
            about = "~" if v.get("approximate") else ""
            parts.append(f"{about}{money_text(lang, v['value'], v.get('currency'))}")
        elif clue.field == "date":
            parts.append(str(v["expression"]))
        elif clue.field == "card_in_possession":
            parts.append(_CARD[lang][bool(v["value"])])
        else:
            parts.append(str(v["value"]))
    label = intent_label(case.intent, lang)
    return f"{label[:1].upper()}{label[1:]}" + (f": {', '.join(parts)}." if parts else ".")


def _facts(session: Session, case: Case, decide: AuditRecord | None) -> list[Fact]:
    facts: list[Fact] = []
    customer = session.get(Customer, case.customer_id)
    if customer is not None:
        src = Source(table="customers", id=customer.customer_id)
        facts += [
            Fact(name="customer_segment", value=customer.segment, source=src),
            Fact(name="customer_country", value=customer.country_code, source=src),
        ]
    tx = session.get(Transaction, case.transaction_id) if case.transaction_id else None
    if tx is not None:
        src = Source(table="transactions", id=tx.transaction_id)
        on = tx.transaction_date.date()
        facts += [
            Fact(name="amount", value=tx.amount, source=src),
            Fact(name="currency", value=tx.currency, source=src),
            Fact(name="merchant", value=tx.merchant_name, source=src),
            Fact(name="date", value=on.isoformat(), source=src),
            Fact(name="channel", value=tx.channel, source=src),
            Fact(name="status", value=tx.transaction_status, source=src),
        ]
        usd = ((decide.payload if decide else None) or {}).get("amount_usd")
        rate = _rate_source(session, tx, on) if usd is not None else None
        if rate is not None:
            facts.append(Fact(name="amount_usd", value=usd, source=rate))
    for d in session.execute(
        select(Dispute).where(Dispute.case_id == case.id).order_by(Dispute.id)
    ).scalars():
        src = Source(table="disputes", id=d.folio or str(d.id))
        facts += [
            Fact(name="dispute_status", value=d.status, source=src),
            Fact(name="dispute_due_date", value=_iso(d.due_date), source=src),
        ]
    return facts


def _rate_source(session: Session, tx: Transaction, on: date) -> Source | None:
    """The exchange rate the USD amount was converted with: the latest one up to the charge
    date, as the conversion takes it. None when no rate is found, so no source is made up."""
    if tx.currency == "USD":
        return Source(table="transactions", id=tx.transaction_id)
    rates = rates_near(session, on, {(tx.currency, "USD")})
    used = max((d for d, s, t in rates if (s, t) == (tx.currency, "USD")), default=None)
    if used is None:
        return None
    return Source(table="exchange_rates", id=f"{used.isoformat()}/{tx.currency}/USD")


def _evidence(session: Session, case: Case, rows: list[AuditRecord]) -> list[Fact]:
    evidence: list[Fact] = []
    detail = _last(rows, "tool", "show_charge_detail")
    twin = ((detail.result if detail else None) or {}).get("twin")
    if twin:
        evidence.append(
            Fact(
                name="identical_charge",
                value=twin["status"],
                source=Source(table="transactions", id=twin["transaction_id"]),
            )
        )
    tx = session.get(Transaction, case.transaction_id) if case.transaction_id else None
    product = session.get(Product, tx.product_id) if tx is not None else None
    if product is not None:
        evidence.append(
            Fact(
                name="product_status",
                value=product.product_status,
                source=Source(table="products", id=product.product_id),
            )
        )
    profile = _last(rows, "tool", "get_customer_profile")
    ids = ((profile.result if profile else None) or {}).get("open_dispute_ids") or {}
    evidence += [
        Fact(name="open_dispute", value="open", source=Source(table="complaints", id=i))
        for i in ids.get("complaints", [])
    ]
    evidence += [
        Fact(name="open_dispute", value="opened", source=Source(table="disputes", id=i))
        for i in ids.get("disputes", [])
    ]
    return evidence


def _identification(row: AuditRecord | None) -> Identification | None:
    if row is None or not row.result:
        return None
    r = row.result
    return Identification(
        decision=r["decision"],
        candidates=r["candidates"],
        conformal_set=list(r["conformal_set"]),
        top=[ScoredCandidate(**c) for c in r.get("top", [])],
    )


def _actions(session: Session, case: Case, rows: list[AuditRecord]) -> list[ActionTaken]:
    checks = [r.verified for r in rows if (r.actor, r.action) == ("agent", "verify_action")]
    out = []
    for a in session.execute(
        select(CaseAction).where(CaseAction.case_id == case.id).order_by(CaseAction.created_at)
    ).scalars():
        if a.status != "executed":
            state = "not_executed"
        elif not checks:
            state = "not_verified"
        else:
            state = "verified" if all(checks) else "failed"
        out.append(
            ActionTaken(action=a.action, state=state, source=Source(table="case_actions", id=a.id))
        )
    # An analyst's approval registers without a case_actions row, and never blocks the card.
    for r in rows:
        done = r.result or {}
        if (r.actor, r.action) != ("human", "decision") or "verified" not in done:
            continue
        src = Source(table="audit_log", id=str(r.id))
        out.append(
            ActionTaken(
                action="register", state="verified" if done["verified"] else "failed", source=src
            )
        )
        if done.get("block_not_executed"):
            out.append(ActionTaken(action="block", state="not_executed", source=src))
    return out


def _open_questions(
    case: Case, rule: RuleTriggered | None, read: Iterable[AuditRecord], lang: Lang
) -> list[OpenQuestion]:
    codes: list[str] = []
    if case.intent in DISPUTE_INTENTS and case.transaction_id is None:
        codes.append("charge_not_identified")
    if rule and rule.rule == "escalate.clarifications_exhausted":
        codes.append("clarifications_exhausted")
    if rule and rule.rule == "escalate.comprehension_unavailable":
        codes.append("comprehension_unavailable")
    if rule and rule.rule == "escalate.amount_unknown":
        codes.append("amount_not_convertible")
    if case.status == "failed":
        codes.append("verification_mismatch")
    if case.intent == "unrecognized_charge" and case.card_in_possession is None:
        codes.append("card_possession_unknown")
    questions = [_question(c, lang) for c in codes]
    dropped = sorted({f for r in read for f in (r.result or {}).get("dropped_clues") or []})
    if dropped:
        questions.append(_question("dropped_clue", lang, fields=", ".join(dropped)))
    return questions


def _question(code: str, lang: Lang, **fields: str) -> OpenQuestion:
    return OpenQuestion(code=code, text=_QUESTIONS[lang][code].format(**fields))


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None

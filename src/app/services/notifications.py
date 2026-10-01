"""In-app notifications of the decisions on a customer's clarifications (TRZ-32).

Code writes every text from a fixed template in the language of the case, with the facts of the
decision: the folio it registered or the day the answer is due. The text passes the fact
checker before it is stored; a text the checker does not back is replaced by one with no
figures. The LLM takes no part, and the analyst's free text never enters a notification: the
question is read in Mis aclaraciones.
"""

from datetime import date
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import Case, Notification
from app.core.errors import AppError
from app.core.time import utcnow
from app.domain.fact_check import VerifiedFacts, unsupported
from app.domain.recognition import long_date
from app.schemas.api import NotificationOut, NotificationsOut

Kind = Literal["approved", "rejected", "info_requested", "audit_reversed"]

# Reasons of the closed list told in plain words to the customer.
REASONS: dict[str, dict[str, str]] = {
    "es": {
        "wrong_charge": "el cargo identificado no era el correcto",
        "should_not_act": "no correspondía registrar esta aclaración",
        "should_have_escalated": "necesitaba otra revisión antes de registrarse",
        "misunderstanding_unresolved": "no quedó claro qué cargo querías aclarar",
        "insufficient_data": "no hubo datos suficientes para registrarla",
        "other": "la analista no encontró base para registrarla",
    },
    "pt": {
        "wrong_charge": "a cobrança identificada não era a correta",
        "should_not_act": "não cabia registrar esta contestação",
        "should_have_escalated": "precisava de outra revisão antes do registro",
        "misunderstanding_unresolved": "não ficou claro qual cobrança você queria esclarecer",
        "insufficient_data": "não houve dados suficientes para o registro",
        "other": "a analista não encontrou base para o registro",
    },
}

_TEMPLATES: dict[str, dict[Kind, str]] = {
    "es": {
        "approved": "Una analista aprobó tu aclaración: quedó registrada con el folio {folio}.",
        "rejected": "Una analista revisó tu aclaración y no procedió: {reason}.",
        "info_requested": (
            "Una analista necesita un dato más sobre tu aclaración. "
            "Respóndele en Mis aclaraciones antes del {due}."
        ),
        "audit_reversed": "Tu aclaración está en revisión por una analista.",
    },
    "pt": {
        "approved": (
            "Uma analista aprovou a sua contestação: foi registrada com o protocolo {folio}."
        ),
        "rejected": "Uma analista revisou a sua contestação e ela não foi aceita: {reason}.",
        "info_requested": (
            "Uma analista precisa de mais um dado sobre a sua contestação. "
            "Responda em Minhas contestações até {due}."
        ),
        "audit_reversed": "A sua contestação está em revisão por uma analista.",
    },
}

# Written when the checker does not back a text: nothing in it can be wrong.
_FALLBACK = {
    "es": "Hay novedades en tu aclaración. Revísala en Mis aclaraciones.",
    "pt": "Há novidades na sua contestação. Veja em Minhas contestações.",
}


def compose(
    kind: Kind,
    language: str,
    folio: str | None = None,
    reason: str | None = None,
    due: date | None = None,
) -> tuple[str, bool]:
    """Writes the text of a notification and checks it against the facts of the decision.

    Args:
        kind: What was decided.
        language: es or pt; anything else is told in Spanish.
        folio: For an approval, the folio registered and verified.
        reason: For a rejection, the reason of the closed list.
        due: For a request for information, the last day to answer.

    Returns:
        The text and whether the fact checker backed it; when it did not, the text is the one
        with no figures.
    """
    lang = language if language in _TEMPLATES else "es"
    text = _TEMPLATES[lang][kind].format(
        folio=folio or "",
        reason=REASONS[lang].get(reason or "", REASONS[lang]["other"]),
        due=long_date(due, lang) if due else "",
    )
    facts = VerifiedFacts(
        folios=frozenset({folio} if folio else ()),
        dates=frozenset({due} if due else ()),
        actions=frozenset({"register_dispute"} if kind == "approved" and folio else ()),
    )
    if unsupported(text, facts):
        return _FALLBACK[lang], False
    return text, True


def notify(
    session: Session,
    case: Case,
    kind: Kind,
    source_key: str,
    folio: str | None = None,
    reason: str | None = None,
    due: date | None = None,
) -> None:
    """Stores the notification of a decision for the customer of the case, once per source.

    Args:
        session: Session with the analyst role, in the transaction of the decision.
        case: The case decided.
        kind: What was decided.
        source_key: The decision it comes from; a second call with it stores nothing.
        folio: For an approval, the folio registered and verified.
        reason: For a rejection, the reason of the closed list.
        due: For a request for information, the last day to answer.
    """
    exists = session.execute(
        select(Notification.id).where(Notification.source_key == source_key)
    ).scalar_one_or_none()
    if exists is not None:
        return
    language = case.language if case.language in _TEMPLATES else "es"
    text, checked = compose(kind, language, folio, reason, due)
    session.add(
        Notification(
            customer_id=case.customer_id,
            case_id=case.id,
            kind=kind,
            language=language,
            text=text,
            checked=checked,
            source_key=source_key,
        )
    )
    write_audit(
        session,
        "system",
        "notify",
        case.id,
        {"kind": kind, "language": language, "source_key": source_key},
        {"fact_check_passed": checked},
        customer_id=case.customer_id,
    )


def list_notifications(session: Session, customer_id: str) -> NotificationsOut:
    """The customer's notifications, newest first, with the number not read yet.

    Args:
        session: Session bound to the customer of the JWT.
        customer_id: Customer of the session.

    Returns:
        The notifications and the unread count.

    Raises:
        AppError: 503 db_unavailable if the database fails.
    """
    try:
        rows = (
            session.execute(
                select(Notification)
                .where(Notification.customer_id == customer_id)
                .order_by(Notification.created_at.desc(), Notification.id.desc())
            )
            .scalars()
            .all()
        )
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc
    return NotificationsOut(
        items=[_out(n) for n in rows], unread=sum(1 for n in rows if n.read_at is None)
    )


def mark_read(session: Session, customer_id: str, notification_id: int) -> NotificationOut:
    """Marks one of the customer's notifications as read; a second call changes nothing.

    Args:
        session: Session bound to the customer of the JWT.
        customer_id: Customer of the session.
        notification_id: The notification.

    Returns:
        The notification as read.

    Raises:
        AppError: 404 notification_not_found, also for another customer's (row level security
            hides it); 503 db_unavailable.
    """
    try:
        session.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.customer_id == customer_id,
                Notification.read_at.is_(None),
            )
            .values(read_at=utcnow())
        )
        row = session.execute(
            select(Notification).where(
                Notification.id == notification_id, Notification.customer_id == customer_id
            )
        ).scalar_one_or_none()
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc
    if row is None:
        raise AppError("notification_not_found", "Notification not found.", 404)
    return _out(row)


def _out(n: Notification) -> NotificationOut:
    return NotificationOut(
        id=n.id,
        case_id=n.case_id,
        kind=n.kind,  # type: ignore[arg-type]
        text=n.text,
        created_at=n.created_at,
        read=n.read_at is not None,
    )

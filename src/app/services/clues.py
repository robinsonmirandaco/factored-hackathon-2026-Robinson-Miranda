"""The clues a case has gathered so far, read from its audit log (TRZ-25).

A message that answers a question of the case adds its clues to the earlier ones; the dossier
shows each clue with the message it was read in. Both read the same rows: the last comprehension
or merge row of the case.
"""

from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.models import AuditRecord, Case
from app.schemas.comprehension import Comprehension

# The clues a later message may add to, one per field.
CLUE_FIELDS = ("amount", "date", "merchant_hint", "channel_hint", "card_in_possession")


@dataclass(frozen=True)
class Clues:
    """The clues of a case and where each one was read.

    Attributes:
        clues: Intent, language and clues, as a comprehension.
        sources: For each clue present, the id of the comprehension row it was read in.
    """

    clues: Comprehension
    sources: dict[str, int]


def last_clues(session: Session, case: Case) -> Clues | None:
    """The clues of the case so far.

    Read from the last comprehension or merge row of this case and this customer; the audit log
    under row level security holds no other customer's rows anyway.

    Args:
        session: Open session bound to the case customer or to the analyst role.
        case: The case.

    Returns:
        The clues, or None when the case has no such row or it does not validate, such as a row
        of an older schema.
    """
    rows = session.execute(
        select(AuditRecord)
        .where(
            AuditRecord.case_id == case.id,
            AuditRecord.customer_id == case.customer_id,
            AuditRecord.actor == "agent",
            AuditRecord.action.in_(("comprehend", "merge_clues")),
        )
        .order_by(AuditRecord.id.desc())
    ).scalars()
    # A merge that found nothing to add to left the clues of its own message, the row before it.
    row = next(
        (r for r in rows if r.action == "comprehend" or (r.result or {}).get("merged")), None
    )
    if row is None or row.result is None:
        return None
    merged = row.action == "merge_clues"
    fields = row.result.get("clues", {}) if merged else row.result
    try:
        clues = Comprehension.model_validate(
            {k: fields.get(k) for k in ("intent", "language", *CLUE_FIELDS)}
        )
    except ValidationError:
        return None
    if merged:
        return Clues(clues, {k: int(v) for k, v in row.result.get("sources", {}).items()})
    return Clues(clues, {k: row.id for k in CLUE_FIELDS if getattr(clues, k) is not None})

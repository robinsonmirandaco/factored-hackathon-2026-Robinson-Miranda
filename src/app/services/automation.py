"""Global automation switch (TRZ-35, design 11.5): everything goes to a person without a
redeploy.

The switch is one row of the database, read on every policy decision and on every confirmation,
so a change takes effect on the next turn. Each change writes one audit row with the analyst,
the value before and the value after; sending the value it already has changes nothing.
"""

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import AutomationSwitch
from app.core.errors import AppError
from app.core.time import utcnow
from app.schemas.api import AutomationOut


def all_to_human(session: Session) -> bool:
    """Tells whether the switch sends every dispute to a person.

    Args:
        session: Any open session; every role reads the switch.

    Returns:
        True when automation is turned off.
    """
    value = session.execute(
        select(AutomationSwitch.all_to_human).where(AutomationSwitch.id == 1)
    ).scalar_one_or_none()
    return bool(value)


def get_switch(session: Session) -> AutomationOut:
    """Reads the switch.

    Args:
        session: Session with the analyst role.

    Returns:
        Its state and its last change.

    Raises:
        AppError: 503 db_unavailable if the database fails.
    """
    try:
        return _out(_row(session, lock=False))
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc


def set_switch(session: Session, analyst: str, value: bool) -> AutomationOut:
    """Turns the switch on or off and writes the change to the audit log (CA3).

    Args:
        session: Session with the analyst role.
        analyst: User name of the analyst session.
        value: True sends every dispute to a person.

    Returns:
        The state after the change; the same state, and no audit row, when it already had it.

    Raises:
        AppError: 503 db_unavailable if the database fails.
    """
    try:
        row = _row(session, lock=True)
        if row.all_to_human == value:
            return _out(row)
        before = row.all_to_human
        row.all_to_human, row.changed_by, row.changed_at = value, analyst, utcnow()
        write_audit(
            session,
            "human",
            "automation_switch",
            None,
            {"all_to_human": value},
            {"before": before, "after": value, "analyst": analyst},
        )
        return _out(row)
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc


def _row(session: Session, lock: bool) -> AutomationSwitch:
    query = select(AutomationSwitch).where(AutomationSwitch.id == 1)
    row = session.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if row is None:
        # Migration 0016 inserts the row; without it the database is not the expected one.
        raise AppError("db_unavailable", "The automation switch row is missing.", 503)
    return row


def _out(row: AutomationSwitch) -> AutomationOut:
    return AutomationOut(
        all_to_human=row.all_to_human, changed_by=row.changed_by, changed_at=row.changed_at
    )

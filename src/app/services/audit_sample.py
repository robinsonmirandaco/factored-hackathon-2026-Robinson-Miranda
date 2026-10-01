"""Draws the audit sample of a case the system resolved on its own (TRZ-29, design 6.7).

Each case the agent registers and verifies after the customer's confirmation is drawn once. The
draw is written to the audit log with the seed, its number, rho and the number drawn, so the
trace shows it and anyone can recompute it. A selected case gets a queue row of kind
audit_sample; the case itself stays as it is.
"""

from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import Case, QueueItem
from app.core.time import utcnow
from app.domain.audit_sample import draw
from app.domain.policy import PolicyConfig

AUDIT_SAMPLE_REASON = "audit.sample"


def sample(session: Session, policy: PolicyConfig, case: Case) -> bool:
    """Draws whether the case goes to the queue as an audit sample.

    Args:
        session: Session of the turn that resolved the case.
        policy: The policy: rho, seed and the SLA of a normal priority.
        case: The case, just registered and verified.

    Returns:
        True when the case was selected.
    """
    n = int(session.execute(text("SELECT nextval('audit_draw_seq')")).scalar_one())
    d = draw(policy.autonomy.audit_sample_seed, n, policy.autonomy.audit_sample_rate)
    write_audit(
        session,
        "policy",
        "audit_draw",
        case.id,
        {"seed": d.seed, "n": d.n, "rho": d.rho},
        {"u": d.u, "selected": d.selected},
        idempotency_key=f"{case.id}:audit_draw",
    )
    if d.selected:
        session.add(
            QueueItem(
                case_id=case.id,
                customer_id=case.customer_id,
                kind="audit_sample",
                reason=AUDIT_SAMPLE_REASON,
                priority="normal",
                sla_due_at=utcnow() + timedelta(hours=policy.queue.sla_hours["normal"]),
            )
        )
    return d.selected

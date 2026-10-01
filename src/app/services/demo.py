"""Demo state (TRZ-38): who the demo people are, the cases the demo starts with, and the reset.

`make seed-demo` chooses the people by the rules of config/demo.yaml and writes their invented
documents. The reset empties the operational tables and creates the starting cases again
through the real agent and the real analyst decisions, never by inserting rows: each case
leaves the same trace a customer's would, and is marked simulated. The scripted turns run with
the LLM off, so a reset always gives the same state and costs nothing.

Nothing here reads an evaluation split: the customers of every case come from
`case_customers.txt`, a list of ids that `make cases` writes next to the splits.
"""

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.errors import UndefinedFunction, UndefinedTable
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import Case
from app.adapters.db.session import Database, bind_context
from app.adapters.llm import LLMClient
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.demo import ChargeRule, DemoConfig, Reject, Script, Seeded
from app.domain.pii import document_hash
from app.schemas.api import DemoResetOut, HumanDecisionIn
from app.services.agent import AgentDeps, handle_message
from app.services.decisions import record_decision

DEMO_SQL = Path("db/demo/demo.sql")
CASE_CUSTOMERS = "case_customers.txt"
NOTE = "[simulado] Decisión del estado del demo."
# The USD amount the dataset carries; a charge in USD comes without it.
_USD = "coalesce(t.amount_usd, CASE WHEN t.currency = 'USD' THEN t.amount END)"


class DemoError(RuntimeError):
    """The demo cannot be seeded with this data and configuration."""


@dataclass(frozen=True)
class Charge:
    """A charge a role uses: its id, and its merchant for a message that names it."""

    transaction_id: str
    merchant: str | None


@dataclass(frozen=True)
class Pick:
    """The customer chosen for one position of a role, with the charges it uses."""

    role: str
    position: int
    customer_id: str
    charges: dict[str, Charge]


def offline(deps: AgentDeps, settings: Settings) -> AgentDeps:
    """The agent's collaborators with the LLM off, as every scripted turn runs.

    Args:
        deps: Collaborators of the running service.
        settings: Its settings.

    Returns:
        The same collaborators with an LLM client that always takes the rules fallback.
    """
    off = settings.model_copy(update={"llm_enabled": False})
    return dataclasses.replace(deps, llm=LLMClient(off))


def read_excluded(eval_dir: Path, manifest_path: Path) -> set[str]:
    """Reads the customers of every evaluation case, checked against the versioned manifest.

    Args:
        eval_dir: DATA_DIR/eval.
        manifest_path: eval/splits/manifest.json.

    Returns:
        Their ids.

    Raises:
        DemoError: When the list is missing or is not the one the manifest names.
    """
    entry = json.loads(manifest_path.read_text("utf-8")).get("case_customers")
    path = eval_dir / CASE_CUSTOMERS
    if not entry or not path.exists():
        raise DemoError(f"{path} or its manifest entry is missing; run make cases OFFLINE=1")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise DemoError(f"{path} does not match the manifest hash")
    return set(data.decode("utf-8").split())


def install(owner: Session) -> None:
    """Creates the demo tables and the reset function, as the schema owner.

    Args:
        owner: Session of the schema owner.

    Raises:
        DemoError: When the owner does not bypass row level security, so the reset function
            would see no rows to empty.
    """
    bypass = owner.execute(
        text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).scalar_one()
    if not bypass:
        raise DemoError("the schema owner must bypass row level security to empty the demo")
    owner.connection().exec_driver_sql(DEMO_SQL.read_text(encoding="utf-8"))


def choose(
    owner: Session, db: Database, deps: AgentDeps, config: DemoConfig, excluded: set[str]
) -> list[Pick]:
    """Fills every role with the first customer that passes its rules and rehearsals.

    Args:
        owner: Session of the schema owner, which reads every customer.
        db: Database of trazo_app, where rehearsals run and roll back.
        deps: Agent collaborators with the LLM off.
        config: config/demo.yaml.
        excluded: Customers of the evaluation cases.

    Returns:
        One pick per persona and seeded role, and one per case of the cell.

    Raises:
        DemoError: When a role has no customer that passes.
    """
    cell = config.pt_cell
    roles = [
        (p.role, p.country, p.document.type, p.charges, p.rehearsals, 1) for p in config.personas
    ]
    roles += [(s.role, s.country, None, s.charges, [s.script], 1) for s in config.seeded]
    roles.append((cell.role, cell.country, None, cell.charges, [cell.script], cell.reviews))
    taken = set(excluded)
    picks: list[Pick] = []
    for role, country, document_type, rules, scripts, count in roles:
        for position in range(count):
            found = _first(
                owner, db, deps, config, role, country, document_type, rules, scripts, taken
            )
            picks.append(dataclasses.replace(found, position=position))
            taken.add(found.customer_id)
    return picks


def _first(
    owner: Session,
    db: Database,
    deps: AgentDeps,
    config: DemoConfig,
    role: str,
    country: str,
    document_type: str | None,
    rules: dict[str, ChargeRule],
    scripts: list[Script],
    taken: set[str],
) -> Pick:
    rehearsed = 0
    for customer_id in _eligible(owner, deps, config, country, document_type):
        if customer_id in taken:
            continue
        charges = {k: _charges(owner, deps, config, customer_id, r) for k, r in rules.items()}
        if any(not c for c in charges.values()):
            continue
        if rehearsed == config.max_candidates:
            break
        rehearsed += 1
        first = {k: c[0] for k, c in charges.items()}
        if all(_rehearse(db, deps, customer_id, first, s) for s in scripts):
            return Pick(role, 0, customer_id, first)
    raise DemoError(f"no customer passes the rules and rehearsals of {role} ({rehearsed} tried)")


def _eligible(
    owner: Session, deps: AgentDeps, config: DemoConfig, country: str, document_type: str | None
) -> list[str]:
    rows = owner.execute(
        text(
            "SELECT c.customer_id FROM customers c "
            "WHERE c.country_code = :country AND c.customer_status = 'Active' "
            "AND (CAST(:dtype AS text) IS NULL OR c.document_type = :dtype) "
            "AND NOT EXISTS (SELECT 1 FROM complaints k WHERE k.customer_id = c.customer_id "
            "AND k.creation_date > CAST(:now AS timestamp) - make_interval(days => :days)) "
            "ORDER BY md5(:seed || ':' || c.customer_id)"
        ),
        {
            "country": country,
            "dtype": document_type,
            "now": deps.clock.now,
            "days": config.complaint_lookback_days,
            "seed": config.order_seed,
        },
    ).scalars()
    return list(rows)


def _charges(
    owner: Session, deps: AgentDeps, config: DemoConfig, customer_id: str, rule: ChargeRule
) -> list[Charge]:
    low, high = rule.usd
    rows = owner.execute(
        text(
            "SELECT transaction_id, merchant_name FROM ("
            "  SELECT t.transaction_id, t.merchant_name, t.currency, "
            f"   {_USD} AS usd, p.product_type, p.product_status, "
            "    count(*) OVER (PARTITION BY t.product_id, t.merchant_name) AS same "
            "  FROM transactions t JOIN products p USING (product_id, customer_id) "
            "  WHERE t.customer_id = :customer AND t.transaction_type = :type "
            "  AND t.transaction_status IN ('Approved', 'Pending') "
            "  AND t.transaction_date > CAST(:now AS timestamp) - make_interval(days => :window) "
            "  AND t.transaction_date <= CAST(:now AS timestamp)"
            ") x WHERE usd > :low AND (CAST(:high AS numeric) IS NULL OR usd <= :high) "
            "AND (NOT :card OR (product_type IN ('credit_card', 'debit_card') "
            "     AND product_status = 'Active')) "
            "AND (CAST(:currency AS text) IS NULL OR currency = :currency) "
            "AND (CAST(:same AS integer) IS NULL OR (merchant_name IS NOT NULL AND same = :same)) "
            "ORDER BY md5(:seed || ':' || transaction_id)"
        ),
        {
            "customer": customer_id,
            "type": rule.type,
            "now": deps.clock.now,
            "window": deps.policy.config.dispute_window_days,
            "low": low,
            "high": high,
            "card": rule.card,
            "currency": rule.currency,
            "same": rule.same_merchant,
            "seed": config.order_seed,
        },
    ).all()
    return [Charge(r[0], r[1]) for r in rows]


def _rehearse(
    db: Database, deps: AgentDeps, customer_id: str, charges: dict[str, Charge], script: Script
) -> bool:
    with db.session(customer_id=customer_id, keep=False) as session:
        session.info["policy_version"] = deps.policy.version
        try:
            run_script(session, deps, customer_id, charges, script)
        except DemoError:
            return False
    return True


def run_script(
    session: Session,
    deps: AgentDeps,
    customer_id: str,
    charges: dict[str, Charge],
    script: Script,
) -> str:
    """Sends the turns of a script to the agent as the customer, on one case.

    Args:
        session: Session bound to the customer.
        deps: Agent collaborators with the LLM off.
        customer_id: The customer.
        charges: Charges of the role, by key.
        script: The turns and the status the case must end in.

    Returns:
        The case.

    Raises:
        DemoError: When a turn has nothing to press or the case ends in another status.
    """
    case_id: str | None = None
    options: list[dict[str, Any]] = []
    pending: str | None = None
    for turn in script.turns:
        if turn.button:
            r = handle_message(
                session,
                deps,
                customer_id,
                turn.say,
                transaction_id=charges[turn.button].transaction_id,
            )
        elif turn.option is not None:
            if turn.option >= len(options):
                raise DemoError(f"option {turn.option} was not shown")
            r = handle_message(
                session,
                deps,
                customer_id,
                turn.say,
                case_id=case_id,
                option=options[turn.option]["transaction_id"],
            )
        elif turn.recognition:
            r = handle_message(
                session,
                deps,
                customer_id,
                turn.say,
                case_id=case_id,
                recognition=turn.recognition,
            )
        elif turn.confirm:
            if pending is None:
                raise DemoError("nothing was offered to confirm")
            r = handle_message(
                session, deps, customer_id, turn.say, case_id=case_id, confirm_action_id=pending
            )
        else:
            r = handle_message(
                session, deps, customer_id, turn.say.format(**charges), case_id=case_id
            )
        case_id = r.case_id
        options = r.facts.get("options", [])
        offered = r.facts.get("pending_action")
        pending = offered["action_id"] if offered else None
    case = session.get(Case, case_id)
    if case is None or case.status != script.expect:
        raise DemoError(f"the script ended in {case.status if case else None}, not {script.expect}")
    return str(case_id)


def assign_documents(owner: Session, key: str, config: DemoConfig, picks: list[Pick]) -> None:
    """Writes the keyed hash of each invented document on its persona.

    The customer's own document stops working: only the hash is stored, never a number. A
    customer that held a demo document since an earlier seed and no longer has the role gets a
    retired hash, keyed on its id, that no document gives, so nobody can sign in as it.

    Args:
        owner: Session of the schema owner.
        key: DOCUMENT_HASH_KEY.
        config: config/demo.yaml.
        picks: The chosen customers.
    """
    chosen = {p.role: p.customer_id for p in picks}
    for persona in config.personas:
        digest = document_hash(key, persona.document.type, persona.document.number)
        holder = owner.execute(
            text("SELECT customer_id FROM customers WHERE document_hash = :h"), {"h": digest}
        ).scalar_one_or_none()
        if holder is not None and holder != chosen[persona.role]:
            owner.execute(
                text("UPDATE customers SET document_hash = :h WHERE customer_id = :c"),
                {"h": document_hash(key, "RETIRED", holder), "c": holder},
            )
        owner.execute(
            text("UPDATE customers SET document_hash = :h WHERE customer_id = :c"),
            {"h": digest, "c": chosen[persona.role]},
        )


def save_roles(owner: Session, picks: list[Pick]) -> None:
    """Replaces the chosen customers of every role in demo_roles.

    Args:
        owner: Session of the schema owner.
        picks: The chosen customers.
    """
    owner.execute(text("DELETE FROM demo_roles"))
    for p in picks:
        # A role keeps one charge: the one its seeded script presses, if any.
        charge = next(iter(p.charges.values()), None)
        owner.execute(
            text(
                "INSERT INTO demo_roles (role, position, customer_id, transaction_id) "
                "VALUES (:r, :p, :c, :t)"
            ),
            {
                "r": p.role,
                "p": p.position,
                "c": p.customer_id,
                "t": charge.transaction_id if charge else None,
            },
        )


def is_seeded(session: Session) -> bool:
    """Tells whether the database was prepared by `make seed-demo`.

    Args:
        session: Session with the analyst role.

    Returns:
        True when demo_roles exists and has rows.
    """
    found = session.execute(text("SELECT to_regclass('demo_roles') IS NOT NULL")).scalar_one()
    return bool(found) and bool(
        session.execute(text("SELECT EXISTS (SELECT 1 FROM demo_roles)")).scalar_one()
    )


def reset(
    db: Database, deps: AgentDeps, config: DemoConfig, analyst: str, key: str
) -> DemoResetOut:
    """Empties the demo and creates its starting cases again, in one transaction.

    Args:
        db: Database of trazo_app.
        deps: Agent collaborators with the LLM off.
        config: config/demo.yaml.
        analyst: Who asked for the reset.
        key: Idempotency key of the request.

    Returns:
        What the reset created. The same key returns the stored answer and resets nothing.

    Raises:
        AppError: 409 demo_not_seeded when `make seed-demo` never ran on this database; 409
            demo_script_diverged when a seeded case did not end as config/demo.yaml expects.
    """
    try:
        with db.session(role="analyst") as session:
            session.info["policy_version"] = deps.policy.version
            if not is_seeded(session):
                raise AppError("demo_not_seeded", "Run make seed-demo on this database.", 409)
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext('demo_reset'))"))
            stored = session.execute(
                text("SELECT response FROM demo_resets WHERE idempotency_key = :k"), {"k": key}
            ).scalar_one_or_none()
            if stored is not None:
                return DemoResetOut.model_validate(stored)
            session.execute(text("SELECT demo_reset()"))
            created = _seed_cases(session, deps, config, _load_roles(session))
            out = DemoResetOut(
                cases_created=len(created),
                reviews=config.pt_cell.reviews,
                reversals=config.pt_cell.reversals,
                demo_version=config.version,
            )
            write_audit(
                session,
                "human",
                "demo_reset",
                None,
                {"analyst": analyst, "demo": config.version},
                out.model_dump(),
                idempotency_key=f"demo_reset:{key}",
            )
            session.execute(
                text(
                    "INSERT INTO demo_resets (idempotency_key, analyst, response) "
                    "VALUES (:k, :a, CAST(:r AS jsonb))"
                ),
                {"k": key, "a": analyst, "r": out.model_dump_json()},
            )
            return out
    except DemoError as exc:
        raise AppError("demo_script_diverged", str(exc), 409) from exc
    except ProgrammingError as exc:
        # The function or a table is missing: this database never ran make seed-demo.
        if isinstance(exc.orig, UndefinedFunction | UndefinedTable):
            raise AppError("demo_not_seeded", "Run make seed-demo on this database.", 409) from exc
        raise


def _load_roles(session: Session) -> dict[str, list[tuple[str, str | None]]]:
    rows = session.execute(
        text("SELECT role, customer_id, transaction_id FROM demo_roles ORDER BY role, position")
    ).all()
    out: dict[str, list[tuple[str, str | None]]] = {}
    for role, customer_id, transaction_id in rows:
        out.setdefault(role, []).append((customer_id, transaction_id))
    return out


def _seed_cases(
    session: Session,
    deps: AgentDeps,
    config: DemoConfig,
    roles: dict[str, list[tuple[str, str | None]]],
) -> list[str]:
    created: list[tuple[str, str]] = []
    cell = config.pt_cell
    cell_rows = roles.get(cell.role, [])
    if len(cell_rows) != cell.reviews:
        raise DemoError(f"demo_roles has {len(cell_rows)} cases of {cell.role}, not {cell.reviews}")
    # The cell first, so the open queue the analyst sees ends with the seeded escalations.
    cell_cases = [_seeded_case(session, deps, cell, row) for row in cell_rows]
    created += cell_cases
    bind_context(session, role="analyst")
    for (case_id, _), decision in zip(cell_cases, cell.decisions, strict=True):
        body = (
            HumanDecisionIn(decision="reject", reason=decision.reject, note=NOTE)
            if isinstance(decision, Reject)
            else HumanDecisionIn(decision="approve", note=NOTE)
        )
        record_decision(session, deps, config.seed_analyst, case_id, body)
    for s in config.seeded:
        (row,) = roles.get(s.role) or [None]
        if row is None:
            raise DemoError(f"demo_roles has no customer for {s.role}")
        created.append(_seeded_case(session, deps, s, row))
    bind_context(session, role="analyst")
    for case_id, customer_id in created:
        session.execute(text("UPDATE cases SET simulated = true WHERE id = :id"), {"id": case_id})
        write_audit(
            session,
            "system",
            "mark_simulated",
            case_id,
            None,
            {"simulated": True, "demo": config.version},
            customer_id=customer_id,
        )
    return [case_id for case_id, _ in created]


def _seeded_case(
    session: Session, deps: AgentDeps, seeded: Seeded, row: tuple[str, str | None]
) -> tuple[str, str]:
    customer_id, transaction_id = row
    charges = {k: Charge(str(transaction_id), None) for k in seeded.charges}
    bind_context(session, customer_id=customer_id)
    return run_script(session, deps, customer_id, charges, seeded.script), customer_id

"""Row level security (TRZ-07 CA5, CA7): the service's role sees only the rows of the customer
fixed for the transaction, the context never outlives its transaction, and the role has no way
around the policies.

Every check that expects no rows has a control that runs the same query as the owner and finds
rows, so a test cannot pass because the data is missing.
"""

from collections.abc import Iterator
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.adapters.db.session import Database, SchemaUrls
from tests.serving_data import card, customer, load, transaction

pytestmark = pytest.mark.integration

AT = datetime(2026, 6, 10, 12, 0)
A, B = "CUS-A", "CUS-B"


def _seed_operational_rows(admin_url: str) -> None:
    """One row per customer in every per-customer table that fixtures do not fill."""
    engine = create_engine(admin_url)
    with engine.begin() as conn:
        for c in (A, B):
            conn.execute(
                text(
                    "INSERT INTO complaints (complaint_id, customer_id, creation_date, "
                    "process_date, case_type, category, reception_channel, has_affected_product,"
                    " priority, status, sla_breached, is_repeat_complainer) VALUES "
                    "(:c || '-Q', :c, :at, :d, 'Claim', 'Transactions', 'App', false, 'Low', "
                    "'Open', false, false)"
                ),
                {"c": c, "at": AT, "d": AT.date()},
            )
            conn.execute(
                text(
                    "INSERT INTO cases (id, customer_id, intent, trace_id) "
                    "VALUES (:c || '-K', :c, 'out_of_scope', 't')"
                ),
                {"c": c},
            )
            params = {"c": c, "k": f"{c}-K", "p": f"{c}-P", "t": f"{c}-T"}
            conn.execute(
                text(
                    "INSERT INTO case_queue (case_id, customer_id, kind) "
                    "VALUES (:k, :c, 'escalation')"
                ),
                params,
            )
            conn.execute(
                text(
                    "INSERT INTO disputes (customer_id, case_id, transaction_id, dispute_type) "
                    "VALUES (:c, :k, :t, 'unrecognized_charge')"
                ),
                params,
            )
            conn.execute(
                text(
                    "INSERT INTO card_blocks (customer_id, product_id, case_id, reason, "
                    "status_before) VALUES (:c, :p, :k, 'unrecognized_charge', 'Active')"
                ),
                params,
            )
            conn.execute(
                text(
                    "INSERT INTO case_actions (id, case_id, customer_id, action, transaction_id) "
                    "VALUES (:c || '-A', :k, :c, 'register', :t)"
                ),
                params,
            )
            conn.execute(
                text(
                    "INSERT INTO audit_log (trace_id, case_id, customer_id, actor, action) "
                    "VALUES ('t', :k, :c, 'tool', 'test')"
                ),
                params,
            )
    engine.dispose()


@pytest.fixture
def two_customers(schema: SchemaUrls) -> Iterator[SchemaUrls]:
    load(
        schema.admin,
        [customer(A), customer(B)],
        [card(f"{A}-P", A), card(f"{B}-P", B)],
        [transaction(f"{A}-T", A, f"{A}-P", AT), transaction(f"{B}-T", B, f"{B}-P", AT)],
    )
    _seed_operational_rows(schema.admin)
    yield schema


def _customer_tables(admin_url: str) -> list[str]:
    """Every base table of the schema with a customer_id column."""
    engine = create_engine(admin_url)
    with engine.connect() as conn:
        tables = conn.execute(
            text(
                "SELECT c.table_name FROM information_schema.columns c "
                "JOIN information_schema.tables t USING (table_schema, table_name) "
                "WHERE c.table_schema = current_schema() AND c.column_name = 'customer_id' "
                "AND t.table_type = 'BASE TABLE' ORDER BY 1"
            )
        ).scalars()
        out = list(tables)
    engine.dispose()
    return out


def _count(url: str, table: str, where: str = "true", **context: str) -> int:
    db = Database(url)
    try:
        with db.session(**context) as s:
            return s.execute(text(f"SELECT count(*) FROM {table} WHERE {where}")).scalar_one()
    finally:
        db.dispose()


def test_every_customer_table_is_covered(two_customers: SchemaUrls) -> None:
    assert _customer_tables(two_customers.admin) == [
        "audit_log",
        "card_blocks",
        "case_actions",
        "case_queue",
        "cases",
        "complaints",
        "customers",
        "disputes",
        "products",
        "transactions",
    ]


def test_customer_context_hides_every_row_of_another_customer(two_customers: SchemaUrls) -> None:
    for table in [*_customer_tables(two_customers.admin), "case_history"]:
        other = f"customer_id = '{B}'"
        assert _count(two_customers.admin, table, other) > 0, table  # control: B has rows
        assert _count(two_customers.app, table, other, customer_id=A) == 0, table
        own = _count(two_customers.app, table, customer_id=A)
        assert own == _count(two_customers.admin, table, f"customer_id = '{A}'") > 0, table


def test_no_context_sees_no_customer_row(two_customers: SchemaUrls) -> None:
    for table in _customer_tables(two_customers.admin):
        assert _count(two_customers.app, table) == 0, table


def test_analyst_context_sees_every_customer(two_customers: SchemaUrls) -> None:
    for table in _customer_tables(two_customers.admin):
        assert _count(two_customers.app, table, role="analyst") == _count(
            two_customers.admin, table
        ), table


def test_customer_context_cannot_write_rows_of_another_customer(
    two_customers: SchemaUrls,
) -> None:
    db = Database(two_customers.app)
    with pytest.raises(DBAPIError, match="row-level security"):
        with db.session(customer_id=A) as s:
            s.execute(
                text(
                    "INSERT INTO audit_log (trace_id, customer_id, actor, action) "
                    "VALUES ('t', :b, 'tool', 'forged')"
                ),
                {"b": B},
            )
    with db.session(customer_id=A) as s:
        changed = s.execute(
            text("UPDATE products SET product_status = 'Blocked' WHERE customer_id = :b"),
            {"b": B},
        ).rowcount
    db.dispose()
    assert changed == 0
    assert _count(two_customers.admin, "products", "product_status = 'Blocked'") == 0


def test_app_role_cannot_get_around_the_policies(two_customers: SchemaUrls) -> None:
    engine = create_engine(two_customers.app)
    with engine.connect() as conn:
        superuser, bypass = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
        owned = conn.execute(
            text("SELECT count(*) FROM pg_tables WHERE tableowner = current_user")
        ).scalar_one()
        flags = conn.execute(
            text(
                "SELECT bool_and(relrowsecurity), bool_and(relforcerowsecurity) FROM pg_class "
                "WHERE oid = ANY(CAST(:t AS regclass[]))"
            ),
            {"t": _customer_tables(two_customers.admin)},
        ).one()
    assert (superuser, bypass, owned) == (False, False, 0)
    assert flags == (True, True)

    with pytest.raises(DBAPIError, match="must be owner"):
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE customers DISABLE ROW LEVEL SECURITY"))
    # Without BYPASSRLS, turning row security off makes a protected query fail instead.
    with pytest.raises(DBAPIError, match="row-level security"):
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL row_security = off"))
            conn.execute(text("SELECT count(*) FROM customers"))
    engine.dispose()


def test_context_lasts_one_transaction_on_a_reused_connection(two_customers: SchemaUrls) -> None:
    # One pooled connection, so every session below reuses the same server backend.
    db = Database(two_customers.app, pool_size=1, max_overflow=0)
    seen = []
    for context in ({"customer_id": A}, {}, {"customer_id": B}):
        with db.session(**context) as s:
            pid = s.execute(text("SELECT pg_backend_pid()")).scalar_one()
            owners = s.execute(text("SELECT customer_id FROM transactions")).scalars().all()
            seen.append((pid, sorted(owners)))
    db.dispose()
    assert len({pid for pid, _ in seen}) == 1
    assert [owners for _, owners in seen] == [[A], [], [B]]


def test_context_is_fixed_again_in_every_transaction_of_a_session(
    two_customers: SchemaUrls,
) -> None:
    db = Database(two_customers.app)
    with db.session(customer_id=A) as s:
        first = s.execute(text("SELECT customer_id FROM customers")).scalars().all()
        s.commit()
        after_commit = s.execute(text("SELECT customer_id FROM customers")).scalars().all()
    db.dispose()
    assert first == after_commit == [A]


def test_a_session_level_set_would_leak_to_the_next_transaction(
    two_customers: SchemaUrls,
) -> None:
    # Negative control for CA7: the same value set with is_local = false survives the commit
    # on the pooled connection, which is why the service always passes true.
    db = Database(two_customers.app, pool_size=1, max_overflow=0)
    with db.session() as s:
        s.execute(text("SELECT set_config('app.customer_id', :a, false)"), {"a": A})
    with db.engine.connect() as conn:
        leaked = conn.execute(text("SELECT current_setting('app.customer_id', true)")).scalar()
    db.dispose()
    assert leaked == A


def test_audit_log_rows_cannot_change_or_disappear(two_customers: SchemaUrls) -> None:
    owner = create_engine(two_customers.admin)
    for stmt in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log"):
        with pytest.raises(DBAPIError, match="append-only"):
            with owner.begin() as conn:
                conn.execute(text(stmt))
    owner.dispose()
    app = create_engine(two_customers.app)
    with pytest.raises(DBAPIError, match="permission denied"):
        with app.begin() as conn:
            conn.execute(text("SELECT set_config('app.role', 'analyst', true)"))
            conn.execute(text("DELETE FROM audit_log"))
    app.dispose()


# ---- the lookup by document before login (TRZ-09) -----------------------------------------


@pytest.fixture
def one_customer(schema: SchemaUrls) -> SchemaUrls:
    load(schema.admin, [customer(A)], [], [])
    return schema


def _document_hash(schema: SchemaUrls) -> str:
    engine = create_engine(schema.admin)
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT document_hash FROM customers WHERE customer_id = :c"), {"c": A}
        ).scalar_one()
    engine.dispose()
    return str(value)


def test_without_context_the_app_sees_no_customer_but_the_lookup_returns_only_the_id(
    one_customer: SchemaUrls,
) -> None:
    key = _document_hash(one_customer)
    engine = create_engine(one_customer.app)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM customers")).scalar_one() == 0
        found = conn.execute(text("SELECT auth_customer_id(:k)"), {"k": key}).one()
        missing = conn.execute(text("SELECT auth_customer_id('nope')")).scalar_one()
    engine.dispose()
    assert tuple(found) == (A,)
    assert missing is None


def test_the_lookup_function_pins_its_search_path_and_only_the_app_may_run_it(
    one_customer: SchemaUrls,
) -> None:
    engine = create_engine(one_customer.admin)
    with engine.connect() as conn:
        schema_name = conn.execute(text("SELECT current_schema()")).scalar_one()
        config, definer, owner = conn.execute(
            text(
                "SELECT p.proconfig, p.prosecdef, pg_get_userbyid(p.proowner) FROM pg_proc p "
                "WHERE p.proname = 'auth_customer_id' "
                "AND p.pronamespace = current_schema()::regnamespace"
            )
        ).one()
        runners = set(
            conn.execute(
                text(
                    "SELECT grantee FROM information_schema.routine_privileges "
                    "WHERE routine_name = 'auth_customer_id' AND routine_schema = :s "
                    "AND privilege_type = 'EXECUTE'"
                ),
                {"s": schema_name},
            ).scalars()
        )
    engine.dispose()
    assert config == [f"search_path={schema_name}, pg_temp"]
    assert definer is True
    # The owner reads two columns of customers and cannot log in; PUBLIC may not run it.
    assert owner == "trazo_auth"
    assert runners == {"trazo_app", "trazo_auth"}

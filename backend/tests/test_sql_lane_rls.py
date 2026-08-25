"""Prove the load-bearing isolation layer on real PostgreSQL.

The gate tests cover the AST boundary; these tests cover what holds when the
gate is imagined away: the ``fyn_analyst`` role plus row-level security. They
replay the migration chain onto a throwaway database, seed two tenants
through the ORM, and then attack the database directly with raw SQL under
the analyst role.
"""
from __future__ import annotations

from datetime import date
import uuid

import pytest
import sqlalchemy as sa
from alembic.command import upgrade
from alembic.config import Config
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.event_time import from_local_parts
from app.models import Transaction, User
from app.seed import seed_system_taxonomy
from app.services.sql_gate import (
    ANALYST_ROLE,
    GOVERNED_TABLES,
    TENANT_GUC,
    SqlCompilationError,
    execute_governed_sql,
    gate_sql,
)

BACKEND_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


@pytest.fixture()
def rls_database():
    """A migrated throwaway PostgreSQL database, dropped afterwards."""
    base = sa.make_url(get_settings().database_url)
    name = f"rls_check_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    except sa.exc.OperationalError as error:
        admin.dispose()
        pytest.skip(f"PostgreSQL is not reachable for RLS tests: {error}")
    url = base.set(database=name).render_as_string(hide_password=False)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.attributes["sqlalchemy.url"] = url
    upgrade(config, "head")
    engine = sa.create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ), {"name": name})
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@pytest.fixture()
def two_tenants(rls_database):
    Session = sessionmaker(bind=rls_database, expire_on_commit=False)
    with Session() as session:
        seed_system_taxonomy(session)
        alice = User(email="alice@example.com", display_name="Alice")
        bob = User(email="bob@example.com", display_name="Bob")
        session.add_all([alice, bob])
        session.flush()
        for owner, merchant, amount in ((alice, "Blue Tokai", 40_000), (bob, "Third Wave", 90_000)):
            session.add(Transaction(
                user_id=owner.id,
                transaction_type="expense",
                amount_minor=amount,
                currency="INR",
                merchant_name=merchant,
                transaction_at=from_local_parts(date(2026, 8, 5), None, "Asia/Kolkata"),
            ))
        session.commit()
        yield session, alice, bob


def _as_analyst(connection, user_id=None):
    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
    if user_id is not None:
        connection.execute(
            sa.text("SELECT set_config(:guc, :uid, true)"),
            {"guc": TENANT_GUC, "uid": str(user_id)},
        )
    connection.exec_driver_sql(f"SET LOCAL ROLE {ANALYST_ROLE}")


def test_rls_returns_only_the_named_tenants_rows(two_tenants):
    session, alice, bob = two_tenants
    result = execute_governed_sql(session, alice.id, "SELECT merchant_name, amount_minor FROM transactions")
    assert result["rows"] == [["Blue Tokai", 40_000]]
    assert result["result_schema"] == [
        {"name": "merchant_name", "type": "character varying"},
        {"name": "amount_minor", "type": "bigint"},
    ]
    assert result["semantic_compile_ms"] >= 0
    result = execute_governed_sql(session, bob.id, "SELECT merchant_name FROM transactions")
    assert result["rows"] == [["Third Wave"]]


def test_postgres_is_the_authoritative_set_operation_compiler(two_tenants):
    session, alice, _bob = two_tenants
    different_widths = (
        "SELECT merchant_name FROM transactions "
        "UNION ALL SELECT merchant_name, amount_minor FROM transactions"
    )
    incompatible_types = (
        "SELECT merchant_name AS value FROM transactions "
        "UNION ALL SELECT amount_minor AS value FROM transactions"
    )

    # The application gate deliberately owns safety, not PostgreSQL semantics.
    assert gate_sql(different_widths).tables == frozenset({"transactions"})
    assert gate_sql(incompatible_types).tables == frozenset({"transactions"})

    with pytest.raises(SqlCompilationError, match="same number of columns"):
        execute_governed_sql(session, alice.id, different_widths)
    with pytest.raises(SqlCompilationError, match="cannot be matched"):
        execute_governed_sql(session, alice.id, incompatible_types)


def test_an_unset_tenant_guc_yields_zero_rows_not_everything(two_tenants):
    session, _alice, _bob = two_tenants
    with session.get_bind().connect() as connection:
        with connection.begin() as transaction:
            _as_analyst(connection)
            rows = connection.execute(sa.text("SELECT id FROM transactions")).fetchall()
            transaction.rollback()
    assert rows == []


def test_the_analyst_role_cannot_write_even_with_handwritten_sql(two_tenants):
    session, alice, _bob = two_tenants
    with session.get_bind().connect() as connection:
        with connection.begin() as transaction:
            connection.execute(
                sa.text("SELECT set_config(:guc, :uid, true)"),
                {"guc": TENANT_GUC, "uid": str(alice.id)},
            )
            connection.exec_driver_sql(f"SET LOCAL ROLE {ANALYST_ROLE}")
            with pytest.raises(sa.exc.ProgrammingError):
                connection.execute(sa.text("DELETE FROM transactions"))
            transaction.rollback()


def test_the_analyst_role_cannot_read_ungoverned_tables(two_tenants):
    session, alice, _bob = two_tenants
    with session.get_bind().connect() as connection:
        with connection.begin() as transaction:
            _as_analyst(connection, alice.id)
            with pytest.raises(sa.exc.ProgrammingError):
                connection.execute(sa.text("SELECT email FROM users"))
            transaction.rollback()


def test_system_taxonomy_is_shared_while_user_rows_are_not(two_tenants):
    session, alice, _bob = two_tenants
    result = execute_governed_sql(session, alice.id, "SELECT slug FROM categories ORDER BY slug")
    assert ["food"] in result["rows"]


def test_every_governed_table_has_rls_enabled_and_an_analyst_policy(two_tenants):
    session, _alice, _bob = two_tenants
    with session.get_bind().connect() as connection:
        secured = {
            row[0]
            for row in connection.execute(sa.text(
                "SELECT tablename FROM pg_tables WHERE rowsecurity AND schemaname = 'public'"
            ))
        }
        policied = {
            row[0]
            for row in connection.execute(sa.text(
                "SELECT tablename FROM pg_policies WHERE policyname = 'analyst_tenant_isolation'"
            ))
        }
    assert GOVERNED_TABLES <= secured
    assert GOVERNED_TABLES <= policied

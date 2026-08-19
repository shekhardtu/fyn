from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select

from app.event_time import from_local_parts
from app.models import Transaction, User
from app.seed import default_user
from app.services import sql_gate
from app.services.semantic_registry import semantic_schema_registry
from app.services.sql_gate import (
    GOVERNED_TABLES,
    SqlGateError,
    execute_governed_sql,
    gate_sql,
)


def rejected(sql: str) -> str:
    with pytest.raises(SqlGateError) as excinfo:
        gate_sql(sql)
    return excinfo.value.code


# --- statement shape ---------------------------------------------------------

@pytest.mark.parametrize("sql,code", [
    ("INSERT INTO transactions (id) VALUES ('x')", "not_select"),
    ("UPDATE transactions SET amount_minor = 0", "not_select"),
    ("DELETE FROM transactions", "not_select"),
    ("DROP TABLE transactions", "not_select"),
    ("CREATE TABLE stolen AS SELECT * FROM transactions", "not_select"),
    ("TRUNCATE transactions", "not_select"),
    ("GRANT SELECT ON transactions TO PUBLIC", "not_select"),
    ("VACUUM", "not_select"),
    ("SET ROLE postgres", "not_select"),
    ("EXPLAIN SELECT 1", "not_select"),
    ("SELECT amount_minor FROM transactions; SELECT 1", "multiple_statements"),
    ("SELECT id INTO stolen FROM transactions", "forbidden_construct"),
    ("SELECT id FROM transactions FOR UPDATE", "forbidden_construct"),
    ("WITH x AS (DELETE FROM transactions RETURNING id) SELECT id FROM x", "forbidden_construct"),
    ("SELECT not valid sql at all FROM FROM", "parse_error"),
])
def test_non_read_statements_are_rejected(sql, code):
    assert rejected(sql) == code


# --- manifest boundary -------------------------------------------------------

@pytest.mark.parametrize("sql,code", [
    ("SELECT email FROM users", "unknown_table"),
    ("SELECT token_hash FROM user_sessions", "unknown_table"),
    ("SELECT * FROM pg_catalog.pg_tables", "forbidden_schema"),
    ("SELECT * FROM information_schema.tables", "forbidden_schema"),
    ("SELECT 1", "unknown_table"),
    ("SELECT nonexistent_column FROM transactions", "unknown_column"),
])
def test_the_manifest_is_the_table_and_column_boundary(sql, code):
    assert rejected(sql) == code


def test_a_cte_alias_may_shadow_a_forbidden_name_without_reading_it():
    gated = gate_sql(
        "WITH users AS (SELECT merchant_name FROM transactions) SELECT merchant_name FROM users"
    )
    assert gated.tables == frozenset({"transactions"})


# --- full tenant-owned row access -------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT description FROM transactions",
    "SELECT t.description FROM transactions t",
    "SELECT location_label FROM transactions",
    "SELECT * FROM transactions",
    "SELECT amount_minor FROM transactions WHERE description LIKE '%salary%'",
    "SELECT amount_minor FROM transactions ORDER BY description",
])
def test_every_column_on_a_tenant_governed_table_is_queryable(sql):
    assert gate_sql(sql).tables == frozenset({"transactions"})


# --- functions ---------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT set_config('app.current_user_id', 'other-user', true) FROM accounts",
    "SELECT pg_sleep(60) FROM accounts",
    "SELECT pg_read_file('/etc/passwd') FROM accounts",
])
def test_dangerous_functions_are_rejected(sql):
    assert rejected(sql) == "forbidden_function"


# --- limits ------------------------------------------------------------------

def test_the_governed_row_cap_is_always_enforced():
    cap = semantic_schema_registry().policy.max_result_rows
    assert f"LIMIT {cap}" in gate_sql("SELECT name FROM accounts").sql
    assert f"LIMIT {cap}" in gate_sql("SELECT name FROM accounts LIMIT 100000").sql
    assert "LIMIT 10" in gate_sql("SELECT name FROM accounts LIMIT 10").sql


def test_joins_unions_and_aggregates_inside_the_manifest_pass():
    gated = gate_sql(
        "SELECT c.name, SUM(t.amount_minor) AS total FROM transactions t "
        "JOIN categories c ON c.id = t.category_id "
        "WHERE t.transaction_type = 'expense' GROUP BY c.name ORDER BY total DESC"
    )
    assert gated.tables == frozenset({"transactions", "categories"})


def test_nested_selects_ctes_set_operations_and_windows_pass():
    gated = gate_sql(
        "WITH monthly AS ("
        " SELECT category_id, date_trunc('month', transaction_at) AS month,"
        " SUM(amount_minor) AS spend_minor FROM transactions"
        " WHERE deleted_at IS NULL GROUP BY category_id, month"
        "), scored AS ("
        " SELECT category_id, month, spend_minor,"
        " AVG(spend_minor) OVER (PARTITION BY category_id ORDER BY month"
        " ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING) AS baseline_minor"
        " FROM monthly"
        ") SELECT category_id, spend_minor, baseline_minor FROM scored"
        " UNION ALL SELECT category_id, 0, 0 FROM transactions WHERE 1 = 0"
    )
    assert gated.tables == frozenset({"transactions"})


# --- one isolation rule per governed surface --------------------------------

def test_every_registry_entity_has_a_tenant_rule():
    assert {entity.table for entity in semantic_schema_registry().entities} == GOVERNED_TABLES


def test_the_gate_and_the_baseline_rls_govern_the_same_tables():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0001_baseline.py"
    spec = importlib.util.spec_from_file_location("migration_baseline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module.BASE_GOVERNED_TABLES) == GOVERNED_TABLES
    assert set(module.USER_TABLES) == sql_gate.USER_TENANT_TABLES
    assert set(module.SCOPED_TABLES) == sql_gate.SCOPED_TENANT_TABLES
    assert set(module.CHILD_TABLES) == sql_gate.TRANSACTION_CHILD_TABLES


# --- execution with emulated RLS (SQLite) ------------------------------------

def expense(user_id, merchant: str, amount: int) -> Transaction:
    return Transaction(
        user_id=user_id,
        transaction_type="expense",
        amount_minor=amount,
        currency="INR",
        merchant_name=merchant,
        transaction_at=from_local_parts(date(2026, 8, 5), None, "Asia/Kolkata"),
    )


def test_execution_is_tenant_isolated_even_without_postgres(db):
    user = default_user(db)
    stranger = User(email="stranger@example.com", display_name="Stranger")
    db.add(stranger)
    db.flush()
    db.add(expense(user.id, "Blue Tokai", 40_000))
    db.add(expense(stranger.id, "Third Wave", 90_000))
    db.commit()

    result = execute_governed_sql(
        db, user.id, "SELECT merchant_name, amount_minor FROM transactions"
    )

    assert result["rows"] == [["Blue Tokai", 40_000]]
    assert result["tables"] == ["transactions"]

    other = execute_governed_sql(
        db, stranger.id, "SELECT merchant_name FROM transactions ORDER BY merchant_name"
    )
    assert other["rows"] == [["Third Wave"]]


def test_system_taxonomy_stays_visible_through_emulated_rls(db):
    user = default_user(db)
    result = execute_governed_sql(db, user.id, "SELECT slug FROM categories ORDER BY slug")
    assert ["food"] in result["rows"]


def test_execution_refuses_what_the_gate_refuses(db):
    user = default_user(db)
    with pytest.raises(SqlGateError):
        execute_governed_sql(db, user.id, "SELECT email FROM users")

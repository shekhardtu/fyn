"""The deterministic gate between model-authored SQL and the database.

Phase 2 of the analyst platform blueprint. The model gains full SQL
authorship; this module is the output boundary that makes that safe:

* parse to an AST with sqlglot — never regex — and reject anything that is
  not exactly one SELECT-shaped statement;
* resolve every table and column against the tenant-governed native source
  manifest and refuse schema escapes whose tables have no tenant policy;
* deny dangerous functions and enforce the governed row limit;
* execute under a read-only transaction, a statement timeout, and the
  ``fyn_analyst`` role, with the tenant id in the ``app.current_user_id``
  GUC that the row-level-security policies read.

The gate is defense in depth, not the tenant barrier: RLS at the database is
the load-bearing isolation (the migration baseline). On SQLite — a disposable
local/test convenience — the same policies are emulated by rewriting every
governed table into a tenant-filtered derived table, so default-deny holds
on every engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.qualify import qualify
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .manifest import scan_native_schema
from .semantic_registry import semantic_schema_registry

ANALYST_ROLE = "fyn_analyst"
TENANT_GUC = "app.current_user_id"
STATEMENT_TIMEOUT_MS = 5000
AUTHORING_DIALECT = "postgres"

# The tenant rule for every governed table. The migration baseline encodes the same
# grouping as PostgreSQL RLS policies; a test pins the two lists together and
# a second test pins them to the registry, so a new governed entity cannot
# ship without an isolation rule.
USER_TENANT_TABLES = frozenset({
    "transactions", "accounts", "account_balance_snapshots",
    "investment_holdings", "investment_valuation_snapshots", "tags",
    "financial_observations", "budgets", "goals", "goal_contributions",
    "loans", "recurring_transactions", "subscriptions",
})
# Not in the gate's schema (they are not registry entities), but migrations
# The baseline grants the analyst role SELECT on them — so the emulated-RLS
# rewrite must know their tenant rule too, or a future schema addition would
# pass them through unfiltered on SQLite while Postgres held.
EXTRA_USER_TENANT_TABLES = frozenset({
    "source_records", "source_annotations", "entity_links", "user_traits",
})
SCOPED_TENANT_TABLES = frozenset({"categories", "subcategories", "merchants"})
TRANSACTION_CHILD_TABLES = frozenset({"transaction_sources"})
GOVERNED_TABLES = USER_TENANT_TABLES | SCOPED_TENANT_TABLES | TRANSACTION_CHILD_TABLES

# Read paths that leak beyond the manifest, write paths disguised as reads,
# and pure denial-of-service surfaces. The read-only transaction and the
# analyst role bound most of these anyway; rejecting them at the gate keeps
# the failure loud and attributable.
FORBIDDEN_FUNCTIONS = frozenset({
    "set_config", "pg_sleep", "pg_read_file", "pg_read_binary_file",
    "pg_ls_dir", "pg_stat_file", "dblink", "dblink_exec", "pg_notify",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "pg_advisory_lock", "pg_advisory_xact_lock", "nextval", "setval",
    "lastval", "lo_import", "lo_export", "current_query",
})

FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
    exp.Alter, exp.TruncateTable, exp.Grant, exp.Command, exp.Transaction,
    exp.Commit, exp.Rollback, exp.Lock,
)


class SqlGateError(ValueError):
    """A rejected statement. ``code`` is a stable machine label."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class SqlCompilationError(ValueError):
    """A safe statement rejected by PostgreSQL's semantic compiler."""

    code = "query_compilation_error"

    def __init__(self, message: str, *, prepared_name: str | None = None) -> None:
        super().__init__(message)
        self.prepared_name = prepared_name


@dataclass(frozen=True)
class GatedSql:
    """One approved statement, normalized for the execution dialect."""

    sql: str
    tables: frozenset[str]
    limit: int


@lru_cache(maxsize=1)
def _manifest_schema() -> dict[str, object]:
    """Physical table -> column -> type, from the native manifest scan."""
    return {
        entity["table"]: {column["name"]: column["type"] for column in entity["columns"]}
        for entity in scan_native_schema().values()
    }


def _reject_forbidden_nodes(expression: exp.Expression) -> None:
    for node in expression.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SqlGateError(
                f"Statement contains a forbidden construct: {type(node).__name__}",
                code="forbidden_construct",
            )
        if isinstance(node, exp.Select) and node.args.get("into") is not None:
            raise SqlGateError("SELECT INTO is not a read", code="forbidden_construct")
        if isinstance(node, exp.Func):
            names = {node.name.lower(), node.sql_name().lower()}
            forbidden = names & FORBIDDEN_FUNCTIONS
            if forbidden:
                raise SqlGateError(
                    f"Function {sorted(forbidden)[0]} is not permitted",
                    code="forbidden_function",
                )


def _reject_unknown_tables(expression: exp.Expression, schema: dict[str, object]) -> frozenset[str]:
    cte_names = {cte.alias_or_name for cte in expression.find_all(exp.CTE)}
    referenced: set[str] = set()
    for table in expression.find_all(exp.Table):
        if table.catalog or table.db:
            raise SqlGateError(
                f"Schema-qualified reference {table.sql()} is outside the governed manifest",
                code="forbidden_schema",
            )
        if table.name in cte_names:
            continue
        if table.name not in schema:
            raise SqlGateError(
                f"Table {table.name} is not in the governed manifest",
                code="unknown_table",
            )
        referenced.add(table.name)
    if not referenced:
        raise SqlGateError("Statement reads no governed table", code="unknown_table")
    return frozenset(referenced)


def gate_sql(sql: str, *, execution_dialect: str = AUTHORING_DIALECT) -> GatedSql:
    """Validate one arbitrary read-only query graph and normalize it.

    The graph may contain any number of nested SELECTs, CTEs, set operations,
    joins and window expressions. Its physical-table leaves must all belong to
    the tenant-governed manifest.
    """
    try:
        statements = sqlglot.parse(sql, read=AUTHORING_DIALECT)
    except ParseError as error:
        raise SqlGateError(f"SQL failed to parse: {error}", code="parse_error") from error
    statements = [statement for statement in statements if statement is not None]
    if len(statements) != 1:
        raise SqlGateError(
            f"Exactly one statement is allowed, got {len(statements)}",
            code="multiple_statements",
        )
    expression = statements[0]
    if not isinstance(expression, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise SqlGateError(
            f"Only SELECT statements are allowed, got {type(expression).__name__}",
            code="not_select",
        )
    _reject_forbidden_nodes(expression)

    schema = _manifest_schema()
    tables = _reject_unknown_tables(expression, schema)
    try:
        expression = qualify(expression, schema=schema)
    except Exception as error:
        raise SqlGateError(f"Column resolution failed: {error}", code="unknown_column") from error

    cap = semantic_schema_registry().policy.max_result_rows
    existing = expression.args.get("limit")
    limit = cap
    if isinstance(existing, exp.Limit) and isinstance(existing.expression, exp.Literal):
        try:
            limit = min(int(existing.expression.name), cap)
        except ValueError:
            limit = cap
    expression = expression.limit(limit, copy=True)

    return GatedSql(
        sql=expression.sql(dialect=execution_dialect),
        tables=tables,
        limit=limit,
    )


def _tenant_predicate(table: str, tenant_literal: str) -> str | None:
    if table in USER_TENANT_TABLES or table in EXTRA_USER_TENANT_TABLES:
        return f"user_id = {tenant_literal}"
    if table in SCOPED_TENANT_TABLES:
        return f"scope = 'system' OR owner_user_id = {tenant_literal}"
    if table in TRANSACTION_CHILD_TABLES:
        return (
            "EXISTS (SELECT 1 FROM transactions parent "
            f"WHERE parent.id = {table}.transaction_id AND parent.user_id = {tenant_literal})"
        )
    return None


def apply_emulated_rls(sql: str, user_id: UUID, *, dialect: str) -> str:
    """Rewrite governed table references into tenant-filtered derived tables.

    The software analogue of the baseline policies for engines without
    row-level security (SQLite). The tenant literal is rendered from a UUID
    object, so it cannot carry SQL; SQLite stores ``Uuid`` columns as 32-char
    hex, which is why the literal differs by dialect.
    """
    tenant_literal = f"'{user_id.hex if dialect == 'sqlite' else str(user_id)}'"
    expression = sqlglot.parse_one(sql, read=dialect)
    cte_names = {cte.alias_or_name for cte in expression.find_all(exp.CTE)}

    def rewrite(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.Table) or node.name in cte_names:
            return node
        predicate = _tenant_predicate(node.name, tenant_literal)
        if predicate is None:
            return node
        inner = sqlglot.parse_one(f'SELECT * FROM "{node.name}" WHERE {predicate}', read=dialect)
        return exp.Subquery(
            this=inner,
            alias=exp.TableAlias(this=exp.to_identifier(node.alias_or_name)),
        )

    return expression.transform(rewrite).sql(dialect=dialect)


def _database_error_detail(error: Exception) -> str:
    """Return PostgreSQL's diagnostic without SQLAlchemy echoing the SQL text."""
    original = getattr(error, "orig", error)
    return str(original).strip() or type(original).__name__


def _prepare_and_describe(connection, sql: str) -> tuple[str, list[str], float]:
    """Ask PostgreSQL to resolve one statement without reading its result rows.

    ``PREPARE`` performs PostgreSQL's real parse, analysis and rewrite phases,
    including set-operation shape and type resolution against the live catalog.
    ``pg_prepared_statements.result_types`` is the server-resolved description
    of the final result. The returned statement name is executed by the caller
    on this same connection and must be deallocated before it is returned to
    the pool.
    """
    name = f"fyn_analysis_{uuid4().hex}"
    started = perf_counter()
    prepared = False
    try:
        # ``text`` lets SQLAlchemy escape percent signs in model-authored LIKE
        # literals before psycopg sees them as DB-API placeholders.
        connection.execute(text(f"PREPARE {name} AS {sql}"))
        prepared = True
        result_types = connection.execute(
            text(
                "SELECT result_types::text[] "
                "FROM pg_catalog.pg_prepared_statements WHERE name = :name"
            ),
            {"name": name},
        ).scalar_one_or_none()
    except Exception as error:
        raise SqlCompilationError(
            _database_error_detail(error),
            prepared_name=name if prepared else None,
        ) from error
    duration_ms = round((perf_counter() - started) * 1000, 3)
    if result_types is None:
        raise SqlCompilationError(
            "PostgreSQL prepared the statement but did not describe a result set.",
            prepared_name=name,
        )
    return name, list(result_types), duration_ms


def execute_governed_sql(db: Session, user_id: UUID, sql: str) -> dict[str, Any]:
    """Gate, compile, then execute with RLS carrying tenant isolation.

    PostgreSQL: a fresh connection, read-only transaction, statement timeout,
    ``SET LOCAL ROLE`` to the analyst, and the tenant GUC for the policies.
    PostgreSQL prepares and describes the normalized statement on that same
    connection before executing it, making the database the authoritative
    semantic compiler. The transaction is always rolled back and the prepared
    statement is deallocated before the connection returns to the pool.
    SQLite (local/test): the session connection, with the gate plus emulated
    RLS (tenant-filtered derived tables) standing in for the database layer.
    """
    bind = db.get_bind()
    dialect = bind.dialect.name
    gated = gate_sql(sql, execution_dialect="postgres" if dialect == "postgresql" else dialect)

    if dialect == "postgresql":
        engine = bind if isinstance(bind, Engine) else bind.engine
        with engine.connect() as connection:
            transaction = connection.begin()
            prepared_name: str | None = None
            result_types: list[str] = []
            semantic_compile_ms = 0.0
            try:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                connection.exec_driver_sql(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
                connection.execute(
                    text("SELECT set_config(:guc, :uid, true)"),
                    {"guc": TENANT_GUC, "uid": str(user_id)},
                )
                connection.exec_driver_sql(f"SET LOCAL ROLE {ANALYST_ROLE}")
                try:
                    prepared_name, result_types, semantic_compile_ms = _prepare_and_describe(
                        connection, gated.sql
                    )
                except SqlCompilationError as error:
                    prepared_name = error.prepared_name
                    raise
                result = connection.execute(text(f"EXECUTE {prepared_name}"))
                columns = list(result.keys())
                rows = [list(row) for row in result.fetchmany(gated.limit)]
            finally:
                transaction.rollback()
                if prepared_name is not None:
                    try:
                        # Prepared statements are session-scoped and survive a
                        # transaction rollback, so explicitly clean the pooled
                        # connection after restoring it to a usable state.
                        connection.exec_driver_sql(f"DEALLOCATE {prepared_name}")
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        connection.invalidate()
    else:
        scoped_sql = apply_emulated_rls(gated.sql, user_id, dialect=dialect)
        result = db.connection().execute(text(scoped_sql))
        columns = list(result.keys())
        rows = [list(row) for row in result.fetchmany(gated.limit)]
        result_types = []
        semantic_compile_ms = 0.0

    return {
        "columns": columns,
        "result_schema": [
            {"name": name, "type": result_types[index] if index < len(result_types) else None}
            for index, name in enumerate(columns)
        ],
        "rows": rows,
        "row_count": len(rows),
        "limit": gated.limit,
        "tables": sorted(gated.tables),
        "sql": gated.sql,
        "semantic_compile_ms": semantic_compile_ms,
    }

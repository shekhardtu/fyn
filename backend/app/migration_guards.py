"""Idempotent DDL helpers for migrations.

``0001_initial`` builds the schema with ``Base.metadata.create_all``, so a
fresh database arrives at revision 0001 already carrying every table the
*current* models declare — including ones later revisions were written to add.
Those later revisions therefore have to tolerate the object already existing,
or the chain cannot be replayed from scratch at all.

``0002``–``0007`` did this by hand with inspector checks. These helpers are the
same idea in one place, so a new migration gets it right by default rather than
by remembering.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def has_table(name: str) -> bool:
    return _inspector().has_table(name)


def has_column(table: str, column: str) -> bool:
    inspector = _inspector()
    if not inspector.has_table(table):
        return False
    return column in {item["name"] for item in inspector.get_columns(table)}


def has_index(table: str, name: str) -> bool:
    inspector = _inspector()
    if not inspector.has_table(table):
        return False
    return name in {item["name"] for item in inspector.get_indexes(table)}


def has_constraint(table: str, name: str) -> bool:
    inspector = _inspector()
    if not inspector.has_table(table):
        return False
    names = {item["name"] for item in inspector.get_unique_constraints(table)}
    names |= {item["name"] for item in inspector.get_foreign_keys(table)}
    check = getattr(inspector, "get_check_constraints", None)
    if check is not None:
        names |= {item["name"] for item in check(table)}
    return name in names


def create_table_if_absent(name: str, *columns, **kwargs) -> None:
    if not has_table(name):
        op.create_table(name, *columns, **kwargs)


def create_index_if_absent(name: str, table: str, columns, **kwargs) -> None:
    if has_table(table) and not has_index(table, name):
        op.create_index(name, table, columns, **kwargs)


def add_column_if_absent(table: str, column: sa.Column, **kwargs) -> None:
    if has_table(table) and not has_column(table, column.name):
        op.add_column(table, column, **kwargs)


def create_foreign_key_if_absent(name: str, source: str, referent: str, local_cols, remote_cols, **kwargs) -> None:
    if has_table(source) and not has_constraint(source, name):
        op.create_foreign_key(name, source, referent, local_cols, remote_cols, **kwargs)


def create_unique_constraint_if_absent(name: str, table: str, columns, **kwargs) -> None:
    if has_table(table) and not has_constraint(table, name):
        op.create_unique_constraint(name, table, columns, **kwargs)


def create_check_constraint_if_absent(name: str, table: str, condition, **kwargs) -> None:
    if has_table(table) and not has_constraint(table, name):
        op.create_check_constraint(name, table, condition, **kwargs)


def drop_index_if_present(name: str, table: str) -> None:
    if has_index(table, name):
        op.drop_index(name, table_name=table)


def drop_table_if_present(name: str) -> None:
    if has_table(name):
        op.drop_table(name)

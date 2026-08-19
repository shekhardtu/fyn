from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.models import User
from app.seed import default_user
from app.services.external_db import (
    _source_engine,
    build_external_tools,
    build_query_statement,
    connect_external_database,
    query_external_source,
    rescan_external_source,
)
from app.services.spreadsheet import annotate_source_field
from app.services.user_data import export_user_data


@pytest.fixture()
def bank(tmp_path):
    """A scratch sqlite file standing in for the user's external bank database."""
    path = tmp_path / "bank.db"
    writer = create_engine(f"sqlite:///{path}")
    with writer.begin() as connection:
        connection.execute(text(
            "CREATE TABLE transactions ("
            "id INTEGER PRIMARY KEY, txn_date TEXT, merchant TEXT, amount NUMERIC, category TEXT)"
        ))
        connection.execute(text(
            "INSERT INTO transactions (txn_date, merchant, amount, category) VALUES "
            "('2026-08-01', 'Blue Tokai', 450.0, 'Food'), "
            "('2026-08-02', 'Metro card', 100.0, 'Travel'), "
            "('2026-08-03', 'Blue Tokai', 450.0, 'Food')"
        ))
        connection.execute(text("CREATE TABLE secrets (id INTEGER PRIMARY KEY, token TEXT)"))
        connection.execute(text("INSERT INTO secrets (token) VALUES ('do-not-profile')"))
    writer.dispose()
    return SimpleNamespace(path=path, url=f"sqlite:///{path}")


def connect(db, user, bank, name="Bank", tables=("transactions",)):
    return connect_external_database(db, user, name, bank.url, list(tables))


# --- connect + manifest -------------------------------------------------------

def test_connect_posts_v1_with_drafted_roles_and_catalogs(db, bank):
    user = default_user(db)
    source, manifest = connect(db, user, bank)
    assert source.kind == "external_db"
    assert source.config == {"url": bank.url, "tables": ["transactions"]}
    assert manifest.version == 1

    document = manifest.document
    assert set(document["physical"]["tables"]) == {"transactions"}  # allowlist only
    section = document["physical"]["tables"]["transactions"]
    assert section["row_count"] == 3
    by_name = {column["name"]: column for column in section["columns"]}
    assert by_name["amount"]["type"] == "decimal"
    assert by_name["category"]["catalog"]["values"] == ["Food", "Travel"]
    assert by_name["merchant"]["catalog"]["distinct"] == 2

    roles = document["semantics"]["tables"]["transactions"]["columns"]
    assert roles["amount"]["role"] == "money"
    assert roles["category"]["role"] == "category"
    assert roles["merchant"]["role"] == "merchant"
    assert document["annotations"] == {"provenance": "user_stated", "fields": {}}


def test_identical_reconnect_is_a_noop_version(db, bank):
    user = default_user(db)
    _, first = connect(db, user, bank)
    source, again = connect(db, user, bank)
    assert again.id == first.id and again.version == 1
    assert source.config["tables"] == ["transactions"]


def test_unknown_table_is_rejected(db, bank):
    user = default_user(db)
    with pytest.raises(ValueError, match="unknown_table: nope"):
        connect(db, user, bank, tables=("transactions", "nope"))
    with pytest.raises(ValueError, match="no_tables_selected"):
        connect(db, user, bank, tables=())


def test_unsupported_scheme_is_rejected(db):
    user = default_user(db)
    with pytest.raises(ValueError, match="unsupported_scheme: mysql"):
        connect_external_database(db, user, "Bank", "mysql://u:p@h/db", ["transactions"])


# --- querying -----------------------------------------------------------------

def test_query_sums_money_in_minor_units_grouped(db, bank):
    user = default_user(db)
    source, _ = connect(db, user, bank)
    result = query_external_source(
        db, user.id, source.id,
        table="transactions", metric="sum", value_field="amount", group_by="category",
    )
    assert result["columns"] == ["category", "value_minor"]
    assert result["rows"][0] == {"category": "Food", "value_minor": 90_000}
    assert result["rows"][1] == {"category": "Travel", "value_minor": 10_000}
    assert result["source_version"] == 1


def test_query_filters_count_and_average(db, bank):
    user = default_user(db)
    source, _ = connect(db, user, bank)
    contains = query_external_source(
        db, user.id, source.id, table="transactions", metric="count",
        filters=[{"field": "merchant", "operator": "contains", "value": "Blue"}],
    )
    assert contains["rows"] == [{"scope": "all", "value": 2}]
    bounded = query_external_source(
        db, user.id, source.id, table="transactions", metric="count",
        filters=[
            {"field": "amount", "operator": "gte", "value": "200"},
            {"field": "category", "operator": "neq", "value": "Travel"},
        ],
    )
    assert bounded["rows"] == [{"scope": "all", "value": 2}]
    average = query_external_source(
        db, user.id, source.id, table="transactions", metric="average", value_field="amount",
        filters=[{"field": "amount", "operator": "lte", "value": "450"}],
    )
    assert average["rows"] == [{"scope": "all", "value_minor": 33_333}]


@pytest.mark.parametrize("kwargs,code", [
    (dict(table="secrets", metric="count"), "unknown_table"),
    (dict(table="transactions", metric="sum", value_field="ghost"), "unknown_field"),
    (dict(table="transactions", metric="median", value_field="amount"), "unknown_metric"),
    (dict(table="transactions", metric="sum"), "value_field_required"),
    (dict(table="transactions", metric="count",
          filters=[{"field": "amount", "operator": "like", "value": "1"}]), "unknown_operator"),
    (dict(table="transactions", metric="count",
          filters=[{"field": "amount", "operator": "gte", "value": "lots"}]), "non_numeric_filter_value"),
])
def test_query_rejects_invalid_inputs_with_stable_codes(db, bank, kwargs, code):
    user = default_user(db)
    source, _ = connect(db, user, bank)
    with pytest.raises(ValueError, match=code):
        query_external_source(db, user.id, source.id, **kwargs)


def test_query_is_tenant_scoped_by_the_source(db, bank):
    user = default_user(db)
    stranger = User(email="stranger@example.com", display_name="Stranger")
    db.add(stranger)
    db.flush()
    source, _ = connect(db, user, bank)
    with pytest.raises(ValueError, match="unknown_source"):
        query_external_source(db, stranger.id, source.id, table="transactions", metric="count")
    with pytest.raises(ValueError, match="unknown_source"):
        query_external_source(db, user.id, uuid4(), table="transactions", metric="count")


def test_query_statement_binds_every_value(db, bank):
    """The single SELECT carries filter values as bind parameters, never text."""
    user = default_user(db)
    source, manifest = connect(db, user, bank)
    statement = build_query_statement(
        manifest, table="transactions", metric="sum", value_field="amount",
        group_by="category",
        filters=[
            {"field": "merchant", "operator": "eq", "value": "Blue Tokai"},
            {"field": "category", "operator": "contains", "value": "Foo'; DROP TABLE transactions--"},
        ],
        limit=250,
    )
    compiled = statement.compile()
    values = list(compiled.params.values())
    assert "Blue Tokai" in values
    assert any("DROP TABLE" in str(value) for value in values)
    sql = str(compiled)
    assert "Blue Tokai" not in sql and "DROP TABLE" not in sql
    assert sql.count("SELECT") == 1  # exactly one statement, no stacking


def test_contains_wildcards_are_literal_not_pattern_syntax(db, bank):
    """A '%' in the user's value must not widen the filter to everything."""
    user = default_user(db)
    source, _ = connect(db, user, bank)
    widened = query_external_source(
        db, user.id, source.id, table="transactions", metric="count",
        filters=[{"field": "merchant", "operator": "contains", "value": "%"}],
    )
    assert widened["rows"] == [{"scope": "all", "value": 0}]


# --- annotations + rescan -----------------------------------------------------

def test_stated_role_flips_money_semantics(db, bank):
    user = default_user(db)
    source, _ = connect(db, user, bank)
    before = query_external_source(
        db, user.id, source.id, table="transactions", metric="sum", value_field="amount",
    )
    assert before["columns"][-1] == "value_minor"  # drafted money role

    annotated = annotate_source_field(
        db, user, source.id, "transactions.amount",
        "Amount is loyalty points, not money", role="number",
    )
    assert annotated.version == 2
    after = query_external_source(
        db, user.id, source.id, table="transactions", metric="sum", value_field="amount",
    )
    assert after["columns"][-1] == "value"
    assert after["rows"] == [{"scope": "all", "value": 1000.0}]

    with pytest.raises(ValueError, match="unknown_field"):
        annotate_source_field(db, user, source.id, "amount", "must be table-qualified")


def test_annotations_survive_rescan(db, bank):
    user = default_user(db)
    source, _ = connect(db, user, bank)
    annotate_source_field(db, user, source.id, "transactions.amount", "INR including GST")

    writer = create_engine(bank.url)
    with writer.begin() as connection:
        connection.execute(text(
            "INSERT INTO transactions (txn_date, merchant, amount, category) "
            "VALUES ('2026-08-04', 'Big Basket', 900.0, 'Groceries')"
        ))
    writer.dispose()

    rescanned = rescan_external_source(db, user, source.id)
    assert rescanned.version == 3  # connect, annotate, rescan
    assert rescanned.document["physical"]["tables"]["transactions"]["row_count"] == 4
    fields = rescanned.document["annotations"]["fields"]
    assert fields["transactions.amount"]["statement"] == "INR including GST"

    stranger = User(email="stranger2@example.com", display_name="Stranger")
    db.add(stranger)
    db.flush()
    with pytest.raises(ValueError, match="unknown_source"):
        rescan_external_source(db, stranger, source.id)


# --- privacy: the config url never leaves the server --------------------------

def test_config_url_never_reaches_any_surface(db, bank):
    user = default_user(db)
    source, manifest = connect(db, user, bank)
    annotate_source_field(db, user, source.id, "transactions.amount", "INR")
    manifest = rescan_external_source(db, user, source.id)
    secret_fragments = (bank.url, str(bank.path), "mode=ro")

    # 1. The manifest document.
    document_json = json.dumps(manifest.document, default=str)
    assert all(fragment not in document_json for fragment in secret_fragments)

    # 2. The tool description and 3. the query payload.
    [tool] = build_external_tools(SimpleNamespace(db=db, user_id=user.id))
    assert all(fragment not in tool.description for fragment in secret_fragments)
    assert "transactions" in tool.description and "amount (money)" in tool.description
    assert "INR" in tool.description  # user-stated notes are surfaced
    payload = tool.entrypoint(
        data_source_id=str(source.id), table="transactions", metric="count",
        value_field=None, group_by=None, filters=None,
    )
    payload_json = json.dumps(payload, default=str)
    assert payload["kind"] == "external_source_query"
    assert all(fragment not in payload_json for fragment in secret_fragments)
    error = tool.entrypoint(
        data_source_id=str(source.id), table="ghost", metric="count",
        value_field=None, group_by=None, filters=None,
    )
    assert error["error"]["code"] == "invalid_source_query"
    assert all(fragment not in json.dumps(error) for fragment in secret_fragments)

    # 4. The privacy export: the row ships without its config column.
    export = export_user_data(db, user)
    export_json = json.dumps(export, default=str)
    assert all(fragment not in export_json for fragment in secret_fragments)
    exported_sources = [row for row in export["dataSources"] if row["id"] == str(source.id)]
    assert exported_sources and "config" not in exported_sources[0]


# --- read-only + engine cache -------------------------------------------------

def test_external_engine_cannot_write(db, bank):
    user = default_user(db)
    source, _ = connect(db, user, bank)
    engine = _source_engine(source)
    with pytest.raises(OperationalError, match="readonly"):
        with engine.connect() as connection:
            connection.execute(text("INSERT INTO transactions (merchant) VALUES ('intruder')"))


def test_engine_cache_invalidates_when_the_config_url_changes(db, bank, tmp_path):
    user = default_user(db)
    source, _ = connect(db, user, bank)
    first = _source_engine(source)
    assert _source_engine(source) is first  # cached per source id

    moved = tmp_path / "moved.db"
    shutil.copy(bank.path, moved)
    source.config = {"url": f"sqlite:///{moved}", "tables": ["transactions"]}
    db.flush()
    replacement = _source_engine(source)
    assert replacement is not first
    result = query_external_source(db, user.id, source.id, table="transactions", metric="count")
    assert result["rows"] == [{"scope": "all", "value": 3}]


# --- verification findings, pinned -------------------------------------------

def test_a_source_may_not_point_at_the_applications_own_database(monkeypatch, tmp_path):
    """The HIGH finding: fyn's own database read through an owner role bypasses
    RLS entirely, so the target must be refused however it authenticates."""
    from app.config import get_settings
    from app.services.external_db import create_read_only_engine

    own = tmp_path / "fyn.db"
    own.touch()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{own}")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="self_target_forbidden"):
            create_read_only_engine(f"sqlite:///{own}")
        # A different file on the same engine stays connectable.
        other = tmp_path / "bank.db"
        other.touch()
        create_read_only_engine(f"sqlite:///{other}").dispose()

        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://finance:finance@localhost:5432/finance")
        get_settings.cache_clear()
        for attempt in (
            "postgresql+psycopg://finance:finance@localhost:5432/finance",
            "postgresql+psycopg://other:other@127.0.0.1:5432/finance",
        ):
            with pytest.raises(ValueError, match="self_target_forbidden"):
                create_read_only_engine(attempt)
    finally:
        get_settings.cache_clear()


def test_a_configured_host_allowlist_bounds_external_targets(monkeypatch):
    from app.config import get_settings
    from app.services.external_db import create_read_only_engine

    monkeypatch.setenv("EXTERNAL_SOURCE_HOSTS", "warehouse.internal, reports.example.com")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="host_not_allowed"):
            create_read_only_engine("postgresql+psycopg://u:p@evil.example.net:5432/books")
    finally:
        monkeypatch.delenv("EXTERNAL_SOURCE_HOSTS", raising=False)
        get_settings.cache_clear()


def test_connect_failures_never_echo_the_connection_url(tmp_path):
    from app.services.external_db import create_read_only_engine

    missing = tmp_path / "absent.db"
    engine = create_read_only_engine(f"sqlite:///{missing}")
    try:
        with pytest.raises(Exception) as excinfo:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        assert "unable to open" in str(excinfo.value).lower()
    finally:
        engine.dispose()


# --- browser-eval findings, pinned -------------------------------------------

def test_a_money_column_already_in_minor_units_is_not_scaled_again():
    """The browser eval caught this: vendor invoices holding `amount_minor`
    were multiplied by 100 again, reporting ₹2,760 as ₹2,76,000."""
    from decimal import Decimal

    from app.services.spreadsheet import money_already_minor, scale_money

    assert money_already_minor("amount_minor") is True
    assert money_already_minor("value_minor") is True
    assert money_already_minor("paise_total") is True
    # A column that merely contains the letters is not a minor-unit column.
    assert money_already_minor("Reminder") is False
    assert money_already_minor("Budget") is False

    assert scale_money(Decimal("276000"), "amount_minor") == 276000
    assert scale_money(Decimal("2760.00"), "Budget") == 276000


def test_the_tool_catalog_line_carries_the_values_a_filter_must_match(db, tmp_path):
    """A filter written against a guessed spelling returns nothing, and an
    empty result reads as absence. The profiled values travel with the column."""
    from app.services.external_db import connect_external_database, external_catalog_line

    user = default_user(db)
    database = tmp_path / "vendors.db"
    import sqlite3

    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY, vendor TEXT, amount_minor INTEGER)")
    connection.executemany(
        "INSERT INTO invoices (id, vendor, amount_minor) VALUES (?, ?, ?)",
        [(1, "Blue Tokai Coffee", 180000), (2, "Blue Tokai Coffee", 96000)],
    )
    connection.commit()
    connection.close()

    source, manifest = connect_external_database(
        db, user, "Vendors", f"sqlite:///{database}", ["invoices"]
    )
    line = external_catalog_line(source, manifest)

    assert "Blue Tokai Coffee" in line
    assert str(database) not in line  # the url is still never in a prompt

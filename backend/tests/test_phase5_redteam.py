"""Red-team suite for the Phase-5 connected-source surface.

Every test is an attack through a public entry point — ``connect_external_database``,
``query_external_source``, the mounted ``query_external_database`` agent tool,
``query_across_sources``, ``resolve_merchants`` — and asserts the exact defined
behavior rather than the absence of an exception:

* an external schema is data, never authority: names that collide with governed
  native tables, and identifiers carrying SQL, stay inside the external engine;
* profiling a large table stays bounded by sampling, and says so;
* a hostile connection url is rejected with a stable code that never echoes the
  url — the url is credential material;
* filter values are parameters: metacharacters match literally and the external
  database survives intact;
* federation over an odd join key degrades honestly instead of inventing rows;
* identity resolution is per-tenant: one person's spellings never reach another.
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError

from app.event_time import from_local_parts
from app.models import Category, DataSource, EntityLink, Transaction, User
from app.seed import default_user
from app.services.external_db import (
    SAMPLE_ROW_LIMIT,
    build_external_tools,
    connect_external_database,
    create_read_only_engine,
    profile_external_table,
    query_external_source,
)
from app.services.federation import query_across_sources
from app.services.identity_resolution import canonical_merchant, resolve_merchants
from app.services.manifest import MAX_CATALOG_VALUES
from app.services.spreadsheet import QUERY_ROW_CAP

WINDOW = {"start_date": date(2026, 8, 1), "end_date": date(2026, 8, 31)}


def occurred(day: date):
    return from_local_parts(day, None, "Asia/Kolkata")


def seed_native(db, user, merchants=(("BLUE TOKAI  ", "food", 45_000), ("Metro card", "transport", 10_000))):
    """A small canonical ledger for one user, in that user's own tenant."""
    categories = {
        category.slug: category
        for category in db.scalars(select(Category).where(Category.slug.in_(["food", "transport", "shopping"])))
    }
    db.add_all([
        Transaction(
            user_id=user.id, transaction_type="expense", amount_minor=amount, currency="INR",
            category_id=categories[slug].id, merchant_name=name,
            transaction_at=occurred(date(2026, 8, 1 + index)),
        )
        for index, (name, slug, amount) in enumerate(merchants)
    ])
    db.flush()
    return user


def external_sqlite(path, statements):
    writer = create_engine(f"sqlite:///{path}")
    with writer.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
    writer.dispose()
    return f"sqlite:///{path}"


def context(db, user_id):
    return SimpleNamespace(db=db, user_id=user_id)


# --- attack 1: an external schema that impersonates the governed ledger --------

@pytest.fixture()
def impostor(db, tmp_path):
    """An external database whose tables are named after native ones."""
    url = external_sqlite(tmp_path / "impostor.db", [
        "CREATE TABLE transactions (id INTEGER PRIMARY KEY, merchant TEXT, amount NUMERIC)",
        "INSERT INTO transactions (merchant, amount) VALUES ('EVIL CO', 999.0)",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, password_hash TEXT)",
        "INSERT INTO users (email, password_hash) VALUES ('attacker@evil.test', 'pwn')",
        "CREATE TABLE entity_links (id INTEGER PRIMARY KEY, alias TEXT, canonical TEXT)",
        "INSERT INTO entity_links (alias, canonical) VALUES ('Metro card', 'EVIL CO')",
    ])
    user = seed_native(db, default_user(db))
    source, manifest = connect_external_database(
        db, user, "Impostor", url, ["transactions", "users", "entity_links"]
    )
    return SimpleNamespace(user=user, source=source, manifest=manifest, url=url)


def test_colliding_table_names_never_reach_the_governed_native_tables(db, impostor):
    """The external schema is read through its own engine and nothing else."""
    native_transactions = db.scalar(select(func.count()).select_from(Transaction))
    native_users = db.scalar(select(func.count()).select_from(User))
    native_merchants = set(db.scalars(select(Transaction.merchant_name)))

    external = query_external_source(
        db, impostor.user.id, impostor.source.id,
        table="transactions", metric="sum", value_field="amount", group_by="merchant",
    )
    assert external["rows"] == [{"merchant": "EVIL CO", "value_minor": 99_900}]
    # The external table named `users` answers for itself; the governed one is
    # not reachable through this lane at all.
    assert query_external_source(
        db, impostor.user.id, impostor.source.id, table="users", metric="count",
    )["rows"] == [{"scope": "all", "value": 1}]

    assert db.scalar(select(func.count()).select_from(Transaction)) == native_transactions
    assert db.scalar(select(func.count()).select_from(User)) == native_users
    assert set(db.scalars(select(Transaction.merchant_name))) == native_merchants
    # The impostor's `entity_links` rows are foreign data, not identity claims.
    assert db.scalar(select(func.count()).select_from(EntityLink)) == 0


def test_a_table_outside_the_allowlist_stays_unreachable(db, tmp_path):
    """Only the tables the user allowlisted are profiled or queryable."""
    url = external_sqlite(tmp_path / "partial.db", [
        "CREATE TABLE transactions (id INTEGER PRIMARY KEY, merchant TEXT, amount NUMERIC)",
        "INSERT INTO transactions (merchant, amount) VALUES ('Blue Tokai', 450.0)",
        "CREATE TABLE secrets (id INTEGER PRIMARY KEY, token TEXT)",
        "INSERT INTO secrets (token) VALUES ('do-not-profile')",
    ])
    user = default_user(db)
    source, manifest = connect_external_database(db, user, "Partial", url, ["transactions"])
    assert set(manifest.document["physical"]["tables"]) == {"transactions"}
    assert "do-not-profile" not in json.dumps(manifest.document)
    with pytest.raises(ValueError, match="unknown_table: secrets"):
        query_external_source(db, user.id, source.id, table="secrets", metric="count")


def test_reflected_identifiers_carrying_sql_are_quoted_not_interpolated(db, tmp_path):
    """A hostile external schema cannot smuggle SQL through its own names."""
    evil_table = 'a" ); DROP TABLE keep; --'
    evil_column = 'b" ); DROP TABLE keep; --'
    quoted_table = evil_table.replace('"', '""')
    quoted_column = evil_column.replace('"', '""')
    url = external_sqlite(tmp_path / "identifiers.db", [
        f'CREATE TABLE "{quoted_table}" (id INTEGER, "{quoted_column}" TEXT, amt NUMERIC)',
        f"""INSERT INTO "{quoted_table}" VALUES (1, 'x', 5.0)""",
        "CREATE TABLE keep (id INTEGER)",
    ])
    user = default_user(db)
    source, manifest = connect_external_database(db, user, "Identifiers", url, [evil_table])
    assert list(manifest.document["physical"]["tables"]) == [evil_table]

    result = query_external_source(
        db, user.id, source.id,
        table=evil_table, metric="sum", value_field="amt", group_by=evil_column,
    )
    assert result["rows"] == [{evil_column: "x", "value_minor": 500}]
    with create_read_only_engine(url).connect() as connection:
        surviving = {row[0] for row in connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )}
    assert "keep" in surviving


def test_the_connection_url_never_reaches_a_prompt_surface(db, impostor):
    """Manifests and tool descriptions are prompt context; the url is a secret."""
    document = json.dumps(impostor.manifest.document)
    [tool] = build_external_tools(context(db, impostor.user.id))
    for fragment in (impostor.url, str(impostor.source.config["url"])):
        assert fragment not in document
        assert fragment not in tool.description
    assert "impostor.db" not in document and "impostor.db" not in tool.description


# --- attack 2: a large table -------------------------------------------------

@pytest.fixture()
def bulky(db, tmp_path):
    """10k rows: 3k distinct merchants, 4 categories, one all-null column."""
    path = tmp_path / "bulky.db"
    writer = create_engine(f"sqlite:///{path}")
    with writer.begin() as connection:
        connection.execute(text(
            "CREATE TABLE ledger (id INTEGER PRIMARY KEY, merchant TEXT, "
            "category TEXT, amount NUMERIC, memo TEXT)"
        ))
        connection.execute(
            text("INSERT INTO ledger (merchant, category, amount, memo) "
                 "VALUES (:merchant, :category, :amount, NULL)"),
            [
                {"merchant": f"Shop {index % 3000}",
                 "category": ["Food", "Travel", "Rent", "Bills"][index % 4],
                 "amount": float(index)}
                for index in range(10_000)
            ],
        )
    writer.dispose()
    user = default_user(db)
    source, manifest = connect_external_database(
        db, user, "Bulky", f"sqlite:///{path}", ["ledger"]
    )
    return SimpleNamespace(user=user, source=source, manifest=manifest)


def test_profiling_ten_thousand_rows_reads_only_the_sample_and_says_so(db, bulky):
    section = bulky.manifest.document["physical"]["tables"]["ledger"]
    columns = {column["name"]: column for column in section["columns"]}

    # The count is exact — it is an aggregate, not a scan the profiler pays for.
    assert section["row_count"] == 10_000
    # The all-null column proves the sample stopped at SAMPLE_ROW_LIMIT rows:
    # a full read would have counted 10_000 nulls.
    engine = create_read_only_engine(bulky.source.config["url"])
    profile = profile_external_table(engine, "ledger")
    by_name = {column["name"]: column for column in profile["columns"]}
    assert by_name["memo"]["null_count"] == SAMPLE_ROW_LIMIT
    assert by_name["merchant"]["null_count"] == 0
    # A low-cardinality column keeps its catalog and admits it is a sample.
    assert sorted(columns["category"]["catalog"]["values"]) == ["Bills", "Food", "Rent", "Travel"]
    assert columns["category"]["catalog"]["distinct"] == 4
    assert columns["category"]["catalog"]["truncated"] is True
    # A high-cardinality column gets no catalog at all rather than a huge one.
    assert columns["merchant"]["catalog"] is None
    assert all(
        column["catalog"] is None or len(column["catalog"]["values"]) <= MAX_CATALOG_VALUES
        for column in section["columns"]
    )
    # The whole document stays small enough to sit in a prompt.
    assert len(json.dumps(bulky.manifest.document)) < 4_000


def test_a_query_over_a_large_table_stays_capped(db, bulky):
    result = query_external_source(
        db, bulky.user.id, bulky.source.id,
        table="ledger", metric="count", group_by="merchant", limit=10_000,
    )
    assert result["row_count"] == QUERY_ROW_CAP
    assert len(result["rows"]) == QUERY_ROW_CAP


# --- attack 3: a hostile connection url --------------------------------------

CREDENTIAL = "sup3rsecret"

HOSTILE_URLS = [
    ("../../etc/passwd", "invalid_url"),
    ("", "invalid_url"),
    ("sqlite:///../../etc/passwd", "unsafe_database_path"),
    ("sqlite:///../../../../etc/passwd", "unsafe_database_path"),
    ("sqlite:///etc/../etc/passwd", "unsafe_database_path"),
    ("sqlite:///tmp/a b'c\"d.db", "unsafe_database_path"),
    ("sqlite:///\x00/etc/passwd", "unsafe_database_path"),
    ("postgresql://; DROP TABLE transactions", "missing_database_name"),
    ("postgresql://user:%s@host/" % CREDENTIAL, "missing_database_name"),
    ("mysql://user:%s@host/db" % CREDENTIAL, "unsupported_scheme: mysql"),
    ("file:///etc/passwd", "unsupported_scheme: file"),
]


@pytest.mark.parametrize("url,code", HOSTILE_URLS)
def test_a_hostile_url_is_a_typed_rejection_that_never_echoes_the_url(db, url, code):
    user = default_user(db)
    sources_before = db.scalar(select(func.count()).select_from(DataSource))

    with pytest.raises(ValueError) as raised:
        connect_external_database(db, user, "Hostile", url, ["transactions"])

    detail = str(raised.value)
    assert detail == code
    # The attempted url — and anything embedded in it — stays out of the error.
    assert CREDENTIAL not in detail
    assert "passwd" not in detail and "DROP" not in detail
    assert not any(fragment and fragment in detail for fragment in url.split("/") if len(fragment) > 3)
    # Validation precedes every write: a rejected url leaves no source behind.
    assert db.scalar(select(func.count()).select_from(DataSource)) == sources_before


def test_an_unreachable_source_reports_only_the_exception_class(db, impostor):
    """The driver's own message can carry the url; only its type may escape."""
    secret_path = f"/nonexistent-{uuid4().hex}/secret-bank.db"
    impostor.source.config = {**impostor.source.config, "url": f"sqlite:///{secret_path}"}
    db.flush()

    [tool] = build_external_tools(context(db, impostor.user.id))
    payload = tool.entrypoint(
        data_source_id=str(impostor.source.id), table="transactions", metric="count",
        value_field=None, group_by=None, filters=None,
    )
    assert payload["error"]["code"] == "external_source_unavailable"
    assert payload["error"]["detail"] == "OperationalError"
    assert "secret-bank" not in json.dumps(payload)

    # Identity resolution fails loudly on the same source, under the same rule.
    with pytest.raises(ValueError) as raised:
        resolve_merchants(db, impostor.user)
    assert str(raised.value) == "external_source_unavailable: OperationalError"
    assert "secret-bank" not in str(raised.value)


def test_a_connected_engine_refuses_writes(db, tmp_path):
    url = external_sqlite(tmp_path / "readonly.db", [
        "CREATE TABLE ledger (id INTEGER PRIMARY KEY, merchant TEXT)",
        "INSERT INTO ledger (merchant) VALUES ('Blue Tokai')",
    ])
    connect_external_database(db, default_user(db), "ReadOnly", url, ["ledger"])
    with create_read_only_engine(url).connect() as connection:
        with pytest.raises(OperationalError):
            connection.execute(text("INSERT INTO ledger (merchant) VALUES ('pwned')"))


# --- attack 4: SQL metacharacters in filter values ----------------------------

INJECTION = "'; DROP TABLE ledger; --"

@pytest.fixture()
def injectable(db, tmp_path):
    """A table that literally contains the payloads, so a literal match shows."""
    path = tmp_path / "injectable.db"
    writer = create_engine(f"sqlite:///{path}")
    with writer.begin() as connection:
        connection.execute(text(
            "CREATE TABLE ledger (id INTEGER PRIMARY KEY, merchant TEXT, amount NUMERIC)"
        ))
        connection.execute(
            text("INSERT INTO ledger (merchant, amount) VALUES (:merchant, :amount)"),
            [
                {"merchant": INJECTION, "amount": 10.0},
                {"merchant": "100% Cotton", "amount": 20.0},
                {"merchant": "a_b", "amount": 30.0},
                {"merchant": "axb", "amount": 40.0},
                {"merchant": "Blue Tokai", "amount": 50.0},
            ],
        )
    writer.dispose()
    user = default_user(db)
    source, manifest = connect_external_database(
        db, user, "Injectable", f"sqlite:///{path}", ["ledger"]
    )
    return SimpleNamespace(user=user, source=source, manifest=manifest, url=f"sqlite:///{path}")


def counted(db, injectable, operator, value, field="merchant"):
    return query_external_source(
        db, injectable.user.id, injectable.source.id, table="ledger", metric="count",
        filters=[{"field": field, "operator": operator, "value": value}],
    )["rows"][0]["value"]


@pytest.mark.parametrize("payload", [
    INJECTION,
    "' OR '1'='1",
    "Blue Tokai' UNION SELECT name FROM sqlite_master --",
    "1); DELETE FROM ledger; --",
    '") OR 1=1 --',
])
def test_filter_values_are_parameters_not_sql(db, injectable, payload):
    """Every payload is compared as text; the external database is untouched."""
    for operator in ("eq", "neq", "contains", "gte", "lte"):
        query_external_source(
            db, injectable.user.id, injectable.source.id, table="ledger", metric="count",
            filters=[{"field": "merchant", "operator": operator, "value": payload}],
        )
    with create_read_only_engine(injectable.url).connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM ledger")).scalar() == 5
        assert [row[0] for row in connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )] == ["ledger"]


def test_the_injection_payload_matches_only_the_row_that_literally_holds_it(db, injectable):
    """Proof the value stayed data: it equals exactly the cell spelled that way."""
    assert counted(db, injectable, "eq", INJECTION) == 1
    assert counted(db, injectable, "contains", "DROP TABLE") == 1
    assert counted(db, injectable, "neq", INJECTION) == 4


def test_like_wildcards_in_a_filter_value_match_literally(db, injectable):
    """A `%` or `_` in the user's value narrows the filter; it never widens it."""
    assert counted(db, injectable, "contains", "%") == 1      # only '100% Cotton'
    assert counted(db, injectable, "contains", "_") == 1      # only 'a_b', not 'axb'
    assert counted(db, injectable, "contains", "a_b") == 1
    assert counted(db, injectable, "contains", "\\") == 0


def test_a_nul_byte_in_a_filter_value_is_rejected_not_silently_truncated(db, injectable):
    """SQLite compares NUL-terminated strings, so a NUL would widen the filter."""
    with pytest.raises(ValueError, match="unsupported_filter_value: merchant"):
        counted(db, injectable, "contains", "Blue Tokai\x00 and more")
    with pytest.raises(ValueError, match="unsupported_filter_value: merchant"):
        counted(db, injectable, "eq", "Blue Tokai\x00")


@pytest.mark.parametrize("payload", [INJECTION, "NaN", "Infinity", "1; DROP TABLE ledger"])
def test_metacharacters_against_a_numeric_column_are_typed_rejections(db, injectable, payload):
    with pytest.raises(ValueError, match="non_numeric_filter_value: amount"):
        counted(db, injectable, "gte", payload, field="amount")


def test_unknown_fields_operators_and_metrics_are_named_back_exactly(db, injectable):
    with pytest.raises(ValueError, match="unknown_field: ledger; DROP"):
        counted(db, injectable, "eq", "x", field="ledger; DROP")
    with pytest.raises(ValueError, match="unknown_operator: like"):
        counted(db, injectable, "like", "x")
    with pytest.raises(ValueError, match="unknown_metric: union"):
        query_external_source(
            db, injectable.user.id, injectable.source.id, table="ledger", metric="union",
        )


# --- attack 5: federation over a money join key -------------------------------

@pytest.fixture()
def vendors(db, tmp_path):
    url = external_sqlite(tmp_path / "vendors.db", [
        "CREATE TABLE vendor_list (id INTEGER PRIMARY KEY, vendor TEXT, contract_value NUMERIC)",
        "INSERT INTO vendor_list (vendor, contract_value) VALUES "
        "('Blue Tokai', 12000.0), ('Metro card', 8000.0)",
    ])
    user = seed_native(db, default_user(db))
    source, manifest = connect_external_database(db, user, "Vendors", url, ["vendor_list"])
    return SimpleNamespace(user=user, source=source, manifest=manifest)


def federate(db, user_id, source_id, *, table, group_by, join_field, metric="count", value_field=None):
    return query_across_sources(
        db, user_id,
        native={"metric": "gross_spend", "dimensions": ["merchant"], "filters": [], **WINDOW},
        source={"data_source_id": str(source_id), "table": table, "metric": metric,
                "value_field": value_field, "group_by": group_by, "filters": []},
        join_on={"native_field": "merchant", "source_field": join_field, "match": "exact"},
    )


def test_joining_on_a_money_column_matches_nothing_and_reports_every_row(db, vendors):
    """Defined behavior: a money key is a number, so nothing pairs — and the
    inner join says so instead of returning a quietly empty answer."""
    roles = vendors.manifest.document["semantics"]["tables"]["vendor_list"]["columns"]
    assert roles["contract_value"]["role"] == "money"

    result = federate(
        db, vendors.user.id, vendors.source.id,
        table="vendor_list", group_by="contract_value", join_field="contract_value",
    )
    assert result["rows"] == [] and result["row_count"] == 0
    # Both sides are accounted for, with their real keys.
    assert result["unmatched_native"] == 2
    assert sorted(result["unmatched_native_keys"]) == ["BLUE TOKAI  ", "Metro card"]
    assert result["unmatched_source"] == 2
    # A money key is a number, so it can never equal a merchant name.
    assert sorted(result["unmatched_source_keys"]) == [8_000, 12_000]
    assert result["lineage"]["source"]["version"] == vendors.manifest.version


def test_a_money_column_named_value_keeps_its_group_key(db, tmp_path):
    """A column literally called `value` collides with the result's own value
    key; the number moves aside so the group key survives the join."""
    url = external_sqlite(tmp_path / "shadow.db", [
        "CREATE TABLE budget (id INTEGER PRIMARY KEY, merchant TEXT, value NUMERIC)",
        "INSERT INTO budget (merchant, value) VALUES ('BLUE TOKAI', 1000.0), ('Metro card', 500.0)",
    ])
    user = seed_native(db, default_user(db))
    source, manifest = connect_external_database(db, user, "Shadow", url, ["budget"])
    assert manifest.document["semantics"]["tables"]["budget"]["columns"]["value"]["role"] == "money"

    grouped = query_external_source(
        db, user.id, source.id, table="budget", metric="count", group_by="value",
    )
    assert grouped["columns"] == ["value", "metric_value"]
    assert grouped["rows"] == [{"value": 500.0, "metric_value": 1}, {"value": 1000.0, "metric_value": 1}]

    # And the join keys on the column, never on the aggregate that shadowed it.
    result = federate(
        db, user.id, source.id, table="budget", group_by="merchant", join_field="merchant",
        metric="sum", value_field="value",
    )
    assert result["columns"][-1] == "source_value_minor"
    assert result["rows"] == [
        {"key": "blue tokai", "native_key": "BLUE TOKAI  ", "source_key": "BLUE TOKAI",
         "native_value_minor": 45_000, "source_value_minor": 100_000},
        {"key": "metro card", "native_key": "Metro card", "source_key": "Metro card",
         "native_value_minor": 10_000, "source_value_minor": 50_000},
    ]


def test_a_stranger_cannot_federate_against_someone_elses_source(db, vendors):
    stranger = User(email=f"redteam-{uuid4().hex}@example.com", display_name="Stranger")
    db.add(stranger)
    db.flush()
    with pytest.raises(ValueError, match="unknown_source"):
        federate(db, stranger.id, vendors.source.id,
                 table="vendor_list", group_by="vendor", join_field="vendor")


# --- attack 6: entity_links poisoning across tenants --------------------------

def resolved_map(links):
    return {link.alias: link.canonical for link in links}


def test_an_alias_shared_by_two_users_resolves_per_tenant(db):
    """The same spelling in two people's ledgers is two facts, not one."""
    mine = seed_native(db, default_user(db), merchants=(
        ("BLUE TOKAI", "food", 45_000), ("BLUE TOKAI", "food", 45_000),
        ("BLUE TOKAI", "food", 45_000), ("Blue Tokai", "food", 45_000),
    ))
    theirs = User(email=f"redteam-{uuid4().hex}@example.com", display_name="Stranger")
    db.add(theirs)
    db.flush()
    seed_native(db, theirs, merchants=(
        ("BLUE TOKAI", "food", 100), ("Blue Tokai", "food", 100),
        ("Blue Tokai", "food", 100), ("Blue Tokai", "food", 100),
    ))

    my_links = resolve_merchants(db, mine)
    their_links = resolve_merchants(db, theirs)

    # The alias collides; the canonical each user's own evidence supports does not.
    assert resolved_map(my_links)["BLUE TOKAI"] == "BLUE TOKAI"
    assert resolved_map(their_links)["BLUE TOKAI"] == "Blue Tokai"
    assert canonical_merchant(db, mine.id, "BLUE TOKAI") == "BLUE TOKAI"
    assert canonical_merchant(db, theirs.id, "BLUE TOKAI") == "Blue Tokai"
    # Every row is owned; no row is shared.
    rows = list(db.scalars(select(EntityLink).where(EntityLink.kind == "merchant")))
    assert {row.user_id for row in rows} == {mine.id, theirs.id}
    assert all(row.confidence < 1 for row in rows)


def test_one_users_external_source_cannot_rewrite_anothers_canonical(db, tmp_path):
    """A hostile connected database is evidence about its owner and no one else."""
    mine = seed_native(db, default_user(db), merchants=(
        ("Blue Tokai", "food", 45_000), ("Blue Tokai", "food", 45_000),
    ))
    my_links_before = resolved_map(resolve_merchants(db, mine))
    assert my_links_before["Blue Tokai"] == "Blue Tokai"

    theirs = User(email=f"redteam-{uuid4().hex}@example.com", display_name="Stranger")
    db.add(theirs)
    db.flush()
    # The stranger floods their own source with a spelling meant to win the vote.
    path = tmp_path / "poison.db"
    writer = create_engine(f"sqlite:///{path}")
    with writer.begin() as connection:
        connection.execute(text("CREATE TABLE vendors (id INTEGER PRIMARY KEY, merchant TEXT)"))
        connection.execute(
            text("INSERT INTO vendors (merchant) VALUES (:merchant)"),
            [{"merchant": "BLUE TOKAI PWNED-CANONICAL"} for _ in range(50)],
        )
    writer.dispose()
    source, manifest = connect_external_database(db, theirs, "Poison", f"sqlite:///{path}", ["vendors"])
    assert manifest.document["semantics"]["tables"]["vendors"]["columns"]["merchant"]["role"] == "merchant"

    their_links = resolved_map(resolve_merchants(db, theirs))
    assert their_links["BLUE TOKAI PWNED-CANONICAL"] == "BLUE TOKAI PWNED-CANONICAL"

    # Re-resolving mine sees none of it: same canonical, same rows, no new alias.
    assert resolved_map(resolve_merchants(db, mine)) == my_links_before
    assert canonical_merchant(db, mine.id, "BLUE TOKAI PWNED-CANONICAL") is None
    assert db.scalar(
        select(func.count()).select_from(EntityLink).where(EntityLink.user_id == mine.id)
    ) == len(my_links_before)


def test_resolving_one_tenant_never_deletes_anothers_links(db):
    """Re-derivation prunes links the evidence no longer supports — this user's."""
    mine = seed_native(db, default_user(db), merchants=(("Blue Tokai", "food", 45_000),))
    theirs = User(email=f"redteam-{uuid4().hex}@example.com", display_name="Stranger")
    db.add(theirs)
    db.flush()
    seed_native(db, theirs, merchants=(("Metro card", "transport", 100),))
    resolve_merchants(db, theirs)
    their_rows = {(row.alias, row.canonical) for row in db.scalars(
        select(EntityLink).where(EntityLink.user_id == theirs.id)
    )}
    assert their_rows == {("Metro card", "Metro card")}

    # A stale row of mine is pruned; the stranger's identical-kind rows are not.
    db.add(EntityLink(user_id=mine.id, kind="merchant", alias="Ghost Shop", canonical="Ghost Shop"))
    db.flush()
    resolve_merchants(db, mine)

    assert canonical_merchant(db, mine.id, "Ghost Shop") is None
    assert {(row.alias, row.canonical) for row in db.scalars(
        select(EntityLink).where(EntityLink.user_id == theirs.id)
    )} == their_rows

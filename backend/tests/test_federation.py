from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text

from app.event_time import from_local_parts
from app.models import Category, Transaction, User
from app.seed import default_user
from app.services.federation import build_federation_tool, query_across_sources
from app.services.external_db import connect_external_database
from app.services.manifest import active_manifest, native_manifest_fingerprint
from app.services.spreadsheet import annotate_source_field, ensure_spreadsheet_manifest

WINDOW = {"start_date": date(2026, 8, 1), "end_date": date(2026, 8, 31)}

BUDGET_HEADERS = ["Category", "Budget Amount"]
BUDGET_ROWS = [
    ["Food", "1000.00"],
    ["Travel", "500.00"],
    ["Rent", "20000.00"],
]


def occurred(day: date):
    return from_local_parts(day, None, "Asia/Kolkata")


def seed_native(db, user):
    """Canonical August spend: Food 900.00, Shopping 2000.00, Travel 100.00.

    Idempotent, because a test may take both source fixtures at once.
    """
    if db.scalar(select(Transaction).where(Transaction.user_id == user.id)) is not None:
        return user
    categories = {
        category.slug: category
        for category in db.scalars(select(Category).where(Category.slug.in_(["food", "travel", "shopping"])))
    }
    db.add_all([
        # Two Blue Tokai expenses, stored the way a bank statement writes them.
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=45_000, currency="INR",
                    category_id=categories["food"].id, merchant_name="BLUE TOKAI  ",
                    transaction_at=occurred(date(2026, 8, 1))),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=45_000, currency="INR",
                    category_id=categories["food"].id, merchant_name="BLUE TOKAI  ",
                    transaction_at=occurred(date(2026, 8, 3))),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR",
                    category_id=categories["travel"].id, merchant_name="Metro card",
                    transaction_at=occurred(date(2026, 8, 2))),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=200_000, currency="INR",
                    category_id=categories["shopping"].id, merchant_name="Zudio",
                    transaction_at=occurred(date(2026, 8, 4))),
    ])
    db.flush()
    return user


@pytest.fixture()
def budgets(db):
    """An uploaded budget sheet, through the Phase-3 upload path."""
    user = seed_native(db, default_user(db))
    source, manifest = ensure_spreadsheet_manifest(
        db, user, "budgets.csv", BUDGET_HEADERS, [list(row) for row in BUDGET_ROWS]
    )
    return SimpleNamespace(user=user, source=source, manifest=manifest)


@pytest.fixture()
def vendors(db, tmp_path):
    """A connected external database holding a vendor contract list."""
    path = tmp_path / "vendors.db"
    writer = create_engine(f"sqlite:///{path}")
    with writer.begin() as connection:
        connection.execute(text(
            "CREATE TABLE vendor_list (id INTEGER PRIMARY KEY, vendor TEXT, contract_value NUMERIC)"
        ))
        connection.execute(text(
            "INSERT INTO vendor_list (vendor, contract_value) VALUES "
            "('Blue Tokai', 12000.0), ('Zudio online', 8000.0), ('Big Basket', 500.0)"
        ))
    writer.dispose()
    user = seed_native(db, default_user(db))
    source, manifest = connect_external_database(
        db, user, "Vendors", f"sqlite:///{path}", ["vendor_list"]
    )
    return SimpleNamespace(user=user, source=source, manifest=manifest, path=path)


def native_spec(**changes):
    values = {"metric": "gross_spend", "dimensions": ["category"], **WINDOW}
    values.update(changes)
    return values


def context(db, user_id):
    return SimpleNamespace(db=db, user_id=user_id)


# --- native x uploaded spreadsheet, exact keys --------------------------------

def test_category_spend_joins_sheet_budgets_on_exact_keys(db, budgets):
    result = query_across_sources(
        db, budgets.user.id,
        native=native_spec(),
        source={
            "data_source_id": budgets.source.id, "metric": "sum",
            "value_field": "Budget Amount", "group_by": "Category",
        },
        join_on={"native_field": "category", "source_field": "Category", "match": "exact"},
    )

    assert result["columns"] == [
        "key", "native_key", "source_key", "native_value_minor", "source_value_minor",
    ]
    assert result["rows"] == [
        {"key": "food", "native_key": "Food", "source_key": "Food",
         "native_value_minor": 90_000, "source_value_minor": 100_000},
        {"key": "travel", "native_key": "Travel", "source_key": "Travel",
         "native_value_minor": 10_000, "source_value_minor": 50_000},
    ]
    assert result["row_count"] == 2
    assert result["native"]["currency"] == "INR"
    assert result["source"]["kind"] == "spreadsheet"
    assert result["source"]["table"] is None
    # Money on both sides is exact integer minor units, never a float.
    assert all(
        isinstance(row[key], int)
        for row in result["rows"]
        for key in ("native_value_minor", "source_value_minor")
    )


def test_unmatched_rows_are_counted_and_named_not_dropped(db, budgets):
    result = query_across_sources(
        db, budgets.user.id,
        native=native_spec(),
        source={
            "data_source_id": budgets.source.id, "metric": "sum",
            "value_field": "Budget Amount", "group_by": "Category",
        },
        join_on={"native_field": "category", "source_field": "Category", "match": "exact"},
    )
    # Native Shopping has no budget row; the sheet's Rent has no native spend.
    assert result["unmatched_native"] == 1
    assert result["unmatched_native_keys"] == ["Shopping"]
    assert result["unmatched_source"] == 1
    assert result["unmatched_source_keys"] == ["Rent"]
    assert result["native"]["row_count"] == 3 and result["source"]["row_count"] == 3
    assert result["native"]["truncated"] is False and result["source"]["truncated"] is False


def test_native_filters_narrow_only_the_native_side(db, budgets):
    result = query_across_sources(
        db, budgets.user.id,
        # The governed grammar filters categories by slug and projects the name.
        native=native_spec(filters=[{"field": "category", "operator": "eq", "value": "food"}]),
        source={
            "data_source_id": budgets.source.id, "metric": "sum",
            "value_field": "Budget Amount", "group_by": "Category",
        },
        join_on={"native_field": "category", "source_field": "Category", "match": "exact"},
    )
    assert [row["key"] for row in result["rows"]] == ["food"]
    assert result["unmatched_native"] == 0
    assert result["unmatched_source"] == 2  # Travel and Rent keep no partner


def test_a_non_money_native_metric_keeps_a_plain_value_key(db, budgets):
    result = query_across_sources(
        db, budgets.user.id,
        native=native_spec(metric="transaction_count"),
        source={
            "data_source_id": budgets.source.id, "metric": "count", "group_by": "Category",
        },
        join_on={"native_field": "category", "source_field": "Category", "match": "exact"},
    )
    assert result["columns"][-2:] == ["native_value", "source_value"]
    assert result["native"]["currency"] is None
    assert result["rows"][0] == {
        "key": "food", "native_key": "Food", "source_key": "Food",
        "native_value": 2, "source_value": 1,
    }


def test_a_user_stated_role_reaches_the_joined_value_key(db, budgets):
    """The provenance law survives federation: a stated role rekeys the join."""
    annotate_source_field(
        db, budgets.user, budgets.source.id, "Budget Amount",
        "These are reward points, not rupees", role="number",
    )
    result = query_across_sources(
        db, budgets.user.id,
        native=native_spec(),
        source={
            "data_source_id": budgets.source.id, "metric": "sum",
            "value_field": "Budget Amount", "group_by": "Category",
        },
        join_on={"native_field": "category", "source_field": "Category", "match": "exact"},
    )
    assert result["columns"][-2:] == ["native_value_minor", "source_value"]
    assert result["rows"][0]["source_value"] == 1000.0


# --- native x external database, merchant-normalized keys ---------------------

def test_merchant_match_normalizes_both_sides(db, vendors):
    result = query_across_sources(
        db, vendors.user.id,
        native=native_spec(dimensions=["merchant"]),
        source={
            "data_source_id": vendors.source.id, "table": "vendor_list", "metric": "sum",
            "value_field": "contract_value", "group_by": "vendor",
        },
        join_on={"native_field": "merchant", "source_field": "vendor", "match": "merchant"},
    )
    assert result["rows"] == [
        # 'BLUE TOKAI  ' meets 'Blue Tokai', and 'Zudio' meets 'Zudio online'.
        {"key": "blue tokai", "native_key": "BLUE TOKAI  ", "source_key": "Blue Tokai",
         "native_value_minor": 90_000, "source_value_minor": 1_200_000},
        {"key": "zudio", "native_key": "Zudio", "source_key": "Zudio online",
         "native_value_minor": 200_000, "source_value_minor": 800_000},
    ]
    assert result["unmatched_native_keys"] == ["Metro card"]
    assert result["unmatched_source_keys"] == ["Big Basket"]
    assert result["source"]["kind"] == "external_db"
    assert result["source"]["table"] == "vendor_list"


def test_exact_match_does_not_do_the_merchant_normalizers_work(db, vendors):
    """Only 'merchant' strips descriptor noise; 'exact' compares the values."""
    result = query_across_sources(
        db, vendors.user.id,
        native=native_spec(dimensions=["merchant"]),
        source={
            "data_source_id": vendors.source.id, "table": "vendor_list", "metric": "sum",
            "value_field": "contract_value", "group_by": "vendor",
        },
        join_on={"native_field": "merchant", "source_field": "vendor", "match": "exact"},
    )
    assert [row["key"] for row in result["rows"]] == ["blue tokai"]  # trimmed, case-folded
    assert "Zudio" in result["unmatched_native_keys"]
    assert "Zudio online" in result["unmatched_source_keys"]


def test_a_normalized_key_collision_fans_out_instead_of_silently_merging(db, vendors):
    """Two native rows that normalize alike each keep their own joined pair.

    Merging them would invent a total the ledger never reported, and dropping
    one would hide spend; an inner join on the declared key does neither.
    """
    db.add(Transaction(
        user_id=vendors.user.id, transaction_type="expense", amount_minor=25_000,
        currency="INR", merchant_name="Blue Tokai POS", transaction_at=occurred(date(2026, 8, 5)),
    ))
    db.flush()
    result = query_across_sources(
        db, vendors.user.id,
        native=native_spec(dimensions=["merchant"]),
        source={
            "data_source_id": vendors.source.id, "table": "vendor_list", "metric": "sum",
            "value_field": "contract_value", "group_by": "vendor",
        },
        join_on={"native_field": "merchant", "source_field": "vendor", "match": "merchant"},
    )
    collided = [row for row in result["rows"] if row["key"] == "blue tokai"]
    assert [row["native_key"] for row in collided] == ["BLUE TOKAI  ", "Blue Tokai POS"]
    assert [row["native_value_minor"] for row in collided] == [90_000, 25_000]
    assert all(row["source_key"] == "Blue Tokai" for row in collided)
    assert result["unmatched_native_keys"] == ["Metro card"]


def test_a_blank_key_joins_to_nothing_and_is_reported_unmatched(db, budgets):
    sparse, _ = ensure_spreadsheet_manifest(
        db, budgets.user, "sparse.csv", BUDGET_HEADERS, [["Food", "1000.00"], ["", "700.00"]]
    )
    result = query_across_sources(
        db, budgets.user.id,
        native=native_spec(),
        source={
            "data_source_id": sparse.id, "metric": "sum",
            "value_field": "Budget Amount", "group_by": "Category",
        },
        join_on={"native_field": "category", "source_field": "Category", "match": "exact"},
    )
    assert [row["key"] for row in result["rows"]] == ["food"]
    assert result["unmatched_source"] == 1
    assert result["unmatched_source_keys"] == [""]


# --- lineage ------------------------------------------------------------------

def test_lineage_names_both_sources(db, budgets):
    result = query_across_sources(
        db, budgets.user.id,
        native=native_spec(),
        source={
            "data_source_id": budgets.source.id, "metric": "sum",
            "value_field": "Budget Amount", "group_by": "Category",
        },
        join_on={"native_field": "category", "source_field": "Category", "match": "exact"},
    )
    lineage = result["lineage"]
    assert lineage["native"] == {"manifestHash": native_manifest_fingerprint()}
    assert lineage["source"] == {
        "dataSourceId": str(budgets.source.id),
        "manifestHash": budgets.manifest.manifest_hash,
        "version": 1,
    }
    assert datetime.fromisoformat(lineage["joinedAt"]).tzinfo is not None


def test_lineage_follows_the_source_to_its_new_version(db, budgets):
    annotate_source_field(db, budgets.user, budgets.source.id, "Category", "Our internal cost heads")
    result = query_across_sources(
        db, budgets.user.id,
        native=native_spec(),
        source={
            "data_source_id": budgets.source.id, "metric": "sum",
            "value_field": "Budget Amount", "group_by": "Category",
        },
        join_on={"native_field": "category", "source_field": "Category", "match": "exact"},
    )
    current = active_manifest(db, budgets.source)
    assert current.version == 2
    assert result["lineage"]["source"] == {
        "dataSourceId": str(budgets.source.id),
        "manifestHash": current.manifest_hash,
        "version": 2,
    }


# --- rejections with stable codes --------------------------------------------

@pytest.mark.parametrize("native_changes,source_changes,join_changes,code", [
    ({}, {}, {"native_field": "merchant"}, "unknown_join_field: native.merchant"),
    ({}, {}, {"source_field": "Budget Amount"}, "unknown_join_field: source.Budget Amount"),
    ({}, {}, {"match": "fuzzy"}, "unknown_join_match: fuzzy"),
    ({}, {"group_by": None}, {}, "missing_source_field: group_by"),
    ({}, {"metric": None}, {}, "missing_source_field: metric"),
    ({"metric": None}, {}, {}, "missing_native_field: metric"),
    ({}, {"value_field": "ghost"}, {}, "unknown_field: ghost"),
    ({}, {"metric": "median"}, {}, "unknown_metric: median"),
    ({}, {"filters": [{"field": "Category", "operator": "like", "value": "F"}]}, {}, "unknown_operator"),
    ({}, {"table": "vendor_list"}, {}, "table_not_supported_for_spreadsheet_source"),
    ({"metric": "invented_revenue"}, {}, {}, "Unknown governed metric"),
    ({"dimensions": ["lender", "category"]}, {}, {}, "not valid for transactions"),
])
def test_rejections_carry_stable_codes(db, budgets, native_changes, source_changes, join_changes, code):
    source = {
        "data_source_id": budgets.source.id, "metric": "sum",
        "value_field": "Budget Amount", "group_by": "Category",
    }
    source.update(source_changes)
    join_on = {"native_field": "category", "source_field": "Category", "match": "exact"}
    join_on.update(join_changes)
    with pytest.raises(ValueError, match=code):
        query_across_sources(
            db, budgets.user.id,
            native=native_spec(**native_changes), source=source, join_on=join_on,
        )


def test_an_external_source_side_requires_its_table(db, vendors):
    with pytest.raises(ValueError, match="missing_source_field: table"):
        query_across_sources(
            db, vendors.user.id,
            native=native_spec(dimensions=["merchant"]),
            source={
                "data_source_id": vendors.source.id, "metric": "sum",
                "value_field": "contract_value", "group_by": "vendor",
            },
            join_on={"native_field": "merchant", "source_field": "vendor", "match": "merchant"},
        )


# --- tenancy ------------------------------------------------------------------

def test_a_stranger_cannot_reach_this_users_source(db, budgets):
    stranger = User(email="federation-stranger@example.com", display_name="Stranger")
    db.add(stranger)
    db.flush()
    source = {
        "data_source_id": budgets.source.id, "metric": "sum",
        "value_field": "Budget Amount", "group_by": "Category",
    }
    join_on = {"native_field": "category", "source_field": "Category", "match": "exact"}
    with pytest.raises(ValueError, match="unknown_source"):
        query_across_sources(db, stranger.id, native=native_spec(), source=source, join_on=join_on)
    with pytest.raises(ValueError, match="unknown_source"):
        query_across_sources(
            db, budgets.user.id, native=native_spec(),
            source={**source, "data_source_id": uuid4()}, join_on=join_on,
        )


def test_the_native_side_carries_its_own_tenant_scope(db, budgets):
    """A stranger joining their own sheet sees none of this user's spending."""
    stranger = User(email="federation-stranger2@example.com", display_name="Stranger")
    db.add(stranger)
    db.flush()
    theirs, _ = ensure_spreadsheet_manifest(
        db, stranger, "budgets.csv", BUDGET_HEADERS, [list(row) for row in BUDGET_ROWS]
    )
    result = query_across_sources(
        db, stranger.id,
        native=native_spec(),
        source={
            "data_source_id": theirs.id, "metric": "sum",
            "value_field": "Budget Amount", "group_by": "Category",
        },
        join_on={"native_field": "category", "source_field": "Category", "match": "exact"},
    )
    assert result["rows"] == []
    assert result["native"]["row_count"] == 0
    assert result["unmatched_source"] == 3  # the sheet is theirs; the ledger is not


# --- the agent tool -----------------------------------------------------------

def test_the_tool_is_not_mounted_without_a_non_native_source(db):
    user = seed_native(db, default_user(db))
    assert build_federation_tool(context(db, user.id)) == []


def test_the_tool_describes_both_sides_and_every_owned_source(db, budgets, vendors):
    [tool] = build_federation_tool(context(db, budgets.user.id))
    assert tool.name == "query_across_sources"
    assert tool.strict is True
    assert f"data_source_id={budgets.source.id}" in tool.description
    assert f"data_source_id={vendors.source.id}" in tool.description
    assert "Budget Amount (money)" in tool.description
    # Columns and roles are listed per table; each may also carry the profiled
    # values a filter has to match, so this asserts membership, not spelling.
    for fragment in ("vendor_list:", "id (number)", "vendor (merchant)", "contract_value (money)"):
        assert fragment in tool.description
    assert "native_join_field must be one of native_dimensions" in tool.description
    assert set(tool.parameters["required"]) == set(tool.parameters["properties"])
    assert tool.parameters["additionalProperties"] is False


def test_the_tool_runs_the_join_and_reports_lineage(db, budgets):
    [tool] = build_federation_tool(context(db, budgets.user.id))
    payload = tool.entrypoint(
        native_metric="gross_spend", native_dimensions=["category"], native_filters=None,
        start_date="2026-08-01", end_date="2026-08-31",
        data_source_id=str(budgets.source.id), table=None, source_metric="sum",
        source_value_field="Budget Amount", source_group_by="Category", source_filters=None,
        native_join_field="category", source_join_field="Category", match="exact",
    )
    assert payload["kind"] == "federated_query"
    assert payload["rows"][0]["native_value_minor"] == 90_000
    assert payload["lineage"]["native"]["manifestHash"] == native_manifest_fingerprint()
    assert payload["lineage"]["source"]["dataSourceId"] == str(budgets.source.id)


def test_the_tool_returns_a_typed_error_for_an_unknown_join_field(db, budgets):
    [tool] = build_federation_tool(context(db, budgets.user.id))
    payload = tool.entrypoint(
        native_metric="gross_spend", native_dimensions=["category"], native_filters=None,
        start_date="2026-08-01", end_date="2026-08-31",
        data_source_id=str(budgets.source.id), table=None, source_metric="sum",
        source_value_field="Budget Amount", source_group_by="Category", source_filters=None,
        native_join_field="merchant", source_join_field="Category", match="exact",
    )
    assert payload["error"]["code"] == "invalid_federated_query"
    assert payload["error"]["detail"] == "unknown_join_field: native.merchant"
    assert "native_join_field must be one of native_dimensions" in payload["error"]["hint"]

    unowned = tool.entrypoint(
        native_metric="gross_spend", native_dimensions=["category"], native_filters=None,
        start_date="2026-08-01", end_date="2026-08-31",
        data_source_id=str(uuid4()), table=None, source_metric="sum",
        source_value_field="Budget Amount", source_group_by="Category", source_filters=None,
        native_join_field="category", source_join_field="Category", match="exact",
    )
    assert unowned["error"]["detail"] == "unknown_source"

"""Identity resolution and deterministic customer traits.

Two lanes share this file because they share one promise: what they store is a
derivation of the user's own data, never a guess and never a leftover. The
tests below pin the arithmetic, the tenant boundary, the idempotence of both
upserts, and the rule that a trait value is never surfaced without its stamp.
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text

from app.config import get_settings
from app.models import Category, EntityLink, Transaction, User, UserTrait
from app.seed import default_user
from app.services import conversation as conversation_service
from app.services.cdp import (
    TRAIT_CATEGORY_BASELINES,
    TRAIT_INCOME_CADENCE,
    TRAIT_RECURRING_LOAD,
    compute_traits,
    get_traits,
    traits_context_line,
)
from app.services.conversation import get_or_create_conversation, handle_chat
from app.services.external_db import connect_external_database
from app.services.identity_resolution import (
    canonical_merchant,
    link_confidence,
    resolve_merchants,
)
from app.services.spreadsheet import annotate_source_field, ensure_spreadsheet_manifest
from app.services.sql_gate import EXTRA_USER_TENANT_TABLES, _tenant_predicate

TODAY = date(2026, 8, 16)


def _second_user(db) -> User:
    user = User(email="other@example.com", display_name="Other")
    db.add(user)
    db.flush()
    return user


def _expense(db, user, *, merchant=None, amount=10_000, at, category=None):
    row = Transaction(
        user_id=user.id,
        transaction_type="expense",
        amount_minor=amount,
        currency="INR",
        merchant_name=merchant,
        category_id=category.id if category is not None else None,
        transaction_at=at,
        status="confirmed",
    )
    db.add(row)
    return row


def _income(db, user, *, at, amount=300_000):
    row = Transaction(
        user_id=user.id,
        transaction_type="income",
        amount_minor=amount,
        currency="INR",
        transaction_at=at,
        status="confirmed",
    )
    db.add(row)
    return row


def _at(year, month, day, hour=9) -> datetime:
    """A mid-morning UTC instant, far from any Asia/Kolkata date boundary."""
    return datetime(year, month, day, hour, 0, tzinfo=timezone.utc)


def _links(db, user) -> dict[str, str]:
    return {
        row.alias: row.canonical
        for row in db.scalars(select(EntityLink).where(EntityLink.user_id == user.id))
    }


# --- identity resolution ------------------------------------------------------

def test_merchant_links_unify_ledger_and_sheet_spellings(db):
    user = default_user(db)
    _expense(db, user, merchant="Blue Tokai", at=_at(2026, 8, 1))
    _expense(db, user, merchant="Blue Tokai", at=_at(2026, 8, 2))
    _expense(db, user, merchant="BLUE TOKAI", at=_at(2026, 8, 3))
    _expense(db, user, merchant="Metro Card", at=_at(2026, 8, 4))
    db.flush()
    ensure_spreadsheet_manifest(
        db, user, "expenses.csv",
        ["Date", "Merchant", "Amount"],
        [
            ["2026-08-05", "Blue Tokai Online", "450"],
            ["2026-08-06", "blue-tokai", "450"],
        ],
    )

    resolved = resolve_merchants(db, user)

    # Every spelling of the same shop resolves to the most frequently written
    # original, including the canonical spelling's link to itself.
    assert _links(db, user) == {
        "Blue Tokai": "Blue Tokai",
        "BLUE TOKAI": "Blue Tokai",
        "Blue Tokai Online": "Blue Tokai",
        "blue-tokai": "Blue Tokai",
        "Metro Card": "Metro Card",
    }
    assert canonical_merchant(db, user.id, "blue-tokai") == "Blue Tokai"
    assert canonical_merchant(db, user.id, "never seen") is None
    assert [row.kind for row in resolved] == ["merchant"] * 5


def test_confidence_rises_with_support_and_never_reaches_certainty(db):
    user = default_user(db)
    for index in range(12):
        _expense(db, user, merchant="Blue Tokai", at=_at(2026, 8, 1 + index % 20))
    _expense(db, user, merchant="BLUE TOKAI", at=_at(2026, 8, 25))
    db.flush()

    resolve_merchants(db, user)
    links = {row.alias: row for row in db.scalars(select(EntityLink))}

    # One sighting is the floor; repetition raises confidence to a bound that
    # is deliberately short of 1 — inference is never a user statement.
    assert links["BLUE TOKAI"].confidence == Decimal("0.500")
    assert links["Blue Tokai"].confidence == Decimal("0.950")
    assert link_confidence(1) == Decimal("0.500")
    assert link_confidence(3) == Decimal("0.600")
    assert max(link_confidence(n) for n in range(1, 500)) < Decimal("1")


def test_a_user_stated_merchant_role_brings_a_column_into_resolution(db):
    user = default_user(db)
    _expense(db, user, merchant="Blue Tokai", at=_at(2026, 8, 1))
    _expense(db, user, merchant="Blue Tokai", at=_at(2026, 8, 2))
    db.flush()
    source, _ = ensure_spreadsheet_manifest(
        db, user, "ledger.csv",
        ["Date", "Narration", "Amount"],
        [["2026-08-05", "BLUE TOKAI", "450"]],
    )

    # The profiler reads "Narration" as a description, so nothing joins yet.
    resolve_merchants(db, user)
    assert _links(db, user) == {"Blue Tokai": "Blue Tokai"}

    annotate_source_field(
        db, user, source.id, "Narration", "This column holds the shop name", role="merchant"
    )
    resolve_merchants(db, user)

    # The user's stated role wins over the inference, immediately.
    assert _links(db, user) == {"Blue Tokai": "Blue Tokai", "BLUE TOKAI": "Blue Tokai"}


def test_external_source_merchants_join_the_same_identity(db, tmp_path):
    user = default_user(db)
    path = tmp_path / "bank.db"
    writer = create_engine(f"sqlite:///{path}")
    with writer.begin() as connection:
        connection.execute(text(
            "CREATE TABLE txns (id INTEGER PRIMARY KEY, merchant TEXT, amount NUMERIC)"
        ))
        connection.execute(text(
            "INSERT INTO txns (merchant, amount) VALUES "
            "('BLUE TOKAI POS', 450.0), ('BLUE TOKAI POS', 450.0), ('Metro card', 100.0)"
        ))
    writer.dispose()
    _expense(db, user, merchant="Blue Tokai", at=_at(2026, 8, 1))
    _expense(db, user, merchant="Blue Tokai", at=_at(2026, 8, 2))
    _expense(db, user, merchant="Blue Tokai", at=_at(2026, 8, 3))
    db.flush()
    source, _ = connect_external_database(db, user, "Bank", f"sqlite:///{path}", ["txns"])

    resolve_merchants(db, user)

    assert _links(db, user)["BLUE TOKAI POS"] == "Blue Tokai"
    by_alias = {row.alias: row for row in db.scalars(select(EntityLink))}
    # A spelling only one source writes is attributed to it; a spelling several
    # places write is attributed to none of them.
    assert by_alias["BLUE TOKAI POS"].source_id == source.id
    assert by_alias["Blue Tokai"].source_id is None


def test_resolution_never_reads_or_writes_another_users_spellings(db):
    user = default_user(db)
    stranger = _second_user(db)
    _expense(db, user, merchant="Blue Tokai", at=_at(2026, 8, 1))
    _expense(db, stranger, merchant="Someone Elses Clinic", at=_at(2026, 8, 1))
    db.flush()
    ensure_spreadsheet_manifest(
        db, stranger, "theirs.csv", ["Merchant"], [["Private Vendor"]]
    )

    resolve_merchants(db, user)

    assert _links(db, user) == {"Blue Tokai": "Blue Tokai"}
    stored = list(db.scalars(select(EntityLink)))
    assert {row.user_id for row in stored} == {user.id}
    assert all("Clinic" not in row.alias and "Private" not in row.alias for row in stored)

    resolve_merchants(db, stranger)
    assert _links(db, stranger) == {
        "Someone Elses Clinic": "Someone Elses Clinic",
        "Private Vendor": "Private Vendor",
    }
    assert _links(db, user) == {"Blue Tokai": "Blue Tokai"}


def test_resolution_is_idempotent_and_rewrites_rather_than_duplicating(db):
    user = default_user(db)
    _expense(db, user, merchant="Blue Tokai", at=_at(2026, 8, 1))
    _expense(db, user, merchant="BLUE TOKAI", at=_at(2026, 8, 2))
    _expense(db, user, merchant="BLUE TOKAI", at=_at(2026, 8, 3))
    db.flush()

    first = resolve_merchants(db, user)
    second = resolve_merchants(db, user)

    assert [(row.alias, row.canonical) for row in first] == [
        (row.alias, row.canonical) for row in second
    ]
    assert db.scalar(select(func.count()).select_from(EntityLink)) == 2
    assert _links(db, user)["Blue Tokai"] == "BLUE TOKAI"

    # The lower-case spelling overtakes the upper-case one. The same aliases
    # must be rewritten in place, not duplicated under a second canonical.
    for day in (4, 5):
        _expense(db, user, merchant="Blue Tokai", at=_at(2026, 8, day))
    db.flush()
    resolve_merchants(db, user)

    assert db.scalar(select(func.count()).select_from(EntityLink)) == 2
    assert _links(db, user) == {"Blue Tokai": "Blue Tokai", "BLUE TOKAI": "Blue Tokai"}


def test_a_duplicate_alias_left_by_another_writer_is_reconciled_away(db):
    """The unique key allows one alias under two canonicals; resolution does not.

    A second row for the same spelling is residue whichever way it got there,
    and the surviving row has to be free to take the new canonical.
    """
    user = default_user(db)
    _expense(db, user, merchant="Blue Tokai", at=_at(2026, 8, 1))
    db.flush()
    db.add_all([
        EntityLink(user_id=user.id, kind="merchant", canonical="Stale One", alias="Blue Tokai"),
        EntityLink(user_id=user.id, kind="merchant", canonical="Stale Two", alias="Blue Tokai"),
    ])
    db.flush()

    resolve_merchants(db, user)

    assert db.scalar(select(func.count()).select_from(EntityLink)) == 1
    assert _links(db, user) == {"Blue Tokai": "Blue Tokai"}


def test_a_link_disappears_when_its_spelling_leaves_the_data(db):
    user = default_user(db)
    kept = _expense(db, user, merchant="Blue Tokai", at=_at(2026, 8, 1))
    removed = _expense(db, user, merchant="Closed Shop", at=_at(2026, 8, 2))
    db.flush()
    resolve_merchants(db, user)
    assert "Closed Shop" in _links(db, user)

    removed.deleted_at = _at(2026, 8, 3)
    db.flush()
    resolve_merchants(db, user)

    # An identity the evidence no longer supports is residue, not resolution.
    assert _links(db, user) == {kept.merchant_name: kept.merchant_name}


# --- traits: arithmetic -------------------------------------------------------

def test_income_cadence_reports_the_median_gap_and_names_it_monthly(db):
    user = default_user(db)
    for at in (_at(2026, 5, 1), _at(2026, 5, 31), _at(2026, 6, 30)):
        _income(db, user, at=at)
    db.flush()

    traits = {trait.name: trait.value for trait in compute_traits(db, user, TODAY)}

    assert traits[TRAIT_INCOME_CADENCE] == {
        "cadence": "monthly",
        "median_gap_days": 30,
        "observations": 3,
    }


def test_irregular_income_is_not_dressed_up_as_a_cadence(db):
    user = default_user(db)
    for at in (_at(2026, 7, 1), _at(2026, 7, 4), _at(2026, 7, 9)):
        _income(db, user, at=at)
    db.flush()

    traits = {trait.name: trait.value for trait in compute_traits(db, user, TODAY)}
    assert traits[TRAIT_INCOME_CADENCE]["cadence"] == "irregular"
    assert traits[TRAIT_INCOME_CADENCE]["median_gap_days"] == 4


def test_a_single_income_record_produces_no_cadence_trait(db):
    user = default_user(db)
    _income(db, user, at=_at(2026, 7, 1))
    db.flush()

    names = {trait.name for trait in compute_traits(db, user, TODAY)}
    assert TRAIT_INCOME_CADENCE not in names


def test_category_baselines_average_three_complete_months_only(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    transport = db.scalar(select(Category).where(Category.slug == "travel"))
    for month in (5, 6, 7):
        _expense(db, user, amount=30_000, at=_at(2026, month, 10), category=food)
    _expense(db, user, amount=15_000, at=_at(2026, 6, 12), category=transport)
    _expense(db, user, amount=3_000, at=_at(2026, 5, 14))  # no category
    # Outside the window in both directions: the month in progress and the
    # month before the baseline both have to be ignored.
    _expense(db, user, amount=999_000, at=_at(2026, 8, 2), category=food)
    _expense(db, user, amount=999_000, at=_at(2026, 4, 28), category=food)
    db.flush()

    traits = {trait.name: trait.value for trait in compute_traits(db, user, TODAY)}
    baselines = traits[TRAIT_CATEGORY_BASELINES]

    assert baselines["months"] == ["2026-05", "2026-06", "2026-07"]
    assert baselines["currency"] == "INR"
    assert baselines["categories"] == [
        {"slug": "food", "name": "Food", "mean_minor": 30_000},
        {"slug": "travel", "name": "Travel", "mean_minor": 5_000},
    ]
    # Spend without a category is reported, not silently dropped.
    assert baselines["uncategorized_mean_minor"] == 1_000


def test_recurring_load_converts_weekly_charges_to_a_monthly_cost(db):
    user = default_user(db)
    for at in (_at(2026, 5, 10), _at(2026, 6, 10), _at(2026, 7, 10)):
        _expense(db, user, merchant="Netflix", amount=49_900, at=at)
    for at in (_at(2026, 7, 1), _at(2026, 7, 8), _at(2026, 7, 15)):
        _expense(db, user, merchant="Corner Cafe", amount=20_000, at=at)
    db.flush()

    traits = {trait.name: trait.value for trait in compute_traits(db, user, TODAY)}
    load = traits[TRAIT_RECURRING_LOAD]

    # 49_900 monthly + 20_000 * 52/12 weekly, rounded half up to minor units.
    assert load == {
        "monthly_minor": 136_567,
        "currency": "INR",
        "pattern_count": 2,
        "cadences": {"monthly": 1, "weekly": 1},
    }


# --- traits: freshness, idempotence, isolation --------------------------------

def test_every_trait_carries_the_moment_and_the_window_it_was_computed_from(db):
    user = default_user(db)
    for at in (_at(2026, 5, 1), _at(2026, 5, 31), _at(2026, 6, 30)):
        _income(db, user, at=at)
    _expense(db, user, amount=30_000, at=_at(2026, 6, 10))
    db.flush()

    traits = compute_traits(db, user, TODAY)

    assert traits, "the seeded data supports at least one trait"
    assert all(trait.computed_at is not None for trait in traits)
    assert {trait.freshness_note for trait in traits} == {
        "computed from data through 2026-08-16"
    }
    line = traits_context_line(traits)
    # The stamp travels with every value, so a reader can never mistake an old
    # number for a current one.
    assert line.count("computed_at") == len(traits)
    assert all(trait.name in line for trait in traits)
    assert "2026-08-16" in line


def test_recomputing_upserts_in_place_and_never_duplicates_a_trait(db):
    user = default_user(db)
    for at in (_at(2026, 5, 1), _at(2026, 5, 31), _at(2026, 6, 30)):
        _income(db, user, at=at)
    db.flush()

    first = compute_traits(db, user, TODAY)
    first_ids = {trait.id for trait in first}
    stamps = {trait.name: trait.computed_at for trait in first}

    second = compute_traits(db, user, TODAY)

    assert {trait.id for trait in second} == first_ids
    assert db.scalar(select(func.count()).select_from(UserTrait)) == len(first)
    assert all(trait.computed_at >= stamps[trait.name] for trait in second)


def test_a_trait_is_deleted_when_its_evidence_disappears(db):
    user = default_user(db)
    incomes = [_income(db, user, at=at) for at in (_at(2026, 5, 1), _at(2026, 5, 31))]
    db.flush()
    assert TRAIT_INCOME_CADENCE in {trait.name for trait in compute_traits(db, user, TODAY)}

    for row in incomes:
        row.deleted_at = _at(2026, 6, 1)
    db.flush()
    compute_traits(db, user, TODAY)

    # No placeholder, no last-known value: the trait is simply gone.
    assert db.scalar(
        select(func.count()).select_from(UserTrait).where(UserTrait.name == TRAIT_INCOME_CADENCE)
    ) == 0


def test_stale_traits_are_recomputed_and_fresh_ones_are_served_as_stored(db):
    user = default_user(db)
    for at in (_at(2026, 5, 1), _at(2026, 5, 31), _at(2026, 6, 30)):
        _income(db, user, at=at)
    db.flush()
    stored = compute_traits(db, user, TODAY)
    original = {trait.name: trait.computed_at for trait in stored}

    unchanged = get_traits(db, user, today=TODAY)
    assert {trait.computed_at for trait in unchanged} == set(original.values())

    for trait in stored:
        trait.computed_at = trait.computed_at - timedelta(hours=48)
    db.flush()
    # New evidence landed while the stored traits went stale.
    _income(db, user, at=_at(2026, 7, 30))
    db.flush()

    refreshed = get_traits(db, user, today=TODAY, max_age_hours=24)

    assert all(trait.computed_at > original[trait.name] for trait in refreshed)
    cadence = next(trait for trait in refreshed if trait.name == TRAIT_INCOME_CADENCE)
    assert cadence.value["observations"] == 4


def test_traits_are_computed_from_one_users_data_only(db):
    user = default_user(db)
    stranger = _second_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    for month in (5, 6, 7):
        _expense(db, user, amount=30_000, at=_at(2026, month, 10), category=food)
        _expense(db, stranger, amount=900_000, at=_at(2026, month, 11), category=food)
    db.flush()

    mine = {trait.name: trait.value for trait in compute_traits(db, user, TODAY)}
    theirs = {trait.name: trait.value for trait in compute_traits(db, stranger, TODAY)}

    assert mine[TRAIT_CATEGORY_BASELINES]["categories"][0]["mean_minor"] == 30_000
    assert theirs[TRAIT_CATEGORY_BASELINES]["categories"][0]["mean_minor"] == 900_000
    assert {row.user_id for row in db.scalars(select(UserTrait))} == {user.id, stranger.id}


def test_a_user_with_no_records_gets_no_traits_and_no_empty_line(db):
    user = default_user(db)

    traits = get_traits(db, user, today=TODAY)

    assert traits == []
    # An empty line would read as "no income, no baselines" rather than "not
    # computed", which is why the surface gates on the list being non-empty.
    assert traits_context_line(traits) == ""


# --- the operator surface -----------------------------------------------------

@pytest.fixture()
def agent_enabled(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _captured_workflow_context(db, user, monkeypatch, message: str) -> dict:
    captured: dict = {}

    def operator_runner(*args, **kwargs):
        captured.update(kwargs.get("workflow_context") or {})
        return None

    monkeypatch.setattr(conversation_service, "run_operator", operator_runner)
    handle_chat(db, user, get_or_create_conversation(db, user), message)
    return captured


def test_the_operator_turn_carries_traits_with_their_stamp(db, monkeypatch, agent_enabled):
    user = default_user(db)
    for at in (_at(2026, 5, 1), _at(2026, 5, 31), _at(2026, 6, 30)):
        _income(db, user, at=at)
    db.commit()

    context = _captured_workflow_context(db, user, monkeypatch, "Share 3 recent transactions")

    assert TRAIT_INCOME_CADENCE in context["userTraits"]
    assert "computed_at" in context["userTraits"]
    assert "monthly" in context["userTraits"]


def test_the_operator_turn_omits_the_traits_key_when_there_are_none(db, monkeypatch, agent_enabled):
    user = default_user(db)

    context = _captured_workflow_context(db, user, monkeypatch, "Share 3 recent transactions")

    assert context, "the operator turn was not reached"
    assert "userTraits" not in context


def test_the_operator_turn_never_carries_a_stale_trait(db, monkeypatch, agent_enabled):
    """Reading `user_traits` directly would surface whatever was last written,
    no matter how old. The turn has to go through the accessor that recomputes
    first, so what the Operator sees is never yesterday's number."""
    user = default_user(db)
    for at in (_at(2026, 5, 1), _at(2026, 5, 31), _at(2026, 6, 30)):
        _income(db, user, at=at)
    db.flush()
    stale = compute_traits(db, user, TODAY)
    stale_stamps = {trait.computed_at.isoformat() for trait in stale}
    for trait in stale:
        trait.computed_at = trait.computed_at - timedelta(days=30)
    _income(db, user, at=_at(2026, 7, 30))
    db.commit()

    context = _captured_workflow_context(db, user, monkeypatch, "Share 3 recent transactions")

    assert all(stamp not in context["userTraits"] for stamp in stale_stamps)
    cadence = db.scalar(
        select(UserTrait).where(UserTrait.name == TRAIT_INCOME_CADENCE, UserTrait.user_id == user.id)
    )
    assert cadence.value["observations"] == 4


def test_external_source_failure_stops_resolution_without_leaking_the_url(db, tmp_path):
    user = default_user(db)
    path = tmp_path / "bank.db"
    writer = create_engine(f"sqlite:///{path}")
    with writer.begin() as connection:
        connection.execute(text("CREATE TABLE txns (id INTEGER PRIMARY KEY, merchant TEXT)"))
        connection.execute(text("INSERT INTO txns (merchant) VALUES ('Blue Tokai')"))
    connect_external_database(db, user, "Bank", f"sqlite:///{path}", ["txns"])
    # The remote table the manifest was profiled from is gone underneath us.
    with writer.begin() as connection:
        connection.execute(text("DROP TABLE txns"))
    writer.dispose()

    with pytest.raises(ValueError) as excinfo:
        resolve_merchants(db, user)

    message = str(excinfo.value)
    assert message.startswith("external_source_unavailable:")
    # A partial map presented as complete would be worse than the failure, and
    # the driver's own message — which can echo the url — never travels.
    assert str(path) not in message and "sqlite" not in message


def test_the_baseline_secures_both_tables_and_the_sql_lane_knows_their_tenant_rule():
    """The Postgres grant and the SQLite emulation of it must not drift.

    The baseline grants the analyst role SELECT on both tables. On SQLite there is
    no row-level security, so `sql_gate` rewrites the tenant filter in software
    — a table the role can read but the rewrite does not recognise would be
    returned unfiltered there while Postgres held the line.
    """
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0001_baseline.py"
    spec = importlib.util.spec_from_file_location("migration_baseline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    secured = {"entity_links", "user_traits"}
    assert secured <= set(module.EXTENDED_USER_TABLES)
    assert secured <= EXTRA_USER_TENANT_TABLES
    for table in secured:
        assert _tenant_predicate(table, "'tenant'") == "user_id = 'tenant'"


def test_the_trait_context_line_states_how_many_categories_it_left_out(db):
    user = default_user(db)
    categories = list(db.scalars(select(Category).where(Category.scope == "system")))[:8]
    assert len(categories) == 8
    for index, category in enumerate(categories):
        _expense(db, user, amount=10_000 * (index + 1), at=_at(2026, 6, 10), category=category)
    db.flush()

    traits = compute_traits(db, user, TODAY)
    line = traits_context_line(traits)

    assert "+2 more" in line

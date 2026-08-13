from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Category, Subcategory, Transaction, TransactionCategoryHint, TransactionDraft, TransactionFieldValue, User
from app.event_time import from_local_parts
from app.services.recommendation import (
    HALF_LIFE_DAYS,
    amount_band,
    geohash_encode,
    load_ledger,
    recommend_categories,
    recommend_subcategories,
    time_bucket,
    tokenize,
)

TODAY = date(2026, 8, 12)
NOON = datetime(2026, 8, 12, 12, 30)


@pytest.fixture()
def user(db):
    record = User(email="recommend@example.com", display_name="Recommend", timezone="Asia/Kolkata")
    db.add(record)
    db.flush()
    return record


def category(db, slug: str) -> Category:
    return db.scalar(select(Category).where(Category.slug == slug))


def subcategory(db, category_slug: str, slug: str) -> Subcategory:
    parent = category(db, category_slug)
    return db.scalar(select(Subcategory).where(
        Subcategory.category_id == parent.id,
        Subcategory.slug == slug,
    ))


def expense_categories(db) -> list[Category]:
    return list(db.scalars(select(Category).where(Category.slug.not_in(("income", "investment")))))


def add_transaction(
    db,
    user: User,
    *,
    category_slug: str,
    subcategory_slug: str | None = None,
    days_ago: int = 1,
    amount_minor: int = 30_000,
    merchant: str | None = None,
    description: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    accuracy: int | None = None,
    time: str | None = None,
) -> Transaction:
    parent = category(db, category_slug)
    child = subcategory(db, category_slug, subcategory_slug) if subcategory_slug else None
    record = Transaction(
        user_id=user.id,
        transaction_type="expense",
        amount_minor=amount_minor,
        merchant_name=merchant,
        category_id=parent.id,
        subcategory_id=child.id if child else None,
        transaction_at=from_local_parts(TODAY - timedelta(days=days_ago), time, user.timezone),
        description=description,
        latitude=latitude,
        longitude=longitude,
        location_accuracy=accuracy,
    )
    db.add(record)
    db.flush()
    return record


def make_draft(
    db,
    user: User,
    *,
    raw_text: str = "spent 300",
    amount_minor: int | None = 30_000,
    merchant: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    accuracy: int | None = None,
    time: str | None = None,
) -> TransactionDraft:
    draft = TransactionDraft(
        user_id=user.id,
        conversation_id=None,
        raw_text=raw_text,
        description=raw_text,
        amount_minor=amount_minor,
        merchant_name=merchant,
        latitude=latitude,
        longitude=longitude,
        location_accuracy=accuracy,
        transaction_at=from_local_parts(TODAY, time, user.timezone),
    )
    return draft


def ledger_for(db, user: User):
    return load_ledger(db, user.id, reference=TODAY)


def test_explicit_transaction_hint_feeds_category_and_subcategory_recommendations(db, user):
    transport = category(db, "transport")
    cab = subcategory(db, "transport", "cab")
    db.add(TransactionCategoryHint(
        user_id=user.id,
        merchant_pattern="Blue Cab",
        normalized_pattern="blue cab",
        category_id=transport.id,
        subcategory_id=cab.id,
    ))
    db.flush()
    draft = make_draft(db, user, raw_text="paid Blue Cab", merchant="Blue Cab")
    ledger = ledger_for(db, user)

    category_result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger, now=NOON)
    assert category_result.top.id == str(transport.id)
    assert category_result.top.reasons[0] == "You set Blue Cab → Transport"

    subcategory_result = recommend_subcategories(db, user, draft, transport, [cab], ledger=ledger, now=NOON)
    assert subcategory_result.top.id == str(cab.id)


# --- primitives -------------------------------------------------------------


def test_geohash_matches_reference_encoding():
    # Canonical worked example: 57.64911, 10.40744 -> u4pruydqqvj
    assert geohash_encode(57.64911, 10.40744, 11) == "u4pruydqqvj"
    assert geohash_encode(57.64911, 10.40744, 5) == "u4pru"


def test_geohash_prefixes_agree_for_nearby_points():
    # ~40m apart: same 150m cell, therefore the same geohash-7 key.
    assert geohash_encode(12.9716, 77.5946, 7) == geohash_encode(12.97175, 77.59475, 7)


def test_amount_band_is_log_scaled():
    assert amount_band(20_000) == "b7"       # 200 -> [128, 256)
    assert amount_band(540_000) == "b12"     # 5400 -> [4096, 8192)
    assert amount_band(None) is None
    assert amount_band(0) is None


def test_time_bucket_separates_weekday_and_weekend():
    assert time_bucket(13, date(2026, 8, 12)) == "3:wd"   # Wednesday afternoon
    assert time_bucket(13, date(2026, 8, 15)) == "3:we"   # Saturday afternoon
    assert time_bucket(None, TODAY) is None


def test_tokenize_drops_stopwords_and_duplicates():
    assert tokenize("Paid 300 for coffee at Coffee House") == ["coffee", "house"]


# --- category recommendation ------------------------------------------------


def test_merchant_history_outranks_static_prior(db, user):
    # The static prior puts Food first for this amount; merchant evidence wins.
    for index in range(6):
        add_transaction(
            db, user,
            category_slug="shopping",
            merchant="Bluedart",
            days_ago=index * 5 + 1,
            amount_minor=30_000,
        )
    draft = make_draft(db, user, merchant="Bluedart")

    result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert result.top.slug == "shopping"
    assert result.top.evidence_backed
    assert "Bluedart → Shopping 6 of 6 times" in result.top.reasons


def test_location_history_drives_the_guess(db, user):
    for index in range(5):
        add_transaction(
            db, user,
            category_slug="health",
            days_ago=index * 3 + 1,
            latitude=12.9716,
            longitude=77.5946,
            accuracy=30,
        )
    draft = make_draft(db, user, latitude=12.97175, longitude=77.59475, accuracy=30)

    result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert result.top.slug == "health"
    assert any("here" in reason for reason in result.top.reasons)


def test_coarse_location_is_demoted_to_the_area_channel(db, user):
    for index in range(5):
        add_transaction(
            db, user,
            category_slug="health",
            days_ago=index * 3 + 1,
            latitude=12.9716,
            longitude=77.5946,
            accuracy=30,
        )
    # A 2km fix cannot identify a shop, so it must not claim "here".
    draft = make_draft(db, user, latitude=12.9716, longitude=77.5946, accuracy=2_000)

    result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert all("here" not in reason for item in result.suggestions for reason in item.reasons)


def test_recent_evidence_outweighs_older_evidence(db, user):
    # Equal counts, but one habit is current and the other is two half-lives old.
    for index in range(4):
        add_transaction(db, user, category_slug="entertainment", merchant="Same Place", days_ago=index + 1)
    for index in range(4):
        add_transaction(
            db, user,
            category_slug="bills",
            merchant="Same Place",
            days_ago=int(HALF_LIFE_DAYS * 2) + index,
        )
    draft = make_draft(db, user, merchant="Same Place")

    result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert result.top.slug == "entertainment"


def test_prior_only_candidates_are_suppressed_when_evidence_exists(db, user):
    for index in range(8):
        add_transaction(db, user, category_slug="bills", merchant="Landlord", days_ago=index * 4 + 1)
    draft = make_draft(db, user, merchant="Landlord", amount_minor=1_500_000)

    result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert all(item.evidence_backed for item in result.suggestions)
    assert result.suggestions[0].slug == "bills"


def test_second_slot_offers_a_different_explanation(db, user):
    # Merchant says Food; the amount band says Shopping. Both deserve a slot.
    for index in range(6):
        add_transaction(db, user, category_slug="food", merchant="Corner Store", days_ago=index * 2 + 1, amount_minor=20_000)
    for index in range(6):
        add_transaction(db, user, category_slug="shopping", days_ago=index * 2 + 2, amount_minor=800_000)
    draft = make_draft(db, user, merchant="Corner Store", amount_minor=800_000)

    result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert len(result.suggestions) >= 2
    assert result.suggestions[0].dominant_channel != result.suggestions[1].dominant_channel


def test_reasons_do_not_restate_the_merchant_as_a_token(db, user):
    # "Croma" is both the merchant and a token in the text; two reasons saying
    # the same thing would waste the card's second line.
    for index in range(9):
        add_transaction(
            db, user,
            category_slug="shopping",
            merchant="Croma",
            description="Bought a gadget at Croma",
            days_ago=index * 6 + 1,
            amount_minor=540_000,
        )
    draft = make_draft(db, user, raw_text="Spent 5400 at Croma", merchant="Croma", amount_minor=540_000)

    result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert result.top.reasons[0] == "Croma → Shopping 9 of 9 times"
    assert not any("“croma”" in reason for reason in result.top.reasons)


def test_cold_start_falls_back_to_honest_static_signals(db, user):
    draft = make_draft(db, user, raw_text="₹200 lunch", amount_minor=20_000)

    result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert len(result.suggestions) == 3
    assert result.top.slug == "food"
    # With no history, nothing may claim the user has done this before.
    assert not any("times" in reason for item in result.suggestions for reason in item.reasons)
    assert not any("Frequently used" in reason for item in result.suggestions for reason in item.reasons)


def test_confidence_gate_holds_back_on_thin_evidence(db, user):
    add_transaction(db, user, category_slug="food", merchant="New Spot", days_ago=1)
    draft = make_draft(db, user, merchant="New Spot")

    result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert not result.is_confident


def test_one_explicit_correction_is_enough_to_auto_apply(db, user):
    # A correction states an intent; a passively accepted category does not.
    # It must therefore clear the gate that three silent observations clear.
    record = add_transaction(
        db, user,
        category_slug="entertainment",
        merchant="Toit",
        description="Spent ₹900 at Toit today",
        days_ago=1,
    )
    db.add(TransactionFieldValue(
        transaction_id=record.id,
        field_name="category_id",
        value={"value": str(record.category_id)},
        origin="user_correction",
        user_confirmed=True,
    ))
    db.flush()
    draft = make_draft(db, user, raw_text="Paid ₹1,100 at Toit today", merchant="Toit")

    result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert result.top.slug == "entertainment"
    assert result.top.confirmed_support == 1
    assert result.is_confident
    assert "You set Toit → Entertainment" in result.top.reasons


def test_unconfirmed_single_observation_still_does_not_auto_apply(db, user):
    add_transaction(db, user, category_slug="entertainment", merchant="Toit", days_ago=1)
    draft = make_draft(db, user, merchant="Toit")

    result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert result.top.slug == "entertainment"
    assert result.top.confirmed_support == 0
    assert not result.is_confident


def test_display_suppression_cannot_inflate_confidence(db, user):
    # One weak observation leaves a single displayed card. Confidence must be
    # measured over every candidate, not over what survived the display filter.
    add_transaction(db, user, category_slug="food", merchant="Solo Sighting", days_ago=1)
    draft = make_draft(db, user, merchant="Solo Sighting")

    result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert len(result.suggestions) == 1
    assert len(result.population) > 1
    assert result.confidence < 1.0


def test_confidence_gate_fires_on_a_settled_habit(db, user):
    for index in range(12):
        add_transaction(db, user, category_slug="transport", merchant="Namma Metro", days_ago=index * 2 + 1)
    draft = make_draft(db, user, merchant="Namma Metro")

    result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert result.is_confident
    assert result.top.slug == "transport"


def test_scoring_is_deterministic(db, user):
    for index in range(5):
        add_transaction(db, user, category_slug="food", merchant="Tiffin Room", days_ago=index + 1)
    draft = make_draft(db, user, merchant="Tiffin Room")

    first = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)
    second = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert [(item.id, item.score, item.reasons) for item in first.suggestions] == \
           [(item.id, item.score, item.reasons) for item in second.suggestions]


def test_deleted_transactions_stop_counting(db, user):
    records = [
        add_transaction(db, user, category_slug="shopping", merchant="Refunded Shop", days_ago=index + 1)
        for index in range(5)
    ]
    for record in records:
        record.deleted_at = TODAY
    db.flush()
    draft = make_draft(db, user, merchant="Refunded Shop")

    result = recommend_categories(db, user, draft, expense_categories(db), ledger=ledger_for(db, user), now=NOON)

    assert not result.top.evidence_backed


# --- subcategory recommendation ---------------------------------------------


def test_subcategory_learns_within_its_category(db, user):
    for index in range(6):
        add_transaction(
            db, user,
            category_slug="food",
            subcategory_slug="groceries",
            merchant="Big Basket",
            days_ago=index * 3 + 1,
        )
    draft = make_draft(db, user, merchant="Big Basket")
    food = category(db, "food")
    options = list(db.scalars(select(Subcategory).where(Subcategory.category_id == food.id)))

    result = recommend_subcategories(db, user, draft, food, options, ledger=ledger_for(db, user), now=NOON)

    assert result.top.slug == "groceries"
    assert result.top.evidence_backed


def test_subcategory_evidence_does_not_leak_across_categories(db, user):
    for index in range(6):
        add_transaction(
            db, user,
            category_slug="food",
            subcategory_slug="delivery",
            merchant="Shared Name",
            days_ago=index * 3 + 1,
        )
    draft = make_draft(db, user, merchant="Shared Name")
    shopping = category(db, "shopping")
    options = list(db.scalars(select(Subcategory).where(Subcategory.category_id == shopping.id)))

    result = recommend_subcategories(db, user, draft, shopping, options, ledger=ledger_for(db, user), now=NOON)

    assert not any(item.evidence_backed for item in result.suggestions)

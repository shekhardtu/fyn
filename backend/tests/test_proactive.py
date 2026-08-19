"""Proactive insights: deterministic detectors and verification on read.

Every test below pins one of the two promises this lane makes. The detectors
are arithmetic, so each one is checked both for firing with the exact numbers
and for staying quiet when its stated threshold is not met — a detector that
only ever fires is a detector nobody can trust to be silent. And a stored claim
is a dated claim, so the second half pins that it is replayed before it is ever
shown again, and disappears from the surface the moment it stops holding.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import current_user, router
from app.config import DEFAULT_TIMEZONE
from app.database import get_db
from app.event_time import local_now
from app.models import Category, FinancialInsight, Transaction, User
from app.seed import default_user
from app.services import conversation as conversation_service
from app.config import get_settings
from app.services.cdp import compute_traits
from app.services.conversation import get_or_create_conversation, handle_chat
from app.services.proactive import (
    ANOMALY_MIN_DELTA_MINOR,
    KIND_CATEGORY_ANOMALY,
    KIND_INCOME_GAP,
    KIND_UPCOMING_OBLIGATION,
    current_insights,
    generate_insights,
    insights_context_line,
    store_insights,
    verify_insight,
)

TODAY = date(2026, 8, 16)


def _second_user(db) -> User:
    user = User(email="other@example.com", display_name="Other")
    db.add(user)
    db.flush()
    return user


def _at(year: int, month: int, day: int, hour: int = 9) -> datetime:
    """A mid-morning UTC instant, far from any Asia/Kolkata date boundary."""
    return datetime(year, month, day, hour, 0, tzinfo=timezone.utc)


def _on(day: date, hour: int = 9) -> datetime:
    return _at(day.year, day.month, day.day, hour)


def _expense(db, user, *, amount: int, at: datetime, category=None, merchant=None) -> Transaction:
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


def _income(db, user, *, at: datetime, amount: int = 300_000) -> Transaction:
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


def _category(db, slug: str) -> Category:
    return db.scalar(select(Category).where(Category.slug == slug, Category.scope == "system"))


def _seed_category_baseline(db, user, category, *, per_month: int) -> None:
    """One complete-month expense in each of the three baseline months."""
    for month in (5, 6, 7):
        _expense(db, user, amount=per_month, at=_at(2026, month, 10), category=category)


def _seed_monthly_income(db, user, *, last_on: date, gap_days: int = 30, count: int = 3) -> None:
    """`count` income records ending at `last_on`, evenly spaced by `gap_days`."""
    for step in reversed(range(count)):
        _income(db, user, at=_on(last_on - timedelta(days=gap_days * step)))


def _of_kind(insights, kind):
    return [item for item in insights if item.kind == kind]


def _values(insight) -> list[int]:
    return [int(row["value"]) for row in insight.evidence["rows"]]


def _application(db, user):
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: (yield db)
    application.dependency_overrides[current_user] = lambda: user
    return application


# --- category_anomaly ---------------------------------------------------------

def test_category_anomaly_fires_with_the_exact_month_to_date_arithmetic(db):
    user = default_user(db)
    food = _category(db, "food")
    _seed_category_baseline(db, user, food, per_month=100_000)
    _expense(db, user, amount=120_000, at=_at(2026, 8, 3), category=food)
    _expense(db, user, amount=80_000, at=_at(2026, 8, 14), category=food)
    db.flush()

    insight = _of_kind(generate_insights(db, user, TODAY), KIND_CATEGORY_ANOMALY)[0]

    assert insight.subject == "food"
    # Month to date, the three-complete-month mean, and the excess between them.
    assert _values(insight) == [200_000, 100_000, 100_000]
    assert insight.headline == "Food is running 2.0x its usual month: ₹2,000 so far against ₹1,000"


def test_category_anomaly_stays_quiet_below_the_stated_ratio(db):
    """A large category that is merely 1.2x its usual month is not news."""
    user = default_user(db)
    food = _category(db, "food")
    _seed_category_baseline(db, user, food, per_month=1_000_000)
    _expense(db, user, amount=1_200_000, at=_at(2026, 8, 3), category=food)
    db.flush()

    insights = generate_insights(db, user, TODAY)

    # The absolute excess is 200_000 — far past the floor — so only the ratio
    # rule is keeping this quiet.
    assert _of_kind(insights, KIND_CATEGORY_ANOMALY) == []


def test_category_anomaly_stays_quiet_when_the_excess_is_trivial(db):
    """Tripling a tiny category is arithmetic, not a finding."""
    user = default_user(db)
    food = _category(db, "food")
    _seed_category_baseline(db, user, food, per_month=20_000)
    _expense(db, user, amount=60_000, at=_at(2026, 8, 3), category=food)
    db.flush()

    insights = generate_insights(db, user, TODAY)

    # 3x the baseline, so the ratio rule passes; the 40_000 excess is under the
    # stated floor, which is the only thing keeping this quiet.
    assert 60_000 - 20_000 < ANOMALY_MIN_DELTA_MINOR
    assert _of_kind(insights, KIND_CATEGORY_ANOMALY) == []


def test_a_category_with_no_baseline_produces_no_claim(db):
    """Spend in a brand-new category has no usual month to exceed."""
    user = default_user(db)
    food = _category(db, "food")
    _expense(db, user, amount=900_000, at=_at(2026, 8, 3), category=food)
    db.flush()

    assert _of_kind(generate_insights(db, user, TODAY), KIND_CATEGORY_ANOMALY) == []


def test_category_anomaly_counts_only_the_month_in_progress(db):
    """Last month's spike must not be re-announced as this month's."""
    user = default_user(db)
    food = _category(db, "food")
    _seed_category_baseline(db, user, food, per_month=100_000)
    _expense(db, user, amount=900_000, at=_at(2026, 7, 20), category=food)
    db.flush()

    assert _of_kind(generate_insights(db, user, TODAY), KIND_CATEGORY_ANOMALY) == []


# --- upcoming_obligation ------------------------------------------------------

def test_upcoming_obligation_projects_the_next_monthly_charge(db):
    user = default_user(db)
    _expense(db, user, amount=49_900, at=_at(2026, 6, 25), merchant="Netflix")
    _expense(db, user, amount=49_900, at=_at(2026, 7, 25), merchant="Netflix")
    db.flush()

    insight = _of_kind(generate_insights(db, user, TODAY), KIND_UPCOMING_OBLIGATION)[0]

    # Amount, days until the projected date, and how many charges were observed.
    assert _values(insight) == [49_900, 9, 2]
    assert insight.evidence["labels"]["dueOn"] == "2026-08-25"
    assert insight.headline == "Netflix takes ₹499 on 2026-08-25 (in 9 days)"


def test_upcoming_obligation_projects_the_next_weekly_charge(db):
    user = default_user(db)
    _expense(db, user, amount=20_000, at=_at(2026, 8, 5), merchant="Metro Card")
    _expense(db, user, amount=20_000, at=_at(2026, 8, 12), merchant="Metro Card")
    db.flush()

    insight = _of_kind(generate_insights(db, user, TODAY), KIND_UPCOMING_OBLIGATION)[0]

    assert insight.evidence["labels"] == {
        "merchant": "Metro Card",
        "cadence": "weekly",
        "dueOn": "2026-08-19",
    }
    assert _values(insight) == [20_000, 3, 2]


def test_upcoming_obligation_stays_quiet_beyond_the_horizon(db):
    user = default_user(db)
    _expense(db, user, amount=49_900, at=_at(2026, 7, 1), merchant="Netflix")
    _expense(db, user, amount=49_900, at=_at(2026, 8, 1), merchant="Netflix")
    db.flush()

    # Due 2026-09-01: sixteen days out, past the fourteen-day horizon.
    assert _of_kind(generate_insights(db, user, TODAY), KIND_UPCOMING_OBLIGATION) == []


def test_upcoming_obligation_ignores_a_charge_that_is_not_recurring(db):
    user = default_user(db)
    _expense(db, user, amount=49_900, at=_at(2026, 8, 10), merchant="Netflix")
    db.flush()

    assert _of_kind(generate_insights(db, user, TODAY), KIND_UPCOMING_OBLIGATION) == []


# --- income_gap ---------------------------------------------------------------

def test_income_gap_fires_when_a_monthly_cadence_misses_its_window(db):
    user = default_user(db)
    _seed_monthly_income(db, user, last_on=date(2026, 6, 30))
    db.flush()

    insight = _of_kind(generate_insights(db, user, TODAY), KIND_INCOME_GAP)[0]

    # Expected by 2026-07-30 (last + median gap), so seventeen days overdue.
    assert _values(insight) == [17, 30, 300_000]
    assert insight.evidence["labels"] == {"lastOn": "2026-06-30", "expectedBy": "2026-07-30"}
    assert insight.headline == (
        "Income is 17 days late: the last ₹3,000 landed on 2026-06-30, "
        "and the monthly cadence expected the next by 2026-07-30"
    )


def test_income_gap_stays_quiet_inside_the_grace_window(db):
    user = default_user(db)
    _seed_monthly_income(db, user, last_on=date(2026, 7, 16), count=2)
    db.flush()

    # Expected by 2026-08-15 and today is the 16th: one day, inside the grace.
    assert _of_kind(generate_insights(db, user, TODAY), KIND_INCOME_GAP) == []


def test_income_gap_says_nothing_about_an_irregular_earner(db):
    user = default_user(db)
    _income(db, user, at=_at(2026, 6, 1))
    _income(db, user, at=_at(2026, 6, 8))
    _income(db, user, at=_at(2026, 6, 15))
    db.flush()

    # A seven-day median gap is not a monthly cadence, so there is no expected
    # window to have missed.
    assert _of_kind(generate_insights(db, user, TODAY), KIND_INCOME_GAP) == []


def test_a_user_with_no_data_gets_no_insights_and_no_empty_line(db):
    user = default_user(db)

    assert generate_insights(db, user, TODAY) == []
    assert insights_context_line([]) == ""


# --- lineage and the recompute key --------------------------------------------

def test_every_insight_carries_its_lineage_and_the_key_that_reproduces_it(db):
    user = default_user(db)
    food = _category(db, "food")
    _seed_category_baseline(db, user, food, per_month=100_000)
    _expense(db, user, amount=200_000, at=_at(2026, 8, 3), category=food)
    _expense(db, user, amount=49_900, at=_at(2026, 6, 25), merchant="Netflix")
    _expense(db, user, amount=49_900, at=_at(2026, 7, 25), merchant="Netflix")
    _seed_monthly_income(db, user, last_on=date(2026, 6, 30))
    db.flush()
    traits_computed_at = max(trait.computed_at for trait in compute_traits(db, user, TODAY))

    insights = generate_insights(db, user, TODAY)

    assert {item.kind for item in insights} == {
        KIND_CATEGORY_ANOMALY,
        KIND_UPCOMING_OBLIGATION,
        KIND_INCOME_GAP,
    }
    for insight in insights:
        assert set(insight.lineage) == {"manifestHash", "traitsComputedAt", "computedAt"}
        assert insight.lineage["manifestHash"]
        # The traits stamp is the snapshot the whole set was computed from, not
        # a fresh clock reading dressed up as provenance.
        assert insight.lineage["traitsComputedAt"] >= traits_computed_at.isoformat()
        assert insight.lineage["computedAt"]
        assert insight.recompute_key["detector"] == insight.kind
        assert insight.recompute_key["subject"] == insight.subject
        assert insight.recompute_key["asOf"] == TODAY.isoformat()
        # The key has to survive storage, so every parameter is JSON-native.
        assert all(
            isinstance(value, (str, int, bool)) for value in insight.recompute_key.values()
        )


def test_the_stated_thresholds_travel_inside_the_key_that_used_them(db):
    user = default_user(db)
    food = _category(db, "food")
    _seed_category_baseline(db, user, food, per_month=100_000)
    _expense(db, user, amount=200_000, at=_at(2026, 8, 3), category=food)
    db.flush()

    key = _of_kind(generate_insights(db, user, TODAY), KIND_CATEGORY_ANOMALY)[0].recompute_key

    # A reader can see which threshold was applied, not merely that one was.
    assert key["ratioThreshold"] == "1.5"
    assert key["minDeltaMinor"] == ANOMALY_MIN_DELTA_MINOR
    assert key["baselineMeanMinor"] == 100_000


# --- verification -------------------------------------------------------------

def test_verify_insight_holds_while_the_data_is_unchanged(db):
    user = default_user(db)
    food = _category(db, "food")
    _seed_category_baseline(db, user, food, per_month=100_000)
    _expense(db, user, amount=200_000, at=_at(2026, 8, 3), category=food)
    db.flush()

    insight = _of_kind(generate_insights(db, user, TODAY), KIND_CATEGORY_ANOMALY)[0]

    assert verify_insight(db, user, insight) is True


def test_verify_insight_fails_once_the_underlying_transaction_changes(db):
    user = default_user(db)
    food = _category(db, "food")
    _seed_category_baseline(db, user, food, per_month=100_000)
    spike = _expense(db, user, amount=200_000, at=_at(2026, 8, 3), category=food)
    db.flush()
    insight = _of_kind(generate_insights(db, user, TODAY), KIND_CATEGORY_ANOMALY)[0]
    assert verify_insight(db, user, insight) is True

    spike.amount_minor = 110_000
    db.flush()

    # 1.1x the baseline no longer clears the ratio, so the claim is simply
    # false — not "approximately true".
    assert verify_insight(db, user, insight) is False


def test_verify_insight_fails_when_the_numbers_move_but_the_claim_still_fires(db):
    """A claim quotes exact numbers, so a bigger spike falsifies it too."""
    user = default_user(db)
    food = _category(db, "food")
    _seed_category_baseline(db, user, food, per_month=100_000)
    _expense(db, user, amount=200_000, at=_at(2026, 8, 3), category=food)
    db.flush()
    insight = _of_kind(generate_insights(db, user, TODAY), KIND_CATEGORY_ANOMALY)[0]

    _expense(db, user, amount=50_000, at=_at(2026, 8, 4), category=food)
    db.flush()

    assert verify_insight(db, user, insight) is False


def test_verify_insight_fails_when_a_recurring_pattern_stops(db):
    user = default_user(db)
    first = _expense(db, user, amount=49_900, at=_at(2026, 6, 25), merchant="Netflix")
    _expense(db, user, amount=49_900, at=_at(2026, 7, 25), merchant="Netflix")
    db.flush()
    insight = _of_kind(generate_insights(db, user, TODAY), KIND_UPCOMING_OBLIGATION)[0]

    first.deleted_at = datetime(2026, 8, 15, tzinfo=timezone.utc)
    db.flush()

    # One charge is not a pattern, so nothing is due.
    assert verify_insight(db, user, insight) is False


def test_verify_insight_fails_once_the_missing_income_arrives(db):
    user = default_user(db)
    _seed_monthly_income(db, user, last_on=date(2026, 6, 30))
    db.flush()
    insight = _of_kind(generate_insights(db, user, TODAY), KIND_INCOME_GAP)[0]

    _income(db, user, at=_at(2026, 8, 14))
    db.flush()

    assert verify_insight(db, user, insight) is False


def test_verify_insight_refuses_another_users_row(db):
    user = default_user(db)
    stranger = _second_user(db)
    _seed_monthly_income(db, user, last_on=date(2026, 6, 30))
    db.flush()
    stored = store_insights(db, user, generate_insights(db, user, TODAY))[0]

    with pytest.raises(ValueError) as excinfo:
        verify_insight(db, stranger, stored)

    assert str(excinfo.value).startswith("insight_tenant_mismatch:")


def test_verify_insight_refuses_a_kind_no_detector_owns(db):
    user = default_user(db)
    orphan = FinancialInsight(
        user_id=user.id,
        insight_type="hunch",
        subject="vibes",
        title="Something feels off",
        evidence={"rows": [], "labels": {}},
        recompute_key={"detector": "hunch"},
        lineage={},
    )
    db.add(orphan)
    db.flush()

    with pytest.raises(ValueError) as excinfo:
        verify_insight(db, user, orphan)

    assert str(excinfo.value).startswith("unknown_insight_kind:")


# --- storage ------------------------------------------------------------------

def test_restating_a_claim_rewrites_one_row_instead_of_appending(db):
    user = default_user(db)
    _seed_monthly_income(db, user, last_on=date(2026, 6, 30))
    db.flush()

    store_insights(db, user, generate_insights(db, user, TODAY))
    store_insights(db, user, generate_insights(db, user, date(2026, 8, 17)))

    rows = list(db.scalars(select(FinancialInsight).where(FinancialInsight.user_id == user.id)))
    assert len(rows) == 1
    # The restatement is the later one: a day passed, so the claim moved with it.
    assert rows[0].recompute_key["asOf"] == "2026-08-17"
    assert "18 days late" in rows[0].title


def test_a_falsified_claim_is_marked_stale_and_kept_out_of_the_current_set(db):
    user = default_user(db)
    _seed_monthly_income(db, user, last_on=date(2026, 6, 30))
    db.flush()
    assert len(current_insights(db, user, TODAY)) == 1

    _income(db, user, at=_at(2026, 8, 14))
    db.flush()

    assert current_insights(db, user, TODAY) == []
    # The row survives as history, stamped with the moment it stopped holding.
    stored = db.scalar(select(FinancialInsight).where(FinancialInsight.user_id == user.id))
    assert stored.stale_at is not None
    assert stored.verified_at is not None and stored.verified_at < stored.stale_at


def test_a_stale_claim_that_becomes_true_again_is_current_again(db):
    user = default_user(db)
    _seed_monthly_income(db, user, last_on=date(2026, 6, 30))
    db.flush()
    current_insights(db, user, TODAY)
    late = _income(db, user, at=_at(2026, 8, 14))
    db.flush()
    assert current_insights(db, user, TODAY) == []

    late.deleted_at = datetime(2026, 8, 15, tzinfo=timezone.utc)
    db.flush()

    revived = current_insights(db, user, TODAY)
    assert [row.insight_type for row in revived] == [KIND_INCOME_GAP]
    assert revived[0].stale_at is None


def test_a_dismissed_claim_is_never_returned(db):
    user = default_user(db)
    _seed_monthly_income(db, user, last_on=date(2026, 6, 30))
    db.flush()
    stored = current_insights(db, user, TODAY)[0]

    stored.dismissed_at = datetime(2026, 8, 16, tzinfo=timezone.utc)
    db.flush()

    assert current_insights(db, user, TODAY) == []


def test_insights_are_generated_from_one_users_data_only(db):
    user = default_user(db)
    stranger = _second_user(db)
    _seed_monthly_income(db, user, last_on=date(2026, 6, 30))
    _expense(db, stranger, amount=49_900, at=_at(2026, 6, 25), merchant="Netflix")
    _expense(db, stranger, amount=49_900, at=_at(2026, 7, 25), merchant="Netflix")
    db.flush()

    mine = current_insights(db, user, TODAY)
    theirs = current_insights(db, stranger, TODAY)

    assert [row.insight_type for row in mine] == [KIND_INCOME_GAP]
    assert [row.insight_type for row in theirs] == [KIND_UPCOMING_OBLIGATION]
    assert {row.user_id for row in db.scalars(select(FinancialInsight))} == {user.id, stranger.id}


# --- the endpoint -------------------------------------------------------------

def _today() -> date:
    """The date the endpoint itself will resolve, so seeds stay clock-independent."""
    return local_now(DEFAULT_TIMEZONE).date()


def test_the_endpoint_returns_verified_insights_with_their_stamps(db):
    user = default_user(db)
    today = _today()
    _seed_monthly_income(db, user, last_on=today - timedelta(days=40))
    db.commit()

    with TestClient(_application(db, user)) as client:
        response = client.get("/api/insights")

    assert response.status_code == 200
    body = response.json()
    assert body["verifiedAt"].endswith("+00:00")
    assert len(body["insights"]) == 1
    insight = body["insights"][0]
    assert insight["kind"] == KIND_INCOME_GAP
    assert insight["subject"] == "income"
    assert insight["evidence"]["rows"][0] == {
        "label": "days overdue",
        "value": 10,
        "unit": "days",
        "currency": None,
        "on": (today - timedelta(days=10)).isoformat(),
    }
    assert set(insight["lineage"]) == {"manifestHash", "traitsComputedAt", "computedAt"}
    assert insight["lineage"]["manifestHash"]
    assert insight["lineage"]["computedAt"]
    assert insight["recomputeKey"]["detector"] == KIND_INCOME_GAP
    assert insight["recomputeKey"]["asOf"] == today.isoformat()
    assert insight["verifiedAt"] == body["verifiedAt"]


def test_the_endpoint_drops_an_insight_the_data_no_longer_supports(db):
    user = default_user(db)
    today = _today()
    _seed_monthly_income(db, user, last_on=today - timedelta(days=40))
    db.commit()

    with TestClient(_application(db, user)) as client:
        first = client.get("/api/insights")
        assert [item["kind"] for item in first.json()["insights"]] == [KIND_INCOME_GAP]

        _income(db, user, at=_on(today - timedelta(days=1)))
        db.commit()
        second = client.get("/api/insights")

    assert second.json()["insights"] == []
    # The pass still ran and still reported when — an empty list is a result,
    # not an absence of information.
    assert second.json()["verifiedAt"]
    stored = db.scalar(select(FinancialInsight).where(FinancialInsight.user_id == user.id))
    assert stored.stale_at is not None


def test_the_endpoint_never_shows_one_users_insight_to_another(db):
    user = default_user(db)
    stranger = _second_user(db)
    today = _today()
    _seed_monthly_income(db, user, last_on=today - timedelta(days=40))
    db.commit()

    with TestClient(_application(db, user)) as client:
        mine = client.get("/api/insights").json()
    with TestClient(_application(db, stranger)) as client:
        theirs = client.get("/api/insights").json()

    assert len(mine["insights"]) == 1
    assert theirs["insights"] == []


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


def test_the_operator_turn_carries_verified_insights_with_their_stamps(db, monkeypatch, agent_enabled):
    user = default_user(db)
    _seed_monthly_income(db, user, last_on=_today() - timedelta(days=40))
    db.commit()

    context = _captured_workflow_context(db, user, monkeypatch, "Share 3 recent transactions")

    line = context["verifiedInsights"]
    assert line.startswith(f"{KIND_INCOME_GAP}: Income is 10 days late")
    # The claim and the moment it was computed travel together, always.
    assert "computed_at" in line and "verified_at" in line


def test_the_operator_turn_omits_the_key_when_nothing_verifies(db, monkeypatch, agent_enabled):
    user = default_user(db)

    context = _captured_workflow_context(db, user, monkeypatch, "Share 3 recent transactions")

    assert context, "the operator turn was not reached"
    assert "verifiedInsights" not in context


def test_the_context_line_states_how_many_insights_it_left_out(db):
    user = default_user(db)
    today = _today()
    # Seven monthly patterns whose next charge lands inside the horizon: the
    # last sighting is 20-26 days back, so the projection is 2-11 days out.
    for index in range(7):
        _expense(db, user, amount=49_900, at=_on(today - timedelta(days=50 + index)), merchant=f"Shop {index}")
        _expense(db, user, amount=49_900, at=_on(today - timedelta(days=20 + index)), merchant=f"Shop {index}")
    db.flush()

    line = insights_context_line(current_insights(db, user, today))

    assert "+2 more" in line


# --- verification findings, pinned -------------------------------------------

def test_a_past_claim_does_not_verify_as_current(db):
    """The HIGH finding: a stored key replays its own historical window
    perfectly, so verification must additionally require the claim to be about
    today — otherwise a cancelled subscription's July obligation is still
    announced in August as five days away."""
    user = default_user(db)
    _expense(db, user, amount=49_900, at=_at(2026, 6, 25), merchant="Netflix")
    _expense(db, user, amount=49_900, at=_at(2026, 7, 25), merchant="Netflix")
    db.flush()

    stored = _of_kind(generate_insights(db, user, TODAY), KIND_UPCOMING_OBLIGATION)[0]
    store_insights(db, user, [stored])

    assert verify_insight(db, user, stored, today=TODAY) is True
    # Same claim, a month later: the sentence is about a date now in the past.
    assert verify_insight(db, user, stored, today=TODAY + timedelta(days=40)) is False

    later = current_insights(db, user, TODAY + timedelta(days=40))
    assert all(row.recompute_key.get("asOf") != TODAY.isoformat() for row in later)


def test_an_unrecognizable_row_is_marked_stale_not_raised(db):
    """One row a migration backfilled empty must not 500 the insights page and
    every chat turn that reads it."""
    user = default_user(db)
    db.add(FinancialInsight(
        user_id=user.id,
        insight_type="kind_that_no_longer_exists",
        subject="whatever",
        title="A claim from a removed detector",
        evidence={},
        recompute_key={},
        lineage={},
    ))
    db.flush()

    survivors = current_insights(db, user, TODAY)

    assert all(row.insight_type != "kind_that_no_longer_exists" for row in survivors)
    orphan = db.scalar(select(FinancialInsight).where(
        FinancialInsight.insight_type == "kind_that_no_longer_exists"
    ))
    assert orphan.stale_at is not None

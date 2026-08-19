from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import current_user, router
from app.database import get_db
from app.models import Account, Budget, Category, Subcategory, Transaction, User
from app.seed import DEFAULT_USER_EMAIL
from app.services.overview import overview_snapshot


def _transaction(user: User, *, amount: int, kind: str, at: str, category=None, subcategory=None, deleted=False):
    return Transaction(
        user_id=user.id,
        transaction_type=kind,
        amount_minor=amount,
        currency=user.currency,
        merchant_name="Test merchant",
        category_id=category.id if category else None,
        subcategory_id=subcategory.id if subcategory else None,
        transaction_at=datetime.fromisoformat(at).replace(tzinfo=timezone.utc),
        status="confirmed",
        deleted_at=datetime(2026, 8, 12, tzinfo=timezone.utc) if deleted else None,
    )


def test_overview_groups_expenses_by_category_and_subcategory(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    food = db.scalar(select(Category).where(Category.slug == "food"))
    delivery = db.scalar(select(Subcategory).where(Subcategory.category_id == food.id, Subcategory.slug == "delivery"))
    groceries = db.scalar(select(Subcategory).where(Subcategory.category_id == food.id, Subcategory.slug == "groceries"))
    transport = db.scalar(select(Category).where(Category.slug == "transport"))
    cab = db.scalar(select(Subcategory).where(Subcategory.category_id == transport.id, Subcategory.slug == "cab"))

    db.add_all([
        _transaction(user, amount=8_200_000, kind="income", at="2026-08-01T09:00:00", category=None),
        _transaction(user, amount=700_000, kind="expense", at="2026-08-04T09:00:00", category=food, subcategory=delivery),
        _transaction(user, amount=440_000, kind="expense", at="2026-08-05T09:00:00", category=food, subcategory=groceries),
        _transaction(user, amount=620_000, kind="expense", at="2026-08-06T09:00:00", category=transport, subcategory=cab),
        _transaction(user, amount=100_000, kind="expense", at="2026-08-07T09:00:00", category=None),
        _transaction(user, amount=999_999, kind="expense", at="2026-08-08T09:00:00", category=food, subcategory=delivery, deleted=True),
        _transaction(user, amount=1_400_000, kind="expense", at="2026-07-08T09:00:00", category=food, subcategory=delivery),
    ])
    db.commit()

    result = overview_snapshot(db, user.id, date(2026, 8, 1), date(2026, 8, 13))

    assert result["period"] == {
        "start": date(2026, 8, 1),
        "end": date(2026, 8, 13),
        "previous_start": date(2026, 7, 1),
        "previous_end": date(2026, 7, 13),
        "label": "August 2026",
        "is_current": True,
    }
    assert result["summary"] == {
        "currency": "INR",
        "income_minor": 8_200_000,
        "spent_minor": 1_860_000,
        "net_minor": 6_340_000,
        "expense_count": 4,
        "previous_spent_minor": 1_400_000,
        "change_minor": 460_000,
        "change_percent": 32.9,
    }
    assert [category["id"] for category in result["categories"]] == ["food", "transport", "uncategorized"]
    assert result["categories"][0] == {
        "id": "food",
        "label": "Food",
        "amount_minor": 1_140_000,
        "count": 2,
        "share_percent": 61.3,
        "subcategories": [
            {"id": "delivery", "label": "Delivery", "amount_minor": 700_000, "count": 1, "share_percent": 61.4},
            {"id": "groceries", "label": "Groceries", "amount_minor": 440_000, "count": 1, "share_percent": 38.6},
        ],
    }
    assert result["trend"][0] == {
        "day": 1,
        "date": date(2026, 8, 1),
        "income_minor": 8_200_000,
        "spent_minor": 0,
        "previous_income_minor": 0,
        "previous_spent_minor": 0,
    }
    assert result["trend"][6]["spent_minor"] == 100_000
    assert result["trend"][7]["previous_spent_minor"] == 1_400_000
    assert [item["merchant"] for item in result["recent_transactions"][:2]] == ["Test merchant", "Test merchant"]
    assert result["accounts"] == []


def test_overview_includes_user_owned_accounts_and_never_another_users(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    other = User(email="elsewhere@example.com", display_name="Elsewhere", currency="INR", timezone="Asia/Kolkata")
    db.add(other)
    db.flush()
    db.add_all([
        Account(user_id=user.id, name="Everyday", account_type="bank", institution="HDFC", mask="4321", balance_minor=345_600, currency="INR"),
        Account(user_id=other.id, name="Private", account_type="bank", balance_minor=999_999, currency="INR"),
    ])
    db.commit()

    result = overview_snapshot(db, user.id, date(2026, 8, 1), date(2026, 8, 13))

    assert result["accounts"] == [{
        "id": result["accounts"][0]["id"],
        "name": "Everyday",
        "account_type": "bank",
        "institution": "HDFC",
        "mask": "4321",
        "balance_minor": 345_600,
        "currency": "INR",
    }]


def test_overview_projects_overall_and_category_budgets_from_the_canonical_records(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    food = db.scalar(select(Category).where(Category.slug == "food"))
    other = User(email="budget-private@example.com", display_name="Private", currency="INR", timezone="Asia/Kolkata")
    db.add(other)
    db.flush()
    overall = Budget(
        user_id=user.id,
        category_id=None,
        name="Monthly spending budget",
        amount_minor=3_000_000,
        currency="INR",
        period="monthly",
    )
    food_budget = Budget(
        user_id=user.id,
        category_id=food.id,
        name="Food budget",
        amount_minor=1_000_000,
        currency="INR",
        period="monthly",
    )
    db.add_all([
        overall,
        food_budget,
        Budget(user_id=user.id, category_id=None, name="Annual plan", amount_minor=9_000_000, currency="INR", period="annual"),
        Budget(user_id=other.id, category_id=None, name="Private budget", amount_minor=99_000_000, currency="INR", period="monthly"),
        _transaction(user, amount=1_200_000, kind="expense", at="2026-08-05T09:00:00", category=food),
        _transaction(user, amount=300_000, kind="expense", at="2026-08-06T09:00:00"),
    ])
    db.commit()

    result = overview_snapshot(db, user.id, date(2026, 8, 1), date(2026, 8, 13))

    assert result["budgets"] == [
        {
            "id": overall.id,
            "name": "Monthly spending budget",
            "category_id": None,
            "category_slug": None,
            "category": None,
            "amount_minor": 3_000_000,
            "spent_minor": 1_500_000,
            "remaining_minor": 1_500_000,
            "over_minor": 0,
            "percent_used": 50.0,
            "currency": "INR",
            "period": "monthly",
        },
        {
            "id": food_budget.id,
            "name": "Food budget",
            "category_id": food.id,
            "category_slug": "food",
            "category": "Food",
            "amount_minor": 1_000_000,
            "spent_minor": 1_200_000,
            "remaining_minor": 0,
            "over_minor": 200_000,
            "percent_used": 120.0,
            "currency": "INR",
            "period": "monthly",
        },
    ]


def test_overview_trend_keeps_the_full_comparison_total_for_a_shorter_month(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    db.add_all([
        _transaction(user, amount=200_000, kind="expense", at="2026-01-31T09:00:00"),
        _transaction(user, amount=100_000, kind="expense", at="2026-02-28T09:00:00"),
    ])
    db.commit()

    result = overview_snapshot(db, user.id, date(2026, 2, 1), date(2026, 8, 13))

    assert len(result["trend"]) == 28
    assert result["trend"][-1]["spent_minor"] == 100_000
    assert result["trend"][-1]["previous_spent_minor"] == 200_000
    assert sum(point["previous_spent_minor"] for point in result["trend"]) == result["summary"]["previous_spent_minor"]


def test_overview_api_serializes_the_frontend_contract_and_rejects_future_months(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    food = db.scalar(select(Category).where(Category.slug == "food"))
    dining = db.scalar(select(Subcategory).where(Subcategory.category_id == food.id, Subcategory.slug == "dining"))
    db.add(_transaction(user, amount=125_000, kind="expense", at="2026-08-02T09:00:00", category=food, subcategory=dining))
    db.commit()

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: (yield db)
    application.dependency_overrides[current_user] = lambda: user
    monkeypatch.setattr("app.api.local_now", lambda _timezone: datetime(2026, 8, 13, tzinfo=timezone.utc))

    with TestClient(application) as client:
        response = client.get("/api/overview", params={"month": "2026-08-01"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["summary"]["spentMinor"] == 125_000
        assert payload["categories"][0]["subcategories"][0]["label"] == "Dining"
        assert payload["period"]["previousEnd"] == "2026-07-13"
        assert len(payload["trend"]) == 13
        assert payload["recentTransactions"][0]["merchant"] == "Test merchant"
        assert payload["accounts"] == []

        future = client.get("/api/overview", params={"month": "2026-09-01"})
        assert future.status_code == 422
        assert future.json()["detail"] == "Overview month cannot be in the future"

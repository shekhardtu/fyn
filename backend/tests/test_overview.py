from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import current_user, router
from app.database import get_db
from app.models import Category, Subcategory, Transaction, User
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

        future = client.get("/api/overview", params={"month": "2026-09-01"})
        assert future.status_code == 422
        assert future.json()["detail"] == "Overview month cannot be in the future"

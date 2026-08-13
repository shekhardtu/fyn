from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import current_user, router
from app.database import get_db
from app.models import Category, Subcategory, Transaction, TransactionFieldValue, User
from app.seed import DEFAULT_USER_EMAIL


def _application(db, user):
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: (yield db)
    application.dependency_overrides[current_user] = lambda: user
    return application


def test_money_page_endpoints_share_taxonomy_and_transaction_truth(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    food = db.scalar(select(Category).where(Category.slug == "food"))
    delivery = db.scalar(select(Subcategory).where(Subcategory.category_id == food.id, Subcategory.slug == "delivery"))
    transport = db.scalar(select(Category).where(Category.slug == "transport"))
    cab = db.scalar(select(Subcategory).where(Subcategory.category_id == transport.id, Subcategory.slug == "cab"))
    transaction = Transaction(
        user_id=user.id,
        transaction_type="expense",
        amount_minor=54_000,
        currency="INR",
        merchant_name="Swiggy",
        category_id=food.id,
        subcategory_id=delivery.id,
        transaction_at=datetime(2026, 8, 13, 8, 30, tzinfo=timezone.utc),
        spend_nature="discretionary",
        status="confirmed",
    )
    db.add(transaction)
    db.commit()

    with TestClient(_application(db, user)) as client:
        categories = client.get("/api/categories")
        assert categories.status_code == 200
        food_payload = next(item for item in categories.json() if item["slug"] == "food")
        assert food_payload["label"] == "Food"
        assert "Delivery" in {item["label"] for item in food_payload["subcategories"]}

        recent = client.get("/api/transactions", params={"limit": 20})
        assert recent.status_code == 200
        assert recent.json()[0] == {
            "id": str(transaction.id),
            "transactionType": "expense",
            "amountMinor": 54_000,
            "currency": "INR",
            "merchant": "Swiggy",
            "transactionAt": "2026-08-13T08:30:00Z",
            "status": "confirmed",
            "categoryId": str(food.id),
            "category": "Food",
            "subcategoryId": str(delivery.id),
            "subcategory": "Delivery",
            "spendNature": "discretionary",
            "location": None,
            "sourceCount": 0,
        }

        updated = client.patch(f"/api/transactions/{transaction.id}", json={
            "amountMinor": 72_500,
            "merchant": "Uber",
            "transactionAt": "2026-08-13T10:00:00Z",
            "transactionType": "expense",
            "categoryId": str(transport.id),
            "subcategoryId": str(cab.id),
            "spendNature": "essential",
            "location": "Bengaluru",
        })
        assert updated.status_code == 200
        assert updated.json()["amountMinor"] == 72_500
        assert updated.json()["category"] == "Transport"
        assert updated.json()["subcategory"] == "Cab"
        assert updated.json()["location"] == "Bengaluru"

        created = client.post("/api/transactions", json={
            "amountMinor": 3_250,
            "merchant": "Namma Metro",
            "transactionAt": "2026-08-13T11:00:00Z",
            "transactionType": "expense",
            "categoryId": str(transport.id),
            "subcategoryId": None,
            "spendNature": "essential",
            "location": "Bengaluru",
        })
        assert created.status_code == 201
        assert created.json()["merchant"] == "Namma Metro"
        assert created.json()["category"] == "Transport"
        created_id = UUID(created.json()["id"])

        searched = client.get("/api/transactions", params={"q": "metro", "transaction_type": "expense"})
        assert searched.status_code == 200
        assert [item["id"] for item in searched.json()] == [str(created_id)]

        second_page = client.get("/api/transactions", params={"limit": 1, "offset": 1})
        assert second_page.status_code == 200
        assert second_page.json()[0]["id"] == str(transaction.id)

    db.refresh(transaction)
    assert transaction.merchant_name == "Uber"
    assert transaction.category_id == transport.id
    assert transaction.subcategory_id == cab.id
    assert db.scalar(select(func.count()).select_from(TransactionFieldValue).where(TransactionFieldValue.transaction_id == transaction.id)) == 8
    assert db.scalar(select(func.count()).select_from(TransactionFieldValue).where(TransactionFieldValue.transaction_id == created_id)) == 8
    assert set(db.scalars(select(TransactionFieldValue.origin).where(TransactionFieldValue.transaction_id == created_id))) == {"manual_entry"}


def test_transaction_page_edit_cannot_cross_the_user_boundary(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    other = User(email="money-page-other@example.com", display_name="Other")
    db.add(other)
    db.flush()
    transaction = Transaction(
        user_id=other.id,
        transaction_type="expense",
        amount_minor=10_000,
        currency="INR",
        transaction_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        status="confirmed",
    )
    db.add(transaction)
    db.commit()

    with TestClient(_application(db, user)) as client:
        response = client.patch(f"/api/transactions/{transaction.id}", json={
            "amountMinor": 20_000,
            "merchant": "Should not change",
            "transactionAt": "2026-08-13T00:00:00Z",
            "transactionType": "expense",
            "categoryId": None,
            "subcategoryId": None,
            "spendNature": "unknown",
            "location": None,
        })
        assert response.status_code == 404
        assert response.json()["detail"] == "Unknown transaction"

    db.refresh(transaction)
    assert transaction.amount_minor == 10_000


def test_creating_taxonomy_from_the_editor_is_user_scoped_and_idempotent_by_name(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))

    with TestClient(_application(db, user)) as client:
        created = client.post("/api/categories", json={"name": "  Pets  "})
        assert created.status_code == 200
        payload = created.json()
        assert payload["label"] == "Pets"
        assert [item["label"] for item in payload["subcategories"]] == ["Other"]

        # Same name, different case: the existing entry comes back, no twin row.
        again = client.post("/api/categories", json={"name": "pets"})
        assert again.status_code == 200
        assert again.json()["id"] == payload["id"]

        directory = client.get("/api/categories").json()
        pets_entries = [item for item in directory if item["label"] == "Pets"]
        assert [item["id"] for item in pets_entries] == [payload["id"]]

        subcategory = client.post(f"/api/categories/{payload['id']}/subcategories", json={"name": "Vet visits"})
        assert subcategory.status_code == 200
        assert subcategory.json()["label"] == "Vet visits"
        duplicate = client.post(f"/api/categories/{payload['id']}/subcategories", json={"name": "vet VISITS"})
        assert duplicate.json()["id"] == subcategory.json()["id"]

    category = db.get(Category, UUID(payload["id"]))
    assert category.scope == "user"
    assert category.owner_user_id == user.id


def test_taxonomy_creation_rejects_blank_names_and_invisible_parents(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    other = User(email="taxonomy-other@example.com", display_name="Other")
    db.add(other)
    db.flush()
    foreign = Category(slug="custom-foreign", name="Foreign", icon="circle-ellipsis", scope="user", owner_user_id=other.id)
    income = db.scalar(select(Category).where(Category.slug == "income"))
    db.add(foreign)
    db.commit()

    with TestClient(_application(db, user)) as client:
        assert client.post("/api/categories", json={"name": "   "}).status_code == 422
        assert client.post(f"/api/categories/{foreign.id}/subcategories", json={"name": "Hidden"}).status_code == 404
        # Income is not an expense category, so the editor cannot grow it.
        assert client.post(f"/api/categories/{income.id}/subcategories", json={"name": "Bonus"}).status_code == 404


def test_category_page_crud_manages_custom_taxonomy_and_transaction_hints(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    food = db.scalar(select(Category).where(Category.slug == "food"))

    with TestClient(_application(db, user)) as client:
        created = client.post("/api/categories", json={"name": "Pets"})
        assert created.status_code == 200
        category_id = created.json()["id"]
        assert created.json()["editable"] is True
        assert created.json()["subcategories"][0]["editable"] is True

        renamed = client.patch(f"/api/categories/{category_id}", json={"name": "Pet care"})
        assert renamed.status_code == 200
        assert renamed.json()["label"] == "Pet care"

        subcategory = client.post(f"/api/categories/{category_id}/subcategories", json={"name": "Vet"})
        subcategory_id = subcategory.json()["id"]
        renamed_subcategory = client.patch(
            f"/api/categories/{category_id}/subcategories/{subcategory_id}",
            json={"name": "Vet visits"},
        )
        assert renamed_subcategory.status_code == 200
        assert renamed_subcategory.json()["label"] == "Vet visits"

        hint = client.post(f"/api/categories/{category_id}/hints", json={
            "merchant": "  Cessna Pets  ",
            "subcategoryId": subcategory_id,
        })
        assert hint.status_code == 201
        hint_id = hint.json()["id"]
        assert hint.json()["subcategory"] == "Vet visits"

        updated_hint = client.patch(f"/api/categories/{category_id}/hints/{hint_id}", json={
            "merchant": "Cessna Pet Clinic",
            "subcategoryId": subcategory_id,
        })
        assert updated_hint.status_code == 200
        assert updated_hint.json()["merchant"] == "Cessna Pet Clinic"

        directory = client.get("/api/categories").json()
        managed = next(item for item in directory if item["id"] == category_id)
        assert managed["hints"] == [updated_hint.json()]

        # The system taxonomy is shared reference data, never mutated by one user.
        assert client.patch(f"/api/categories/{food.id}", json={"name": "Meals"}).status_code == 403
        assert client.delete(f"/api/categories/{food.id}").status_code == 403

        assert client.delete(f"/api/categories/{category_id}/hints/{hint_id}").status_code == 204
        assert client.delete(f"/api/categories/{category_id}/subcategories/{subcategory_id}").status_code == 204
        assert client.delete(f"/api/categories/{category_id}").status_code == 204
        assert category_id not in {item["id"] for item in client.get("/api/categories").json()}


def test_category_page_refuses_to_delete_taxonomy_that_financial_records_use(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))

    with TestClient(_application(db, user)) as client:
        category = client.post("/api/categories", json={"name": "Home project"}).json()
        recorded = client.post("/api/transactions", json={
            "amountMinor": 25_000,
            "merchant": "Local hardware",
            "transactionAt": "2026-08-13T11:00:00Z",
            "transactionType": "expense",
            "categoryId": category["id"],
            "subcategoryId": None,
            "spendNature": "essential",
            "location": None,
        })
        assert recorded.status_code == 201
        deletion = client.delete(f"/api/categories/{category['id']}")
        assert deletion.status_code == 409
        assert "reassign" in deletion.json()["detail"]

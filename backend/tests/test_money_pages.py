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
    travel = db.scalar(select(Category).where(Category.slug == "travel"))
    local_transport = db.scalar(select(Subcategory).where(Subcategory.category_id == travel.id, Subcategory.slug == "local_transport"))
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
            "deletedAt": None,
        }

        updated = client.patch(f"/api/transactions/{transaction.id}", json={
            "amountMinor": 72_500,
            "merchant": "Uber",
            "transactionAt": "2026-08-13T10:00:00Z",
            "transactionType": "expense",
            "categoryId": str(travel.id),
            "subcategoryId": str(local_transport.id),
            "spendNature": "essential",
            "location": "Bengaluru",
        })
        assert updated.status_code == 200
        assert updated.json()["amountMinor"] == 72_500
        assert updated.json()["category"] == "Travel"
        assert updated.json()["subcategory"] == "Local transport"
        assert updated.json()["location"] == "Bengaluru"

        created = client.post("/api/transactions", json={
            "amountMinor": 3_250,
            "merchant": "Namma Metro",
            "transactionAt": "2026-08-13T11:00:00Z",
            "transactionType": "expense",
            "categoryId": str(travel.id),
            "subcategoryId": None,
            "spendNature": "essential",
            "location": "Bengaluru",
        })
        assert created.status_code == 201
        assert created.json()["merchant"] == "Namma Metro"
        assert created.json()["category"] == "Travel"
        created_id = UUID(created.json()["id"])

        searched = client.get("/api/transactions", params={"q": "metro", "transaction_type": "expense"})
        assert searched.status_code == 200
        assert [item["id"] for item in searched.json()] == [str(created_id)]

        second_page = client.get("/api/transactions", params={"limit": 1, "offset": 1})
        assert second_page.status_code == 200
        assert second_page.json()[0]["id"] == str(transaction.id)

    db.refresh(transaction)
    assert transaction.merchant_name == "Uber"
    assert transaction.category_id == travel.id
    assert transaction.subcategory_id == local_transport.id
    assert db.scalar(select(func.count()).select_from(TransactionFieldValue).where(TransactionFieldValue.transaction_id == transaction.id)) == 8
    assert db.scalar(select(func.count()).select_from(TransactionFieldValue).where(TransactionFieldValue.transaction_id == created_id)) == 8
    assert set(db.scalars(select(TransactionFieldValue.origin).where(TransactionFieldValue.transaction_id == created_id))) == {"manual_entry"}


def test_removed_transactions_stay_in_the_log_but_out_of_the_totals(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    food = db.scalar(select(Category).where(Category.slug == "food"))
    kept = Transaction(
        user_id=user.id,
        transaction_type="expense",
        amount_minor=30_000,
        currency="INR",
        merchant_name="BigBasket",
        category_id=food.id,
        transaction_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        spend_nature="essential",
        status="confirmed",
    )
    removed = Transaction(
        user_id=user.id,
        transaction_type="expense",
        amount_minor=50_000,
        currency="INR",
        merchant_name="Groceries",
        category_id=food.id,
        transaction_at=datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc),
        spend_nature="essential",
        status="confirmed",
        deleted_at=datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc),
    )
    db.add_all([kept, removed])
    db.commit()

    with TestClient(_application(db, user)) as client:
        # The log keeps the removed record visible, flagged rather than hidden.
        listed = client.get("/api/transactions")
        assert listed.status_code == 200
        by_id = {item["id"]: item for item in listed.json()}
        assert by_id[str(removed.id)]["deletedAt"] == "2026-07-13T09:00:00Z"
        assert by_id[str(kept.id)]["deletedAt"] is None

        overview = client.get("/api/overview", params={"month": "2026-07-01"})
        assert overview.status_code == 200
        assert overview.json()["summary"]["spentMinor"] == 30_000
        assert overview.json()["summary"]["expenseCount"] == 1

        edit = client.patch(f"/api/transactions/{removed.id}", json={
            "amountMinor": 60_000,
            "merchant": "Groceries",
            "transactionAt": "2026-07-12T09:00:00Z",
            "transactionType": "expense",
            "spendNature": "essential",
        })
        assert edit.status_code == 404

        searched = client.get("/api/transactions", params={"q": "groceries"})
        assert [item["id"] for item in searched.json()] == [str(removed.id)]

        # The log can be narrowed back to canonical records on request.
        canonical_only = client.get("/api/transactions", params={"include_removed": "false"})
        assert [item["id"] for item in canonical_only.json()] == [str(kept.id)]

        # Restore clears the tombstone and the record rejoins the totals.
        restored = client.post(f"/api/transactions/{removed.id}/restore")
        assert restored.status_code == 200
        assert restored.json()["deletedAt"] is None
        overview_after = client.get("/api/overview", params={"month": "2026-07-01"})
        assert overview_after.json()["summary"]["spentMinor"] == 80_000
        assert overview_after.json()["summary"]["expenseCount"] == 2

        # Restoring an active record is a 404, not a silent success.
        again = client.post(f"/api/transactions/{removed.id}/restore")
        assert again.status_code == 404

        # The ledger page can remove directly: same tombstone as the chat.
        page_removed = client.delete(f"/api/transactions/{kept.id}")
        assert page_removed.status_code == 200
        assert page_removed.json()["deletedAt"] is not None
        overview_final = client.get("/api/overview", params={"month": "2026-07-01"})
        assert overview_final.json()["summary"]["spentMinor"] == 50_000
        # A second delete of the same record is a refusal.
        assert client.delete(f"/api/transactions/{kept.id}").status_code == 404
        # And the round trip closes: restore brings it back.
        assert client.post(f"/api/transactions/{kept.id}/restore").status_code == 200


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


def test_money_page_normalizes_non_expense_taxonomy_and_spend_nature(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    food = db.scalar(select(Category).where(Category.slug == "food"))
    dining = db.scalar(select(Subcategory).where(Subcategory.category_id == food.id, Subcategory.slug == "dining"))
    transaction = Transaction(
        user_id=user.id,
        transaction_type="expense",
        amount_minor=50_000,
        currency="INR",
        merchant_name="Correction",
        category_id=food.id,
        subcategory_id=dining.id,
        transaction_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        spend_nature="discretionary",
        status="confirmed",
    )
    db.add(transaction)
    db.commit()

    with TestClient(_application(db, user)) as client:
        response = client.patch(f"/api/transactions/{transaction.id}", json={
            "amountMinor": 50_000,
            "merchant": "Correction",
            "transactionAt": "2026-08-13T00:00:00Z",
            "transactionType": "income",
            # The API adapter omits category writes for non-expenses; the
            # canonical service must still replace the old expense path.
            "categoryId": str(food.id),
            "subcategoryId": str(dining.id),
            "spendNature": "discretionary",
            "location": None,
        })

    assert response.status_code == 200
    assert response.json()["category"] == "Income"
    assert response.json()["subcategory"] == "Other"
    assert response.json()["spendNature"] == "unknown"


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


def test_transaction_endpoints_store_a_device_fix_only_while_location_is_allowed(db):
    """Coordinates travel the whole way, and only with permission.

    Every field on a transaction is hand-copied across five places — the schema,
    both endpoints, and both service functions — so a field can be accepted by
    the API and silently dropped before the row is written. This exercises the
    seam end to end rather than any one layer.
    """
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    food = db.scalar(select(Category).where(Category.slug == "food"))
    entry = {
        "amountMinor": 42_000,
        "merchant": "Third Wave",
        "transactionAt": "2026-08-19T09:15:00Z",
        "transactionType": "expense",
        "categoryId": str(food.id),
        "subcategoryId": None,
        "spendNature": "discretionary",
        "location": "Indiranagar",
    }
    fix = {"latitude": 12.971599, "longitude": 77.594566, "locationAccuracy": 18}

    with TestClient(_application(db, user)) as client:
        # Off by default: a client that sends coordinates anyway is refused by
        # the writer, not merely unasked by the interface.
        refused = client.post("/api/transactions", json={**entry, **fix})
        assert refused.status_code == 201
        stored = db.get(Transaction, UUID(refused.json()["id"]))
        assert (stored.latitude, stored.longitude, stored.location_accuracy) == (None, None, None)
        assert stored.location_source == "user"

        assert client.patch("/api/privacy/location", json={"enabled": True}).status_code == 200

        created = client.post("/api/transactions", json={**entry, **fix})
        assert created.status_code == 201
        saved = db.get(Transaction, UUID(created.json()["id"]))
        assert float(saved.latitude) == 12.971599
        assert float(saved.longitude) == 77.594566
        assert saved.location_accuracy == 18
        assert saved.location_source == "device"

        # Renaming the place keeps the fix and its provenance.
        renamed = client.patch(f"/api/transactions/{saved.id}", json={**entry, "location": "100 Feet Road"})
        assert renamed.status_code == 200
        db.refresh(saved)
        assert renamed.json()["location"] == "100 Feet Road"
        assert float(saved.latitude) == 12.971599
        assert saved.location_source == "device"

        moved = client.patch(f"/api/transactions/{saved.id}", json={
            **entry, "latitude": 12.934533, "longitude": 77.626579, "locationAccuracy": 42,
        })
        assert moved.status_code == 200
        db.refresh(saved)
        assert float(saved.latitude) == 12.934533
        assert saved.location_accuracy == 42

        # Revoked afterwards: an edit from a device that has lost permission
        # neither records a new fix nor erases where the spend actually was.
        assert client.patch("/api/privacy/location", json={"enabled": False}).status_code == 200
        edited = client.patch(f"/api/transactions/{saved.id}", json={**entry, "latitude": 1.0, "longitude": 1.0})
        assert edited.status_code == 200
        db.refresh(saved)
        assert float(saved.latitude) == 12.934533
        assert saved.location_source == "device"

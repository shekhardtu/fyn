from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import current_user, router
from app.database import get_db
from app.models import Conversation, Message, Transaction, User
from app.seed import DEFAULT_USER_EMAIL


def test_api_enforces_one_identity_boundary_across_user_data(db, monkeypatch):
    """Exercise the HTTP boundary with two users, including destructive APIs."""
    default_user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    other_user = User(email="other@fynai.local", display_name="Other user", currency="USD")
    db.add(other_user)
    db.flush()

    default_thread = Conversation(user_id=default_user.id, title="Default private thread")
    other_thread = Conversation(user_id=other_user.id, title="Other private thread")
    db.add_all([default_thread, other_thread])
    db.flush()
    db.add_all([
        Message(conversation_id=default_thread.id, role="user", content="default secret"),
        Message(conversation_id=other_thread.id, role="user", content="other secret"),
        Transaction(
            user_id=default_user.id,
            transaction_type="expense",
            amount_minor=11_100,
            currency="INR",
            merchant_name="Default Merchant",
            transaction_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            status="confirmed",
        ),
        Transaction(
            user_id=other_user.id,
            transaction_type="expense",
            amount_minor=22_200,
            currency="USD",
            merchant_name="Other Merchant",
            transaction_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
            status="confirmed",
        ),
    ])
    db.commit()

    identity = {"user_id": default_user.id}
    application = FastAPI()
    application.include_router(router)

    def override_db():
        yield db

    def override_user():
        return db.get(User, identity["user_id"])

    application.dependency_overrides[get_db] = override_db
    application.dependency_overrides[current_user] = override_user
    monkeypatch.setattr("app.services.user_data.export_user_memories", lambda _user_id: [])
    monkeypatch.setattr("app.services.user_data.clear_user_memories", lambda _user_id: 0)

    with TestClient(application) as client:
        # Every conversation-scoped transport has identical not-found behavior.
        foreign_id = str(other_thread.id)
        assert client.get(f"/api/conversations/{foreign_id}").status_code == 404
        assert client.delete(f"/api/conversations/{foreign_id}").status_code == 404
        assert client.get(f"/api/agent/threads/{foreign_id}").status_code == 404
        assert client.post("/api/agent", json={
            "threadId": foreign_id,
            "runId": str(uuid4()),
            "state": {},
            "messages": [{"id": str(uuid4()), "role": "user", "content": "hello"}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }).status_code == 404
        assert client.post(
            "/api/imports/csv",
            data={"conversation_id": foreign_id},
            files={"file": ("foreign.csv", b"date,description,debit\n2026-08-01,test,10\n", "text/csv")},
        ).status_code == 404

        # Rejected chat IDs must not create a replacement thread under the caller.
        assert db.scalar(select(func.count()).select_from(Conversation)) == 2

        rail = client.get("/api/conversations").json()
        assert [item["id"] for item in rail["items"]] == [str(default_thread.id)]
        transactions = client.get("/api/transactions").json()
        assert [item["merchant"] for item in transactions] == ["Default Merchant"]
        overview = client.get("/api/overview", params={"month": "2026-08-01"}).json()
        assert overview["summary"]["spentMinor"] == 11_100
        assert overview["categories"][0]["label"] == "Uncategorized"
        exported = client.get("/api/privacy/export").json()
        assert exported["user"]["email"] == DEFAULT_USER_EMAIL
        assert [item["merchant_name"] for item in exported["transactions"]] == ["Default Merchant"]
        assert "other secret" not in str(exported)

        identity["user_id"] = other_user.id
        assert client.get(f"/api/conversations/{default_thread.id}").status_code == 404
        assert client.get(f"/api/conversations/{other_thread.id}").status_code == 200
        assert [item["merchant"] for item in client.get("/api/transactions").json()] == ["Other Merchant"]
        assert client.get("/api/overview", params={"month": "2026-08-01"}).json()["summary"]["spentMinor"] == 22_200

        # Deleting the default user's complete data leaves the other identity intact.
        identity["user_id"] = default_user.id
        assert client.request(
            "DELETE",
            "/api/privacy/data",
            json={"confirmation": "DELETE MY DATA"},
        ).json() == {"deleted": True}
        identity["user_id"] = other_user.id
        assert client.get(f"/api/conversations/{other_thread.id}").status_code == 200
        assert [item["merchant"] for item in client.get("/api/transactions").json()] == ["Other Merchant"]
        assert db.get(User, other_user.id) is not None

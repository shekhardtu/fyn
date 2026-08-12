import json

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import create_observation, current_user, delete_data, export_data, privacy_status, revoke_source, router, set_location_preference
from app.config import SESSION_COOKIE_NAME
from app.database import get_db
from app.models import Category, Merchant, TaxonomyScope, Transaction, User, UserPreference
from app.schemas import DataDeletionIn, LocationPreferenceIn, ObservationIn
from app.seed import DEFAULT_USER_EMAIL, default_user
from app.services.conversation import get_or_create_conversation, handle_action, handle_chat
from app.services.user_data import DEPENDENT_USER_DATA, OWNED_USER_DATA, validate_user_data_registry


def test_privacy_preferences_and_source_revocation_are_enforced(db):
    user = default_user(db)
    status = privacy_status(db, user)
    assert status.location_enabled is False
    assert status.sources["sms"] is True

    set_location_preference(LocationPreferenceIn(enabled=True), db, user)
    assert privacy_status(db, user).location_enabled is True
    revoke_source("sms", db, user)
    assert privacy_status(db, user).sources["sms"] is False

    observation = ObservationIn(source_type="sms", source_message_id="blocked-1", transaction_type="expense", amount_minor=10000, merchant="Cafe", transaction_date="2026-08-10")
    with pytest.raises(HTTPException) as error:
        create_observation(observation, db, user)
    assert error.value.status_code == 403


def test_export_contains_complete_registered_financial_state(db, monkeypatch):
    user = default_user(db)
    monkeypatch.setattr(
        "app.services.user_data.export_user_memories",
        lambda user_id: [{"memory_id": "memory-1", "user_id": str(user_id), "memory": "saved"}],
    )
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Spent ₹2,000 at Toit today")
    handle_action(db, user, conversation, "commit_transaction", {"draftId": response.widgets[0].data["draftId"]})

    response = export_data(db, user)
    payload = json.loads(response.body)
    assert payload["user"]["email"] == DEFAULT_USER_EMAIL
    assert payload["transactions"][0]["amount_minor"] == 200_000
    assert payload["transactionSources"][0]["source_type"] == "manual"
    assert payload["merchants"][0]["owner_user_id"] == str(user.id)
    assert payload["messages"]
    assert payload["userMemories"][0]["memory_id"] == "memory-1"
    assert {item.key for item in OWNED_USER_DATA if item.exportable} <= payload.keys()
    assert {item.key for item in DEPENDENT_USER_DATA} <= payload.keys()
    # Credentials are deleted with the account but deliberately withheld from
    # the export: a session digest or one-time-code hash protects the account
    # rather than recording anything its owner did.
    assert not {item.key for item in OWNED_USER_DATA if not item.exportable} & payload.keys()
    assert response.headers["content-disposition"].endswith('.json"')


def test_explicit_deletion_removes_user_financial_state_and_memory(db, monkeypatch):
    user = default_user(db)
    deleted_memory_for = []
    monkeypatch.setattr(
        "app.services.user_data.clear_user_memories",
        lambda user_id: deleted_memory_for.append(user_id) or 1,
    )
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Salary ₹3 lakh credited today")
    handle_action(db, user, conversation, "commit_transaction", {"draftId": response.widgets[0].data["draftId"]})
    set_location_preference(LocationPreferenceIn(enabled=True), db, user)

    # Deletion also has to clear the session cookie, so the route takes the
    # outgoing response.
    outgoing = Response()
    result = delete_data(DataDeletionIn(confirmation="DELETE MY DATA"), outgoing, db, user)
    assert result.deleted is True
    assert SESSION_COOKIE_NAME in outgoing.headers["set-cookie"]
    assert db.scalar(select(User)) is None
    assert db.scalar(select(Transaction)) is None
    assert db.scalar(select(UserPreference)) is None
    assert db.scalar(select(Merchant)) is None
    assert db.scalar(select(Category).where(Category.scope == TaxonomyScope.USER.value)) is None
    assert deleted_memory_for == [user.id]


def test_user_data_registry_covers_every_owned_model():
    validate_user_data_registry()


def test_privacy_routes_answer_over_http_in_the_shape_the_client_reads(db):
    """Exercised through the transport, which is where these last broke.

    Calling the route functions directly proves the domain logic and nothing
    about serialization: FastAPI re-validates whatever a route returns against
    its `response_model`, and a body that cannot survive that answers 500 to
    every real caller while the direct-call tests stay green.
    """
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[current_user] = lambda: default_user(db)

    with TestClient(application) as client:
        status = client.get("/api/privacy")
        assert status.status_code == 200, status.text
        assert status.json()["locationEnabled"] is False
        assert status.json()["sources"]["sms"] is True

        location = client.patch("/api/privacy/location", json={"enabled": True})
        assert location.status_code == 200, location.text
        assert location.json() == {"locationEnabled": True}

        revoked = client.post("/api/privacy/sources/sms/revoke", json={})
        assert revoked.status_code == 200, revoked.text
        assert revoked.json() == {"sourceType": "sms", "active": False}

        assert client.get("/api/privacy").json()["locationEnabled"] is True
        assert client.get("/api/privacy").json()["sources"]["sms"] is False

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api_contacts import router
from app.database import get_db
from app.event_time import now_utc
from app.models import SharedRecord, SharedRecordParticipant, User, UserIdentity
from app.security import current_user
from app.seed import default_user


def _user(db, *, name: str, email: str, phone: str) -> User:
    user = User(display_name=name, email=email, phone=phone)
    db.add(user)
    db.flush()
    db.add_all([
        UserIdentity(user_id=user.id, provider="email", identifier=email, email=email, source="otp", verified_at=now_utc()),
        UserIdentity(user_id=user.id, provider="phone", identifier=phone, source="otp", verified_at=now_utc()),
    ])
    db.commit()
    return user


def _client(db, user: User) -> TestClient:
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[current_user] = lambda: user
    return TestClient(application)


def _share_record(db, first: User, second: User) -> None:
    record = SharedRecord(kind="personal_loan", status="active", created_by_user_id=first.id)
    db.add(record)
    db.flush()
    db.add_all([
        SharedRecordParticipant(shared_record_id=record.id, member_user_id=first.id, role="lender", display_name=first.display_name, state="accepted"),
        SharedRecordParticipant(shared_record_id=record.id, member_user_id=second.id, role="borrower", display_name=second.display_name, state="accepted"),
    ])
    db.commit()


def test_partial_lookup_only_searches_prior_relationships_and_supports_both_identifiers(db):
    hari = default_user(db)
    assert hari is not None
    rahul = _user(db, name="Rahul Sharma", email="rahul@example.test", phone="+919876543210")
    _user(db, name="Rhea Unknown", email="rhea@example.test", phone="+919812345678")
    _share_record(db, hari, rahul)
    client = _client(db, hari)

    by_email = client.get("/contacts", params={"channel": "email", "q": "rah"})
    assert by_email.status_code == 200, by_email.text
    assert by_email.json() == [{
        "channel": "email",
        "identifier": "rahul@example.test",
        "displayName": "Rahul Sharma",
        "matchKind": "previously_shared",
    }]

    by_phone = client.get("/contacts", params={"channel": "phone", "q": "987"})
    assert by_phone.status_code == 200, by_phone.text
    assert by_phone.json()[0]["identifier"] == "+919876543210"
    assert by_phone.json()[0]["displayName"] == "Rahul Sharma"

    unknown_partial = client.get("/contacts", params={"channel": "email", "q": "rhe"})
    assert unknown_partial.status_code == 200
    assert unknown_partial.json() == []


def test_complete_identifier_resolves_an_exact_account_but_never_the_caller(db):
    hari = default_user(db)
    assert hari is not None
    rhea = _user(db, name="Rhea Kapoor", email="rhea@example.test", phone="+919812345678")
    client = _client(db, hari)

    exact_email = client.get("/contacts", params={"channel": "email", "q": "RHEA@example.test"})
    assert exact_email.status_code == 200, exact_email.text
    assert exact_email.json() == [{
        "channel": "email",
        "identifier": "rhea@example.test",
        "displayName": rhea.display_name,
        "matchKind": "exact",
    }]

    exact_phone = client.get("/contacts", params={"channel": "phone", "q": "98123 45678"})
    assert exact_phone.status_code == 200, exact_phone.text
    assert exact_phone.json()[0]["matchKind"] == "exact"

    own_email = client.get("/contacts", params={"channel": "email", "q": hari.email})
    assert own_email.status_code == 200
    assert own_email.json() == []

    too_short = client.get("/contacts", params={"channel": "email", "q": "ra"})
    assert too_short.status_code == 422

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.api_lending import router
from app.database import get_db
from app.event_time import now_utc
from app.models import (
    CommandReceipt,
    DocumentRevision,
    Loan,
    NotificationOutbox,
    SharedRecord,
    User,
    UserIdentity,
)
from app.security import current_user, optional_user
from app.seed import default_user
from app.config import get_settings
from app.services.lending_notifications import deliver_one


def _user(db, *, name: str, email: str | None = None, phone: str | None = None) -> User:
    user = User(display_name=name, email=email, phone=phone)
    db.add(user)
    db.flush()
    if email:
        db.add(UserIdentity(
            user_id=user.id,
            provider="email",
            identifier=email.lower(),
            email=email.lower(),
            source="otp",
            verified_at=now_utc(),
        ))
    if phone:
        db.add(UserIdentity(
            user_id=user.id,
            provider="phone",
            identifier=phone,
            source="otp",
            verified_at=now_utc(),
        ))
    db.commit()
    return user


def _client(db, user: User | None) -> TestClient:
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    if user is not None:
        application.dependency_overrides[current_user] = lambda: user
        application.dependency_overrides[optional_user] = lambda: user
    return TestClient(application)


def _key(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"


def test_creator_cannot_invite_their_own_sign_in_identifier(db):
    hari = default_user(db)
    assert hari is not None and hari.email is not None
    client = _client(db, hari)
    records_before = db.scalar(select(func.count()).select_from(SharedRecord))

    response = client.post(
        "/loan-agreements",
        headers={"Idempotency-Key": _key("self-invite")},
        json={
            "direction": "lent",
            "counterpartyName": "Myself",
            "inviteChannel": "email",
            "inviteValue": hari.email,
            "principalMinor": 10_000,
            "currency": "INR",
            "moneyDate": "2026-08-25",
            "dueDate": "2026-09-25",
            "annualRateBps": 0,
            "securityItems": [],
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "Choose someone else’s email address or phone number for this shared plan."
    assert db.scalar(select(func.count()).select_from(SharedRecord)) == records_before
    assert db.scalar(select(func.count()).select_from(CommandReceipt)) == 0


def test_email_invitation_document_amendment_payment_and_two_party_close(db):
    hari = default_user(db)
    assert hari is not None
    rahul = _user(db, name="Rahul", email="rahul@example.test")
    hari_client = _client(db, hari)
    rahul_client = _client(db, rahul)

    created = hari_client.post(
        "/loan-agreements",
        headers={"Idempotency-Key": _key("create")},
        json={
            "direction": "lent",
            "counterpartyName": "Rahul",
            "inviteChannel": "email",
            "inviteValue": "rahul@example.test",
            "principalMinor": 100_000,
            "currency": "INR",
            "moneyDate": "2026-08-24",
            "dueDate": "2026-12-24",
            "annualRateBps": 0,
            "note": "For the laptop",
            "securityItems": [{
                "kind": "post_dated_cheque",
                "description": "Post-dated cheque for the agreed total",
                "maskedIdentifier": "Cheque ••4821",
                "statedValueMinor": 100_000,
            }],
        },
    )
    assert created.status_code == 201, created.text
    created_body = created.json()
    loan_id = created_body["loan"]["id"]
    share_path = created_body["loan"]["invitation"]["sharePath"]
    token = share_path.rsplit("/", 1)[-1]
    assert created_body["loan"]["status"] == "pending_acceptance"
    assert created_body["loan"]["documentRevision"]["revisionNumber"] == 1
    assert created_body["loan"]["documentRevision"]["contentHash"]
    assert created_body["loan"]["securityItems"][0]["heldBy"] == "Hari"
    assert created_body["loan"]["documentRevision"]["content"]["assuranceItems"][0]["maskedIdentifier"] == "Cheque ••4821"

    public_preview = _client(db, None).get(f"/loan-invitations/{token}")
    assert public_preview.status_code == 200
    assert public_preview.json()["tokenValid"] is True
    assert public_preview.json()["canRedeem"] is False

    controlled_preview = rahul_client.get(f"/loan-invitations/{token}")
    assert controlled_preview.json()["canRedeem"] is True
    redeemed = rahul_client.post(f"/loan-invitations/{token}/redeem")
    assert redeemed.status_code == 200, redeemed.text
    assert redeemed.json()["loan"]["responseNeeded"] is True

    accepted = rahul_client.post(
        f"/loan-agreements/{loan_id}/accept",
        headers={"Idempotency-Key": _key("accept-initial")},
        json={"expectedRowVersion": redeemed.json()["loan"]["rowVersion"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["loan"]["status"] == "active"
    assert accepted.json()["loan"]["fundingStatus"] == "confirmed"

    before_amendment = hari_client.get(f"/loan-agreements/{loan_id}").json()
    amended = hari_client.post(
        f"/loan-agreements/{loan_id}/term-proposals",
        headers={"Idempotency-Key": _key("amend")},
        json={
            "dueDate": "2027-01-24",
            "annualRateBps": 300,
            "note": "Extended together",
            "expectedRowVersion": before_amendment["rowVersion"],
        },
    )
    assert amended.status_code == 200, amended.text
    amendment = amended.json()["loan"]
    assert amendment["documentRevision"]["revisionNumber"] == 2
    paths = {change["fieldPath"] for change in amendment["documentRevision"]["changes"]}
    assert {"terms.dueDate", "terms.annualRateBps", "terms.note"} <= paths
    assert amendment["currentTerms"]["version"] == 1

    revisions = hari_client.get(
        f"/shared-documents/{amendment['documentRevision']['documentId']}/revisions"
    )
    assert revisions.status_code == 200, revisions.text
    assert [item["revisionNumber"] for item in revisions.json()] == [2, 1]
    assert revisions.json()[1]["state"] == "accepted"

    accepted_amendment = rahul_client.post(
        f"/loan-agreements/{loan_id}/accept",
        headers={"Idempotency-Key": _key("accept-amendment")},
        json={"expectedRowVersion": amendment["rowVersion"]},
    )
    assert accepted_amendment.status_code == 200, accepted_amendment.text
    active = accepted_amendment.json()["loan"]
    assert active["currentTerms"]["version"] == 2
    assert active["currentTerms"]["annualRateBps"] == 300

    reminder = hari_client.post(
        f"/loan-agreements/{loan_id}/reminders",
        headers={"Idempotency-Key": _key("reminder")},
        json={"tone": "friendly", "note": "Just keeping our plan visible"},
    )
    assert reminder.status_code == 200, reminder.text
    assert reminder.json()["channel"] == "email"
    assert reminder.json()["state"] == "pending"

    total = active["currentTerms"]["totalRepayableMinor"]
    recorded = rahul_client.post(
        f"/loan-agreements/{loan_id}/payments",
        headers={"Idempotency-Key": _key("payment")},
        json={"amountMinor": total, "occurredOn": str(date(2026, 10, 1)), "note": "Paid in full"},
    )
    assert recorded.status_code == 200, recorded.text
    pending = recorded.json()["loan"]
    cashflow_id = pending["cashflows"][0]["id"]
    assert pending["cashflows"][0]["state"] == "proposed"

    confirmation_key = _key("confirm-payment")
    confirmed = hari_client.post(
        f"/loan-cashflows/{cashflow_id}/confirm",
        headers={"Idempotency-Key": confirmation_key},
        json={"expectedRowVersion": pending["rowVersion"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["loan"]["status"] == "settlement_pending"
    assert confirmed.json()["loan"]["outstandingPrincipalMinor"] == 0

    replay = hari_client.post(
        f"/loan-cashflows/{cashflow_id}/confirm",
        headers={"Idempotency-Key": confirmation_key},
        json={"expectedRowVersion": pending["rowVersion"]},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert len(list(db.scalars(select(CommandReceipt).where(CommandReceipt.command_type == "personal_loan.confirm_payment")))) == 1

    closure = hari_client.post(
        f"/loan-agreements/{loan_id}/close",
        headers={"Idempotency-Key": _key("close-propose")},
    )
    assert closure.status_code == 200
    assert closure.json()["loan"]["status"] == "settlement_pending"
    assert closure.json()["loan"]["securityItems"][0]["state"] == "return_pending_confirmation"
    closed = rahul_client.post(
        f"/loan-agreements/{loan_id}/close",
        headers={"Idempotency-Key": _key("close-confirm")},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["loan"]["status"] == "closed"
    assert closed.json()["loan"]["securityItems"][0]["state"] == "returned"

    integrity = hari_client.get(f"/loan-agreements/{loan_id}/integrity")
    assert integrity.status_code == 200
    assert integrity.json()["valid"] is True
    projections = list(db.scalars(select(Loan).where(
        Loan.shared_record_id == UUID(closed.json()["loan"]["sharedRecordId"])
    )))
    assert len(projections) == 2
    assert {projection.direction for projection in projections} == {"lent", "borrowed"}


def test_phone_invitation_requires_matching_verified_identity_and_failures_are_atomic(db):
    hari = default_user(db)
    assert hari is not None
    priya = _user(db, name="Priya", phone="+919999999999")
    stranger = _user(db, name="Stranger", phone="+918888888888")
    hari_client = _client(db, hari)

    bad = hari_client.post(
        "/loan-agreements",
        headers={"Idempotency-Key": _key("bad-create")},
        json={
            "direction": "borrowed",
            "counterpartyName": "Priya",
            "inviteChannel": "phone",
            "inviteValue": "+919999999999",
            "principalMinor": 50_000,
            "currency": "INR",
            "moneyDate": "2026-10-01",
            "dueDate": "2026-09-01",
            "annualRateBps": 0,
        },
    )
    assert bad.status_code == 422
    assert len(list(db.scalars(select(SharedRecord)))) == 0

    created = hari_client.post(
        "/loan-agreements",
        headers={"Idempotency-Key": _key("phone-create")},
        json={
            "direction": "borrowed",
            "counterpartyName": "Priya",
            "inviteChannel": "phone",
            "inviteValue": "+919999999999",
            "principalMinor": 50_000,
            "currency": "INR",
            "moneyDate": "2026-08-24",
            "dueDate": "2026-09-24",
            "annualRateBps": 0,
        },
    )
    assert created.status_code == 201, created.text
    token = created.json()["loan"]["invitation"]["sharePath"].rsplit("/", 1)[-1]

    denied = _client(db, stranger).post(f"/loan-invitations/{token}/redeem")
    assert denied.status_code == 422
    assert "phone number or email" in denied.json()["detail"]
    redeemed = _client(db, priya).post(f"/loan-invitations/{token}/redeem")
    assert redeemed.status_code == 200, redeemed.text
    assert redeemed.json()["loan"]["direction"] == "lent"

    outbox = db.scalar(select(NotificationOutbox).where(NotificationOutbox.topic == "shared_record.invitation"))
    assert outbox is not None
    assert outbox.channel == "phone"
    assert "+919999999999" not in outbox.destination_ciphertext
    assert token not in outbox.context_ciphertext
    assert len(list(db.scalars(select(DocumentRevision)))) == 1

    worker_settings = get_settings().model_copy(update={
        "sms_provider": "console",
        "email_provider": "console",
    })
    WorkerSession = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    while deliver_one(WorkerSession, worker_settings):
        pass
    db.expire_all()
    delivered = db.scalar(select(NotificationOutbox).where(NotificationOutbox.id == outbox.id))
    assert delivered is not None
    assert delivered.state == "sent"
    assert delivered.attempts == 1
    assert delivered.sent_at is not None

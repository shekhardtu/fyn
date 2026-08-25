from __future__ import annotations

from datetime import date
import io
import json
import zipfile
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.api_lending import router
from app.database import get_db
from app.event_time import now_utc
from app.models import (
    CommandReceipt,
    DocumentRevision,
    DocumentAsset,
    Loan,
    NotificationOutbox,
    SharedRecord,
    User,
    UserIdentity,
)
from app.security import current_user, optional_user
from app.seed import default_user
from app.config import get_settings
from app.services.document_assets import read_asset_bytes
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


def _client(db, user: User | None, *, settings=None) -> TestClient:
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    if user is not None:
        application.dependency_overrides[current_user] = lambda: user
        application.dependency_overrides[optional_user] = lambda: user
    if settings is not None:
        application.dependency_overrides[get_settings] = lambda: settings
    return TestClient(application)


def _key(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"


def test_production_lending_api_is_absent_without_complete_r2_credentials(db):
    hari = default_user(db)
    assert hari is not None
    settings = get_settings().model_copy(update={
        "environment": "production",
        "document_storage_provider": "local",
        "r2_account_id": None,
        "r2_bucket": None,
        "r2_access_key_id": None,
        "r2_secret_access_key": None,
    })

    response = _client(db, hari, settings=settings).get("/loan-agreements")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


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
            "interestRateBps": 0,
            "interestPeriod": "yearly",
            "interestMode": "simple",
            "securityItems": [],
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "Choose someone else’s email address or phone number for this shared plan."
    assert db.scalar(select(func.count()).select_from(SharedRecord)) == records_before
    assert db.scalar(select(func.count()).select_from(CommandReceipt)) == 0


def test_interest_period_is_required_and_monthly_interest_uses_a_30_day_basis(db):
    hari = default_user(db)
    assert hari is not None
    _user(db, name="Monthly Rahul", email="monthly-rahul@example.test")
    client = _client(db, hari)
    payload = {
        "direction": "lent",
        "intent": "record_given",
        "counterpartyName": "Monthly Rahul",
        "inviteChannel": "email",
        "inviteValue": "monthly-rahul@example.test",
        "principalMinor": 100_000,
        "currency": "INR",
        "moneyDate": "2026-01-01",
        "dueDate": "2026-03-02",
        "interestRateBps": 300,
        "interestMode": "simple",
        "securityItems": [],
    }

    missing_period = client.post(
        "/loan-agreements",
        headers={"Idempotency-Key": _key("missing-interest-period")},
        json=payload,
    )
    assert missing_period.status_code == 422

    created = client.post(
        "/loan-agreements",
        headers={"Idempotency-Key": _key("monthly-interest")},
        json={**payload, "interestPeriod": "monthly"},
    )
    assert created.status_code == 201, created.text
    loan = created.json()["loan"]
    term = loan["currentTerms"]
    assert term["interestRateBps"] == 300
    assert term["interestPeriod"] == "monthly"
    assert term["interestMode"] == "simple"
    assert term["annualizedRateBps"] == 3_600
    assert term["interestMethod"] == "simple_monthly"
    assert term["calculationBasis"] == "fixed_30_day_month"
    assert term["roundingPolicy"] == "half_up_minor_unit"
    assert term["totalInterestMinor"] == 6_000
    assert term["totalRepayableMinor"] == 106_000
    document_terms = loan["documentRevision"]["content"]["terms"]
    assert document_terms["interestPeriod"] == "monthly"
    assert document_terms["calculationBasis"] == "fixed_30_day_month"


def test_compound_monthly_interest_is_explicit_and_deterministic(db):
    hari = default_user(db)
    assert hari is not None
    _user(db, name="Compound Rahul", email="compound-rahul@example.test")
    created = _client(db, hari).post(
        "/loan-agreements",
        headers={"Idempotency-Key": _key("compound-interest")},
        json={
            "direction": "lent",
            "intent": "offer_to_lend",
            "counterpartyName": "Compound Rahul",
            "inviteChannel": "email",
            "inviteValue": "compound-rahul@example.test",
            "principalMinor": 100_000,
            "currency": "INR",
            "moneyDate": "2026-01-01",
            "dueDate": "2026-03-02",
            "interestRateBps": 1_000,
            "interestPeriod": "monthly",
            "interestMode": "compound",
        },
    )

    assert created.status_code == 201, created.text
    term = created.json()["loan"]["currentTerms"]
    assert term["interestMethod"] == "compound_monthly"
    assert term["totalInterestMinor"] == 21_000
    assert term["totalRepayableMinor"] == 121_000


def test_lender_requests_documents_and_borrower_fulfills_from_private_library(db, tmp_path):
    hari = default_user(db)
    assert hari is not None
    rahul = _user(db, name="Evidence Rahul", email="requested-evidence@example.test")
    settings = get_settings().model_copy(update={"document_storage_provider": "local", "document_storage_path": str(tmp_path)})
    hari_client = _client(db, hari, settings=settings)
    rahul_client = _client(db, rahul, settings=settings)

    created = hari_client.post(
        "/loan-agreements",
        headers={"Idempotency-Key": _key("request-evidence")},
        json={
            "direction": "lent",
            "intent": "offer_to_lend",
            "counterpartyName": "Evidence Rahul",
            "inviteChannel": "email",
            "inviteValue": "requested-evidence@example.test",
            "principalMinor": 80_000,
            "currency": "INR",
            "moneyDate": "2026-08-25",
            "dueDate": "2026-12-25",
            "interestRateBps": 300,
            "interestPeriod": "monthly",
            "interestMode": "simple",
            "documentRequests": [{
                "label": "Transfer receipt",
                "classification": "transfer_receipt",
                "instructions": "Upload the receipt after the transfer is made.",
                "required": True,
            }],
        },
    )
    assert created.status_code == 201, created.text
    loan = created.json()["loan"]
    token = loan["invitation"]["sharePath"].rsplit("/", 1)[-1]
    assert rahul_client.post(f"/loan-invitations/{token}/redeem").status_code == 200
    borrower_view = rahul_client.get(f"/loan-agreements/{loan['id']}").json()
    document_request = borrower_view["documentRequests"][0]
    assert document_request["requestedFromCurrentUser"] is True

    blocked = rahul_client.post(
        f"/loan-agreements/{loan['id']}/accept",
        headers={"Idempotency-Key": _key("accept-without-evidence")},
        json={"expectedRowVersion": borrower_view["rowVersion"]},
    )
    assert blocked.status_code == 422
    assert "required documents" in blocked.json()["detail"]

    upload = rahul_client.post(
        "/document-assets",
        data={"classification": "transfer_receipt", "description": "UPI transfer receipt"},
        files={"file": ("transfer.pdf", b"%PDF-1.4\n% transfer receipt\n", "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    library_asset = upload.json()
    library = rahul_client.get("/document-assets")
    assert [item["id"] for item in library.json()] == [library_asset["id"]]

    fulfilled = rahul_client.post(
        f"/loan-agreements/{loan['id']}/document-requests/fulfill",
        headers={"Idempotency-Key": _key("fulfill-evidence")},
        json={
            "items": [{"requestId": document_request["id"], "assetId": library_asset["id"]}],
            "expectedRowVersion": borrower_view["rowVersion"],
        },
    )
    assert fulfilled.status_code == 200, fulfilled.text
    replacement = fulfilled.json()["loan"]
    assert replacement["documentRevision"]["revisionNumber"] == 2
    assert replacement["documentRequests"][0]["state"] == "fulfilled"
    assert replacement["documentRequests"][0]["fulfilledAsset"]["id"] != library_asset["id"]
    assert len(replacement["documentRevision"]["acceptances"]) == 1
    assert replacement["documentRevision"]["acceptances"][0]["participantName"] == "Evidence Rahul"
    assert [item["id"] for item in rahul_client.get("/document-assets").json()] == [library_asset["id"]]

    accepted = hari_client.post(
        f"/loan-agreements/{loan['id']}/accept",
        headers={"Idempotency-Key": _key("lender-accepts-evidence")},
        json={"expectedRowVersion": replacement["rowVersion"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["loan"]["currentTerms"]["version"] == 2
    assert accepted.json()["loan"]["status"] == "funding_pending"


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
            "interestRateBps": 0,
            "interestPeriod": "yearly",
            "interestMode": "simple",
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
            "interestRateBps": 300,
            "interestPeriod": "yearly",
            "interestMode": "simple",
            "note": "Extended together",
            "expectedRowVersion": before_amendment["rowVersion"],
        },
    )
    assert amended.status_code == 200, amended.text
    amendment = amended.json()["loan"]
    assert amendment["documentRevision"]["revisionNumber"] == 2
    paths = {change["fieldPath"] for change in amendment["documentRevision"]["changes"]}
    assert {"terms.dueDate", "terms.interestRateBps", "terms.note"} <= paths
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
    assert active["currentTerms"]["interestRateBps"] == 300
    assert active["currentTerms"]["interestPeriod"] == "yearly"

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
            "interestRateBps": 0,
            "interestPeriod": "yearly",
            "interestMode": "simple",
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
            "interestRateBps": 0,
            "interestPeriod": "yearly",
            "interestMode": "simple",
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


def test_supporting_document_is_bound_to_revision_and_exported_as_evidence(db, tmp_path):
    hari = default_user(db)
    assert hari is not None
    rahul = _user(db, name="Rahul", email="rahul-evidence@example.test")
    settings = get_settings().model_copy(update={"document_storage_provider": "local", "document_storage_path": str(tmp_path)})
    hari_client = _client(db, hari, settings=settings)
    rahul_client = _client(db, rahul, settings=settings)

    uploaded = hari_client.post(
        "/document-assets",
        data={"classification": "external_agreement", "description": "Existing signed note"},
        files={"file": ("signed-note.pdf", b"%PDF-1.4\n% private evidence\n", "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    asset = uploaded.json()
    assert asset["originalFilename"] == "signed-note.pdf"
    assert asset["state"] == "clean"

    mismatch = hari_client.post(
        "/document-assets",
        data={"classification": "supporting_evidence"},
        files={"file": ("not-really.pdf", b"\x89PNG\r\n\x1a\ncontent", "application/pdf")},
    )
    assert mismatch.status_code == 422
    assert "do not match" in mismatch.json()["detail"]

    created = hari_client.post(
        "/loan-agreements",
        headers={"Idempotency-Key": _key("evidence-create")},
        json={
            "direction": "lent",
            "intent": "record_given",
            "counterpartyName": "Rahul",
            "inviteChannel": "email",
            "inviteValue": "rahul-evidence@example.test",
            "principalMinor": 75_000,
            "currency": "INR",
            "moneyDate": "2026-08-25",
            "dueDate": "2026-11-25",
            "interestRateBps": 300,
            "interestPeriod": "yearly",
            "interestMode": "simple",
            "assetIds": [asset["id"]],
        },
    )
    assert created.status_code == 201, created.text
    loan = created.json()["loan"]
    revision = loan["documentRevision"]
    assert revision["assets"][0]["sha256"] == asset["sha256"]
    assert revision["manifestHash"] != revision["contentHash"]
    assert revision["evidenceHash"] == revision["acceptances"][0]["evidenceHash"]
    assert len(revision["acceptances"][0]["requestIpHash"]) == 64
    assert len(revision["acceptances"][0]["userAgentHash"]) == 64

    stranger = _user(db, name="Stranger", email="stranger-evidence@example.test")
    assert _client(db, stranger, settings=settings).get(f"/document-assets/{asset['id']}/download").status_code == 404

    token = loan["invitation"]["sharePath"].rsplit("/", 1)[-1]
    assert rahul_client.post(f"/loan-invitations/{token}/redeem").status_code == 200
    reviewed = rahul_client.get(f"/loan-agreements/{loan['id']}").json()
    accepted = rahul_client.post(
        f"/loan-agreements/{loan['id']}/accept",
        headers={"Idempotency-Key": _key("evidence-accept")},
        json={"expectedRowVersion": reviewed["rowVersion"]},
    )
    assert accepted.status_code == 200, accepted.text
    acceptances = accepted.json()["loan"]["documentRevision"]["acceptances"]
    assert len(acceptances) == 2
    assert acceptances[1]["actorIdentifierMasked"].startswith("r")
    assert acceptances[1]["actorIdentifierMasked"].endswith("@example.test")
    assert "rahul-evidence" not in acceptances[1]["actorIdentifierMasked"]
    assert acceptances[1]["statementText"].startswith("I reviewed this exact revision")
    assert len(acceptances[1]["requestIpHash"]) == 64
    assert len(acceptances[1]["userAgentHash"]) == 64

    shared_asset_id = revision["assets"][0]["id"]
    assert shared_asset_id != asset["id"]
    downloaded = rahul_client.get(f"/document-assets/{shared_asset_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.content.startswith(b"%PDF-1.4")
    saved_asset = db.get(DocumentAsset, UUID(shared_asset_id))
    assert saved_asset is not None and read_asset_bytes(saved_asset, settings) == downloaded.content

    pdf = rahul_client.get(f"/loan-agreements/{loan['id']}/agreement.pdf")
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF-")
    reader = PdfReader(io.BytesIO(pdf.content))
    assert len(reader.pages) == 1
    agreement_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    normalized_agreement_text = " ".join(agreement_text.split())
    assert "Signing through authenticated acknowledgement" in normalized_agreement_text
    assert "Electronically acknowledged by Rahul" in normalized_agreement_text
    assert "Network fingerprint" in normalized_agreement_text
    assert "3% yearly simple · actual/365" in normalized_agreement_text
    assert "No handwritten signature or uploaded signature image is required" in normalized_agreement_text
    assert "not represented as a certificate-based digital signature" in normalized_agreement_text
    assert "regulated eSign" in normalized_agreement_text
    bundle = rahul_client.get(f"/loan-agreements/{loan['id']}/evidence-bundle")
    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        names = archive.namelist()
        assert any(name.endswith(".pdf") and name.startswith("agreement-") for name in names)
        assert any(name.endswith("signed-note.pdf") for name in names)
        evidence = json.loads(archive.read("evidence.json"))
        assert evidence["agreement"]["documentRevision"]["evidenceHash"] == revision["evidenceHash"]


def test_offer_requires_separate_funding_confirmation(db):
    hari = default_user(db)
    assert hari is not None
    rahul = _user(db, name="Rahul", email="rahul-funding@example.test")
    hari_client = _client(db, hari)
    rahul_client = _client(db, rahul)
    created = hari_client.post(
        "/loan-agreements",
        headers={"Idempotency-Key": _key("offer-create")},
        json={
            "direction": "lent",
            "intent": "offer_to_lend",
            "counterpartyName": "Rahul",
            "inviteChannel": "email",
            "inviteValue": "rahul-funding@example.test",
            "principalMinor": 25_000,
            "currency": "INR",
            "moneyDate": "2026-08-26",
            "dueDate": "2026-10-26",
            "interestRateBps": 0,
            "interestPeriod": "yearly",
            "interestMode": "simple",
        },
    ).json()["loan"]
    token = created["invitation"]["sharePath"].rsplit("/", 1)[-1]
    redeemed = rahul_client.post(f"/loan-invitations/{token}/redeem").json()["loan"]
    accepted = rahul_client.post(
        f"/loan-agreements/{created['id']}/accept",
        headers={"Idempotency-Key": _key("offer-accept")},
        json={"expectedRowVersion": redeemed["rowVersion"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["loan"]["status"] == "funding_pending"
    assert accepted.json()["loan"]["fundingCashflow"] is None

    funded = hari_client.post(
        f"/loan-agreements/{created['id']}/funding",
        headers={"Idempotency-Key": _key("offer-fund")},
        json={"occurredOn": "2026-08-26", "note": "UPI reference ending 91"},
    )
    assert funded.status_code == 200, funded.text
    pending = funded.json()["loan"]
    assert pending["fundingCashflow"]["state"] == "proposed"
    confirmed = rahul_client.post(
        f"/loan-cashflows/{pending['fundingCashflow']['id']}/confirm",
        headers={"Idempotency-Key": _key("offer-confirm")},
        json={"expectedRowVersion": pending["rowVersion"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["loan"]["status"] == "active"
    assert confirmed.json()["loan"]["fundingStatus"] == "confirmed"

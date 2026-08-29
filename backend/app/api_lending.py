"""HTTP boundary for collaborative personal lending.

The route layer stays deliberately small: transaction control, authentication,
contract validation, and translation from domain errors to stable HTTP answers.
All financial and document rules live in reusable service modules.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import io
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import get_db
from .config import Settings, get_settings
from .lending_schemas import (
    ConfirmLoanPaymentIn,
    CreatePersonalLoanIn,
    DocumentRevisionOut,
    DocumentAssetOut,
    FulfillDocumentRequestsIn,
    InvitationPreviewOut,
    LoanCommandOut,
    LoanTermProposalIn,
    PersonalLoanDetailOut,
    PersonalLoanListOut,
    RecordLoanPaymentIn,
    RecordLoanFundingIn,
    ReminderOut,
    SendLoanReminderIn,
)
from .models import DocumentRevision, PersonalLoanAgreement, SharedDocument, User
from .security import current_user, optional_user
from .services.personal_loans import (
    PersonalLoanError,
    _document_dict,
    accept_current_terms,
    close_loan,
    confirm_payment,
    create_personal_loan,
    detail_payload,
    fulfill_document_requests,
    invitation_preview,
    list_personal_loans,
    propose_terms,
    record_payment,
    record_funding,
    redeem_loan_invitation,
    send_reminder,
    summary_payload,
    verify_projection_integrity,
)
from .services.document_assets import (
    DocumentAssetError,
    asset_dict,
    delete_draft_asset,
    library_assets,
    owned_draft_asset,
    presigned_download_url,
    readable_asset,
    store_upload,
    stored_path,
)
from .services.evidence_documents import agreement_pdf, evidence_bundle
from .services.shared_records import (
    SharedRecordConflict,
    SharedRecordError,
    SharedRecordNotFound,
    record_for_user,
)


def _require_personal_lending(settings: Settings = Depends(get_settings)) -> None:
    if not settings.personal_lending_available:
        # A disabled product surface is indistinguishable from one that does
        # not exist. This prevents stale clients from bypassing the UI gate.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


router = APIRouter(tags=["personal-lending"], dependencies=[Depends(_require_personal_lending)])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]


@contextmanager
def _translated_errors(db: Session):
    try:
        yield
    except SharedRecordNotFound as error:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except SharedRecordConflict as error:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (PersonalLoanError, SharedRecordError, DocumentAssetError) as error:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That change was already applied or the shared record changed. Refresh and try again.",
        ) from error


def _request_hash(value: str | None, settings: Settings) -> str | None:
    if not value:
        return None
    return hmac.new(settings.auth_secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _command(
    db: Session,
    agreement: PersonalLoanAgreement,
    user: User,
    *,
    replayed: bool,
    invitation_token: str | None = None,
) -> LoanCommandOut:
    return LoanCommandOut.model_validate({
        "loan": detail_payload(db, agreement, user, invitation_token=invitation_token),
        "replayed": replayed,
    })


@router.get("/loan-agreements", response_model=PersonalLoanListOut)
def loans(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> PersonalLoanListOut:
    items = [summary_payload(db, agreement, user) for agreement in list_personal_loans(db, user)]
    default_currency_items = [item for item in items if item["currency"] == user.currency]
    return PersonalLoanListOut.model_validate({
        # Totals cannot safely add different units. Keep every agreement in
        # the list, but summarize only the profile's current default currency.
        "moneyIGaveMinor": sum(item["outstandingPrincipalMinor"] for item in default_currency_items if item["direction"] == "lent"),
        "moneyIReceivedMinor": sum(item["outstandingPrincipalMinor"] for item in default_currency_items if item["direction"] == "borrowed"),
        "needsResponseCount": sum(1 for item in items if item["responseNeeded"]),
        "items": items,
    })


@router.post("/loan-agreements", response_model=LoanCommandOut, status_code=status.HTTP_201_CREATED)
def create_loan(
    payload: CreatePersonalLoanIn,
    http_request: Request,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> LoanCommandOut:
    with _translated_errors(db):
        agreement, token, replayed = create_personal_loan(
            db,
            user=user,
            request=payload,
            idempotency_key=idempotency_key,
            settings=settings,
            request_ip_hash=_request_hash(http_request.client.host if http_request.client else None, settings),
            user_agent_hash=_request_hash(http_request.headers.get("user-agent"), settings),
        )
        output = _command(db, agreement, user, replayed=replayed, invitation_token=token)
        db.commit()
        return output


@router.get("/loan-agreements/{agreement_id}", response_model=PersonalLoanDetailOut)
def loan_detail(
    agreement_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> PersonalLoanDetailOut:
    with _translated_errors(db):
        agreement = db.scalar(select(PersonalLoanAgreement).where(PersonalLoanAgreement.id == agreement_id))
        if agreement is None:
            raise SharedRecordNotFound("Loan not found")
        return PersonalLoanDetailOut.model_validate(detail_payload(db, agreement, user))


@router.get("/loan-agreements/{agreement_id}/agreement.pdf")
def loan_agreement_pdf(
    agreement_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    with _translated_errors(db):
        agreement = db.get(PersonalLoanAgreement, agreement_id)
        if agreement is None:
            raise SharedRecordNotFound("Loan not found")
        payload = detail_payload(db, agreement, user)
        rendered = agreement_pdf(payload)
        return StreamingResponse(
            io.BytesIO(rendered),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="agreement-{agreement_id}.pdf"'},
        )


@router.get("/loan-agreements/{agreement_id}/evidence-bundle")
def loan_evidence_bundle(
    agreement_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
):
    with _translated_errors(db):
        agreement = db.get(PersonalLoanAgreement, agreement_id)
        if agreement is None:
            raise SharedRecordNotFound("Loan not found")
        payload = detail_payload(db, agreement, user)
        revision = db.get(DocumentRevision, payload["documentRevision"]["id"])
        if revision is None:
            raise SharedRecordNotFound("Agreement document not found")
        bundle = evidence_bundle(payload=payload, revision=revision, db=db, settings=settings)
        return StreamingResponse(
            io.BytesIO(bundle),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="fyn-evidence-{agreement_id}.zip"'},
        )


@router.get("/loan-invitations/{token}", response_model=InvitationPreviewOut)
def preview_invitation(
    token: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(optional_user),
) -> InvitationPreviewOut:
    return InvitationPreviewOut.model_validate(invitation_preview(db, token, user))


@router.post("/loan-invitations/{token}/redeem", response_model=LoanCommandOut)
def redeem_invite(
    token: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> LoanCommandOut:
    with _translated_errors(db):
        agreement = redeem_loan_invitation(db, raw_token=token, user=user)
        output = _command(db, agreement, user, replayed=False)
        db.commit()
        return output


@router.post("/loan-agreements/{agreement_id}/accept", response_model=LoanCommandOut)
def accept_terms(
    agreement_id: UUID,
    payload: ConfirmLoanPaymentIn,
    http_request: Request,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> LoanCommandOut:
    with _translated_errors(db):
        agreement, replayed = accept_current_terms(
            db,
            agreement_id=agreement_id,
            user=user,
            expected_row_version=payload.expected_row_version,
            idempotency_key=idempotency_key,
            request_ip_hash=_request_hash(http_request.client.host if http_request.client else None, settings),
            user_agent_hash=_request_hash(http_request.headers.get("user-agent"), settings),
        )
        output = _command(db, agreement, user, replayed=replayed)
        db.commit()
        return output


@router.post("/loan-agreements/{agreement_id}/document-requests/fulfill", response_model=LoanCommandOut)
def fulfill_requested_documents(
    agreement_id: UUID,
    payload: FulfillDocumentRequestsIn,
    http_request: Request,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> LoanCommandOut:
    with _translated_errors(db):
        agreement, replayed = fulfill_document_requests(
            db,
            agreement_id=agreement_id,
            user=user,
            request=payload,
            idempotency_key=idempotency_key,
            settings=settings,
            request_ip_hash=_request_hash(http_request.client.host if http_request.client else None, settings),
            user_agent_hash=_request_hash(http_request.headers.get("user-agent"), settings),
        )
        output = _command(db, agreement, user, replayed=replayed)
        db.commit()
        return output


@router.get("/document-assets", response_model=list[DocumentAssetOut])
def list_document_assets(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[DocumentAssetOut]:
    return [DocumentAssetOut.model_validate(asset_dict(asset)) for asset in library_assets(db, user)]


@router.post("/document-assets", response_model=DocumentAssetOut, status_code=status.HTTP_201_CREATED)
def upload_document_asset(
    file: UploadFile = File(...),
    classification: str = Form("supporting_evidence"),
    description: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> DocumentAssetOut:
    with _translated_errors(db):
        asset = store_upload(
            db,
            user=user,
            upload=file,
            classification=classification,
            description=description,
            settings=settings,
        )
        output = DocumentAssetOut.model_validate(asset_dict(asset))
        db.commit()
        return output


@router.get("/document-assets/{asset_id}/download")
def download_document_asset(
    asset_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
):
    with _translated_errors(db):
        asset = readable_asset(db, asset_id, user)
        signed_url = presigned_download_url(asset, settings)
        if signed_url:
            return RedirectResponse(signed_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
        return FileResponse(stored_path(asset, settings), media_type=asset.media_type, filename=asset.original_filename)


@router.delete("/document-assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_document_asset(
    asset_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> None:
    with _translated_errors(db):
        asset = owned_draft_asset(db, asset_id, user)
        delete_draft_asset(db, asset, settings)
        db.commit()


@router.post("/loan-agreements/{agreement_id}/term-proposals", response_model=LoanCommandOut)
def amend_terms(
    agreement_id: UUID,
    request: LoanTermProposalIn,
    http_request: Request,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> LoanCommandOut:
    with _translated_errors(db):
        agreement, replayed = propose_terms(
            db,
            agreement_id=agreement_id,
            user=user,
            request=request,
            idempotency_key=idempotency_key,
            request_ip_hash=_request_hash(http_request.client.host if http_request.client else None, settings),
            user_agent_hash=_request_hash(http_request.headers.get("user-agent"), settings),
        )
        output = _command(db, agreement, user, replayed=replayed)
        db.commit()
        return output


@router.post("/loan-agreements/{agreement_id}/payments", response_model=LoanCommandOut)
def add_payment(
    agreement_id: UUID,
    request: RecordLoanPaymentIn,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> LoanCommandOut:
    with _translated_errors(db):
        cashflow, replayed = record_payment(
            db,
            agreement_id=agreement_id,
            user=user,
            request=request,
            idempotency_key=idempotency_key,
        )
        agreement = db.get(PersonalLoanAgreement, cashflow.agreement_id)
        if agreement is None:
            raise SharedRecordNotFound("Loan not found")
        output = _command(db, agreement, user, replayed=replayed)
        db.commit()
        return output


@router.post("/loan-agreements/{agreement_id}/funding", response_model=LoanCommandOut)
def add_funding(
    agreement_id: UUID,
    request: RecordLoanFundingIn,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> LoanCommandOut:
    with _translated_errors(db):
        cashflow, replayed = record_funding(
            db,
            agreement_id=agreement_id,
            user=user,
            request=request,
            idempotency_key=idempotency_key,
        )
        agreement = db.get(PersonalLoanAgreement, cashflow.agreement_id)
        if agreement is None:
            raise SharedRecordNotFound("Loan not found")
        output = _command(db, agreement, user, replayed=replayed)
        db.commit()
        return output


@router.post("/loan-cashflows/{cashflow_id}/confirm", response_model=LoanCommandOut)
def confirm_cashflow(
    cashflow_id: UUID,
    request: ConfirmLoanPaymentIn,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> LoanCommandOut:
    with _translated_errors(db):
        agreement, replayed = confirm_payment(
            db,
            cashflow_id=cashflow_id,
            user=user,
            expected_row_version=request.expected_row_version,
            idempotency_key=idempotency_key,
        )
        output = _command(db, agreement, user, replayed=replayed)
        db.commit()
        return output


@router.post("/loan-agreements/{agreement_id}/reminders", response_model=ReminderOut)
def remind(
    agreement_id: UUID,
    request: SendLoanReminderIn,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ReminderOut:
    with _translated_errors(db):
        reminder, outbox, masked, _replayed = send_reminder(
            db,
            agreement_id=agreement_id,
            user=user,
            request=request,
            idempotency_key=idempotency_key,
        )
        output = ReminderOut.model_validate({
            "id": reminder.id,
            "state": outbox.state,
            "channel": outbox.channel,
            "destinationMasked": masked,
            "queuedAt": outbox.created_at,
        })
        db.commit()
        return output


@router.post("/loan-agreements/{agreement_id}/close", response_model=LoanCommandOut)
def close(
    agreement_id: UUID,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> LoanCommandOut:
    with _translated_errors(db):
        agreement, replayed = close_loan(
            db,
            agreement_id=agreement_id,
            user=user,
            idempotency_key=idempotency_key,
        )
        output = _command(db, agreement, user, replayed=replayed)
        db.commit()
        return output


@router.get("/shared-documents/{document_id}/revisions", response_model=list[DocumentRevisionOut])
def document_revisions(
    document_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[DocumentRevisionOut]:
    with _translated_errors(db):
        document = db.scalar(select(SharedDocument).where(SharedDocument.id == document_id))
        if document is None:
            raise SharedRecordNotFound("Document not found")
        record_for_user(db, document.shared_record_id, user.id)
        revisions = list(db.scalars(
            select(DocumentRevision)
            .where(DocumentRevision.document_id == document.id)
            .order_by(DocumentRevision.revision_number.desc())
        ))
        return [DocumentRevisionOut.model_validate(_document_dict(db, revision)) for revision in revisions]


@router.get("/loan-agreements/{agreement_id}/integrity")
def loan_integrity(
    agreement_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    with _translated_errors(db):
        agreement = db.scalar(select(PersonalLoanAgreement).where(PersonalLoanAgreement.id == agreement_id))
        if agreement is None:
            raise SharedRecordNotFound("Loan not found")
        record_for_user(db, agreement.shared_record_id, user.id)
        return verify_projection_integrity(db, agreement_id)

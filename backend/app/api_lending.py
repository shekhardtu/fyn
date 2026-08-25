"""HTTP boundary for collaborative personal lending.

The route layer stays deliberately small: transaction control, authentication,
contract validation, and translation from domain errors to stable HTTP answers.
All financial and document rules live in reusable service modules.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import get_db
from .lending_schemas import (
    ConfirmLoanPaymentIn,
    CreatePersonalLoanIn,
    DocumentRevisionOut,
    InvitationPreviewOut,
    LoanCommandOut,
    LoanTermProposalIn,
    PersonalLoanDetailOut,
    PersonalLoanListOut,
    RecordLoanPaymentIn,
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
    invitation_preview,
    list_personal_loans,
    propose_terms,
    record_payment,
    redeem_loan_invitation,
    send_reminder,
    summary_payload,
    verify_projection_integrity,
)
from .services.shared_records import (
    SharedRecordConflict,
    SharedRecordError,
    SharedRecordNotFound,
    record_for_user,
)


router = APIRouter(tags=["personal-lending"])
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
    except (PersonalLoanError, SharedRecordError) as error:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That change was already applied or the shared record changed. Refresh and try again.",
        ) from error


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
    return PersonalLoanListOut.model_validate({
        "moneyIGaveMinor": sum(item["outstandingPrincipalMinor"] for item in items if item["direction"] == "lent"),
        "moneyIReceivedMinor": sum(item["outstandingPrincipalMinor"] for item in items if item["direction"] == "borrowed"),
        "needsResponseCount": sum(1 for item in items if item["responseNeeded"]),
        "items": items,
    })


@router.post("/loan-agreements", response_model=LoanCommandOut, status_code=status.HTTP_201_CREATED)
def create_loan(
    request: CreatePersonalLoanIn,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> LoanCommandOut:
    with _translated_errors(db):
        agreement, token, replayed = create_personal_loan(
            db,
            user=user,
            request=request,
            idempotency_key=idempotency_key,
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
    request: ConfirmLoanPaymentIn,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> LoanCommandOut:
    with _translated_errors(db):
        agreement, replayed = accept_current_terms(
            db,
            agreement_id=agreement_id,
            user=user,
            expected_row_version=request.expected_row_version,
            idempotency_key=idempotency_key,
        )
        output = _command(db, agreement, user, replayed=replayed)
        db.commit()
        return output


@router.post("/loan-agreements/{agreement_id}/term-proposals", response_model=LoanCommandOut)
def amend_terms(
    agreement_id: UUID,
    request: LoanTermProposalIn,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> LoanCommandOut:
    with _translated_errors(db):
        agreement, replayed = propose_terms(
            db,
            agreement_id=agreement_id,
            user=user,
            request=request,
            idempotency_key=idempotency_key,
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

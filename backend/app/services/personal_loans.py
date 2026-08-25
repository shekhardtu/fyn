"""Deterministic personal-loan aggregate composed from reusable modules."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..domain import IdentityProvider, OtpChannel
from ..event_time import as_utc, now_utc
from ..lending_schemas import (
    CreatePersonalLoanIn,
    FulfillDocumentRequestsIn,
    RecordLoanFundingIn,
    LoanTermProposalIn,
    RecordLoanPaymentIn,
    SendLoanReminderIn,
)
from ..models import (
    DocumentAcceptance,
    DocumentAsset,
    DocumentChange,
    DocumentRequest,
    DocumentRevision,
    Loan,
    LoanCashflow,
    LoanReminder,
    LoanSecurityItem,
    LoanTermVersion,
    NotificationOutbox,
    PersonalLoanAgreement,
    SharedDocument,
    SharedRecord,
    SharedRecordEvent,
    SharedRecordInvitation,
    SharedRecordParticipant,
    User,
    UserIdentity,
)
from .documents import accept_revision, create_document, create_revision, loan_document_content
from .document_assets import asset_dict, attach_draft_assets, carry_forward_revision_assets, revision_assets
from .identity import IdentityError, normalize_channel_value
from .shared_records import (
    SharedRecordConflict,
    SharedRecordError,
    SharedRecordNotFound,
    append_event,
    begin_command,
    decrypt_destination,
    finish_command,
    invitation_for_token,
    issue_invitation,
    other_participant,
    payload_hash,
    queue_notification,
    record_for_user,
    redeem_invitation,
    user_controls_invitation_destination,
)


class PersonalLoanError(SharedRecordError):
    pass


def _interest_metadata(interest_rate_bps: int, interest_period: str, interest_mode: str) -> tuple[str, str, str]:
    if interest_rate_bps == 0:
        return "none", "not_applicable", "half_up_minor_unit"
    basis = "fixed_30_day_month" if interest_period == "monthly" else "actual_365"
    return f"{interest_mode}_{interest_period}", basis, "half_up_minor_unit"


def _annualized_rate_bps(interest_rate_bps: int, interest_period: str) -> int:
    return interest_rate_bps * 12 if interest_period == "monthly" else interest_rate_bps


def _interest(principal_minor: int, interest_rate_bps: int, interest_period: str, interest_mode: str, money_date: date, due_date: date) -> int:
    if interest_rate_bps == 0 or due_date <= money_date:
        return 0
    days = (due_date - money_date).days
    period_days = 30 if interest_period == "monthly" else 365
    periodic_rate = Decimal(interest_rate_bps) / Decimal(10_000)
    if interest_mode == "compound":
        full_periods, remaining_days = divmod(days, period_days)
        factor = (Decimal(1) + periodic_rate) ** full_periods
        factor *= Decimal(1) + periodic_rate * Decimal(remaining_days) / Decimal(period_days)
        value = Decimal(principal_minor) * (factor - Decimal(1))
    else:
        value = Decimal(principal_minor) * periodic_rate * Decimal(days) / Decimal(period_days)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _term_payload(
    *,
    principal_minor: int,
    currency: str,
    money_date: date,
    due_date: date,
    interest_rate_bps: int,
    interest_period: str,
    interest_mode: str,
    note: str | None,
) -> dict[str, Any]:
    total_interest = _interest(principal_minor, interest_rate_bps, interest_period, interest_mode, money_date, due_date)
    interest_method, calculation_basis, rounding_policy = _interest_metadata(interest_rate_bps, interest_period, interest_mode)
    return {
        "principalMinor": principal_minor,
        "currency": currency,
        "moneyDate": money_date.isoformat(),
        "dueDate": due_date.isoformat(),
        "interestRateBps": interest_rate_bps,
        "interestPeriod": interest_period,
        "interestMode": interest_mode,
        "annualizedRateBps": _annualized_rate_bps(interest_rate_bps, interest_period),
        "interestMethod": interest_method,
        "calculationBasis": calculation_basis,
        "roundingPolicy": rounding_policy,
        "totalInterestMinor": total_interest,
        "totalRepayableMinor": principal_minor + total_interest,
        "note": note,
    }


def _participants(db: Session, shared_record_id: UUID) -> list[SharedRecordParticipant]:
    return list(db.scalars(
        select(SharedRecordParticipant)
        .where(SharedRecordParticipant.shared_record_id == shared_record_id)
        .order_by(SharedRecordParticipant.role)
    ))


def _party_by_role(participants: list[SharedRecordParticipant], role: str) -> SharedRecordParticipant:
    party = next((item for item in participants if item.role == role), None)
    if party is None:
        raise PersonalLoanError(f"The {role} is missing from this plan.")
    return party


def _agreement_for_record(db: Session, shared_record_id: UUID, *, lock: bool = False) -> PersonalLoanAgreement:
    statement = select(PersonalLoanAgreement).where(PersonalLoanAgreement.shared_record_id == shared_record_id)
    if lock:
        statement = statement.with_for_update()
    agreement = db.scalar(statement)
    if agreement is None:
        raise SharedRecordNotFound("Loan not found")
    return agreement


def _loan_context(
    db: Session,
    loan_id: UUID,
    user_id: UUID,
    *,
    lock: bool = False,
) -> tuple[SharedRecord, SharedRecordParticipant, PersonalLoanAgreement]:
    agreement_statement = select(PersonalLoanAgreement).where(PersonalLoanAgreement.id == loan_id)
    if lock:
        agreement_statement = agreement_statement.with_for_update()
    agreement = db.scalar(agreement_statement)
    if agreement is None:
        raise SharedRecordNotFound("Loan not found")
    record, participant = record_for_user(db, agreement.shared_record_id, user_id, lock=lock)
    return record, participant, agreement


def _current_term(db: Session, agreement: PersonalLoanAgreement) -> LoanTermVersion:
    term = db.scalar(select(LoanTermVersion).where(
        LoanTermVersion.agreement_id == agreement.id,
        LoanTermVersion.version == agreement.current_terms_version,
    ))
    if term is None:
        raise PersonalLoanError("The current repayment plan is missing.")
    return term


def _latest_term(db: Session, agreement: PersonalLoanAgreement) -> LoanTermVersion:
    term = db.scalar(
        select(LoanTermVersion)
        .where(LoanTermVersion.agreement_id == agreement.id)
        .order_by(LoanTermVersion.version.desc())
        .limit(1)
    )
    if term is None:
        raise PersonalLoanError("The repayment plan is missing.")
    return term


def _document_for_record(db: Session, shared_record_id: UUID) -> SharedDocument:
    document = db.scalar(select(SharedDocument).where(
        SharedDocument.shared_record_id == shared_record_id,
        SharedDocument.kind == "repayment_plan",
    ))
    if document is None:
        raise PersonalLoanError("The shared repayment document is missing.")
    return document


def _latest_revision(db: Session, document_id: UUID) -> DocumentRevision:
    revision = db.scalar(
        select(DocumentRevision)
        .where(DocumentRevision.document_id == document_id)
        .order_by(DocumentRevision.revision_number.desc())
        .limit(1)
    )
    if revision is None:
        raise PersonalLoanError("The document revision is missing.")
    return revision


def _confirmed_totals(db: Session, agreement_id: UUID) -> tuple[int, int, int]:
    rows = list(db.scalars(select(LoanCashflow).where(
        LoanCashflow.agreement_id == agreement_id,
        LoanCashflow.kind == "repayment",
        LoanCashflow.state == "confirmed",
    )))
    return (
        sum(item.principal_minor for item in rows),
        sum(item.interest_minor for item in rows),
        sum(item.amount_minor for item in rows),
    )


def _security_items(db: Session, agreement_id: UUID) -> list[LoanSecurityItem]:
    return list(db.scalars(
        select(LoanSecurityItem)
        .where(LoanSecurityItem.agreement_id == agreement_id)
        .order_by(LoanSecurityItem.created_at, LoanSecurityItem.id)
    ))


def _security_snapshot(items: list[LoanSecurityItem]) -> list[dict[str, Any]]:
    return [{
        "kind": item.kind,
        "description": item.description,
        "maskedIdentifier": item.masked_identifier,
        "statedValueMinor": item.stated_value_minor,
        "currency": item.currency,
    } for item in items]


def _document_requests(db: Session, shared_record_id: UUID) -> list[DocumentRequest]:
    return list(db.scalars(
        select(DocumentRequest)
        .where(DocumentRequest.shared_record_id == shared_record_id)
        .order_by(DocumentRequest.created_at, DocumentRequest.id)
    ))


def _remaining(db: Session, agreement: PersonalLoanAgreement, term: LoanTermVersion | None = None) -> tuple[int, int, int]:
    current = term or _current_term(db, agreement)
    principal_paid, interest_paid, total_paid = _confirmed_totals(db, agreement.id)
    return (
        max(current.principal_minor - principal_paid, 0),
        max(current.total_interest_minor - interest_paid, 0),
        total_paid,
    )


def _latest_event_sequence(db: Session, shared_record_id: UUID) -> int:
    return db.scalar(select(func.max(SharedRecordEvent.sequence)).where(
        SharedRecordEvent.shared_record_id == shared_record_id
    )) or 0


def _response_needed(db: Session, agreement: PersonalLoanAgreement, participant: SharedRecordParticipant) -> bool:
    latest = _latest_term(db, agreement)
    if latest.state == "proposed":
        accepted = db.scalar(select(DocumentAcceptance.id).where(
            DocumentAcceptance.revision_id == latest.document_revision_id,
            DocumentAcceptance.participant_id == participant.id,
        ))
        if accepted is None:
            return True
    pending = db.scalar(select(LoanCashflow.id).where(
        LoanCashflow.agreement_id == agreement.id,
        LoanCashflow.state == "proposed",
        LoanCashflow.initiated_by_participant_id != participant.id,
    ))
    if pending is not None:
        return True
    returned = db.scalar(select(LoanSecurityItem.id).where(
        LoanSecurityItem.agreement_id == agreement.id,
        LoanSecurityItem.state == "return_pending_confirmation",
        LoanSecurityItem.held_by_participant_id != participant.id,
    ))
    return returned is not None


def rebuild_projection_for_participant(
    db: Session,
    *,
    record: SharedRecord,
    agreement: PersonalLoanAgreement,
    participant: SharedRecordParticipant,
) -> Loan | None:
    if participant.member_user_id is None:
        return None
    participants = _participants(db, record.id)
    lender = _party_by_role(participants, "lender")
    counterparty = next(item for item in participants if item.id != participant.id)
    term = _current_term(db, agreement)
    outstanding, interest, _paid = _remaining(db, agreement, term)
    direction = "lent" if participant.role == "lender" else "borrowed"
    months = max(math.ceil(max((term.due_date - date.today()).days, 0) / 30), 0)
    projection = db.scalar(select(Loan).where(
        Loan.user_id == participant.member_user_id,
        Loan.shared_record_id == record.id,
    ))
    values = {
        "account_id": None,
        "shared_record_id": record.id,
        "name": f"{counterparty.display_name} personal loan",
        "loan_type": "personal_peer",
        "lender": lender.display_name,
        "direction": direction,
        "counterparty_name": counterparty.display_name,
        "outstanding_principal_minor": outstanding,
        "accrued_interest_minor": interest,
        "annual_rate_percent": Decimal(_annualized_rate_bps(term.interest_rate_bps, term.interest_period)) / Decimal(100),
        "rate_type": "fixed",
        "remaining_tenure_months": months,
        "current_emi_minor": outstanding + interest if outstanding + interest else None,
        "next_due_date": term.due_date if outstanding + interest else None,
        "next_due_minor": outstanding + interest if outstanding + interest else None,
        "response_needed": _response_needed(db, agreement, participant),
        "last_projected_event_sequence": _latest_event_sequence(db, record.id),
        "prepayment_fee_percent": Decimal("0"),
        "status": agreement.status,
        "currency": agreement.currency,
    }
    if projection is None:
        projection = Loan(user_id=participant.member_user_id, **values)
        db.add(projection)
    else:
        for key, value in values.items():
            setattr(projection, key, value)
    db.flush()
    return projection


def rebuild_projections(db: Session, record: SharedRecord, agreement: PersonalLoanAgreement) -> None:
    for participant in _participants(db, record.id):
        rebuild_projection_for_participant(db, record=record, agreement=agreement, participant=participant)


def _preferred_delivery(db: Session, participant: SharedRecordParticipant) -> tuple[OtpChannel, str, str]:
    invitation = db.scalar(
        select(SharedRecordInvitation)
        .where(SharedRecordInvitation.participant_id == participant.id)
        .order_by(SharedRecordInvitation.created_at.desc())
        .limit(1)
    )
    if invitation is not None:
        return OtpChannel(invitation.channel), decrypt_destination(invitation.destination_ciphertext), invitation.destination_masked
    if participant.member_user_id is None:
        raise PersonalLoanError("That person has no verified reminder channel.")
    identity = db.scalar(
        select(UserIdentity)
        .where(
            UserIdentity.user_id == participant.member_user_id,
            UserIdentity.provider.in_((IdentityProvider.EMAIL.value, IdentityProvider.PHONE.value)),
        )
        .order_by(UserIdentity.provider)
        .limit(1)
    )
    if identity is None:
        raise PersonalLoanError("That person has no verified reminder channel.")
    channel = OtpChannel.EMAIL if identity.provider == IdentityProvider.EMAIL.value else OtpChannel.PHONE
    from .identity import mask
    return channel, identity.identifier, mask(channel, identity.identifier)


def create_personal_loan(
    db: Session,
    *,
    user: User,
    request: CreatePersonalLoanIn,
    idempotency_key: str,
    settings: Settings,
    request_ip_hash: str | None = None,
    user_agent_hash: str | None = None,
) -> tuple[PersonalLoanAgreement, str | None, bool]:
    if user.display_name.strip().casefold() == "you":
        raise PersonalLoanError("Add your real display name in Profile before creating a shared agreement.")
    request_data = request.model_dump(mode="json")
    prior = begin_command(
        db,
        actor_user_id=user.id,
        command_type="personal_loan.create",
        idempotency_key=idempotency_key,
        request_payload=request_data,
    )
    if prior is not None:
        agreement = db.get(PersonalLoanAgreement, UUID(prior.response_payload["loanId"]))
        if agreement is None:
            raise PersonalLoanError("The previously created loan is no longer available.")
        return agreement, None, True

    invitation_channel = OtpChannel(request.invite_channel)
    try:
        invitation_destination = normalize_channel_value(invitation_channel, request.invite_value).key
    except IdentityError as error:
        raise PersonalLoanError(str(error)) from error
    mirrored_identifier = user.email if invitation_channel is OtpChannel.EMAIL else user.phone
    if mirrored_identifier is not None:
        try:
            if normalize_channel_value(invitation_channel, mirrored_identifier).key == invitation_destination:
                raise PersonalLoanError("Choose someone else’s email address or phone number for this shared plan.")
        except IdentityError:
            # Legacy mirror columns may contain a stale value; the verified
            # identity table below remains authoritative in that case.
            pass
    own_identifier = db.scalar(select(UserIdentity.id).where(
        UserIdentity.user_id == user.id,
        UserIdentity.provider == invitation_channel.value,
        UserIdentity.identifier == invitation_destination,
    ))
    if own_identifier is not None:
        raise PersonalLoanError("Choose someone else’s email address or phone number for this shared plan.")

    record = SharedRecord(kind="personal_loan", status="pending_acceptance", created_by_user_id=user.id, row_version=1)
    db.add(record)
    db.flush()

    own_role, counterparty_role = ("lender", "borrower") if request.direction == "lent" else ("borrower", "lender")
    own = SharedRecordParticipant(
        shared_record_id=record.id,
        member_user_id=user.id,
        role=own_role,
        display_name=user.display_name,
        state="accepted",
        verification_claim="fyn_account",
        claimed_at=now_utc(),
    )
    counterparty = SharedRecordParticipant(
        shared_record_id=record.id,
        role=counterparty_role,
        display_name=request.counterparty_name,
        state="invited",
    )
    db.add_all([own, counterparty])
    db.flush()

    intent = request.intent or ("record_given" if request.direction == "lent" else "record_received")
    if request.direction == "lent" and intent not in {"record_given", "offer_to_lend"}:
        raise PersonalLoanError("That intent does not match the selected lending direction.")
    if request.direction == "borrowed" and intent not in {"record_received", "request_to_borrow"}:
        raise PersonalLoanError("That intent does not match the selected borrowing direction.")
    agreement = PersonalLoanAgreement(
        shared_record_id=record.id,
        currency=request.currency,
        status="pending_acceptance",
        funding_status="pending_confirmation",
        intent=intent,
        current_terms_version=1,
    )
    db.add(agreement)
    db.flush()

    lender = own if own_role == "lender" else counterparty
    borrower = own if own_role == "borrower" else counterparty
    if request.document_requests and own_role != "lender":
        raise PersonalLoanError("Only the lender can require documents from the borrower. You can share your own repository documents instead.")
    requested_documents = [DocumentRequest(
        shared_record_id=record.id,
        requested_by_participant_id=lender.id,
        requested_from_participant_id=borrower.id,
        label=item.label,
        classification=item.classification,
        instructions=item.instructions,
        required=item.required,
        state="requested",
    ) for item in request.document_requests]
    db.add_all(requested_documents)
    assurance_items = [LoanSecurityItem(
        agreement_id=agreement.id,
        kind=item.kind,
        description=item.description,
        masked_identifier=item.masked_identifier,
        stated_value_minor=item.stated_value_minor,
        provided_by_participant_id=borrower.id,
        held_by_participant_id=lender.id,
        state="proposed",
        currency=request.currency,
    ) for item in request.security_items]
    db.add_all(assurance_items)
    db.flush()

    term_data = _term_payload(
        principal_minor=request.principal_minor,
        currency=request.currency,
        money_date=request.money_date,
        due_date=request.due_date,
        interest_rate_bps=request.interest_rate_bps,
        interest_period=request.interest_period,
        interest_mode=request.interest_mode,
        note=request.note,
    )
    term_data["securityItems"] = _security_snapshot(assurance_items)
    source_hash = payload_hash(term_data)
    term = LoanTermVersion(
        agreement_id=agreement.id,
        version=1,
        principal_minor=request.principal_minor,
        interest_rate_bps=request.interest_rate_bps,
        interest_period=request.interest_period,
        interest_mode=request.interest_mode,
        interest_method=term_data["interestMethod"],
        calculation_basis=term_data["calculationBasis"],
        rounding_policy=term_data["roundingPolicy"],
        money_date=request.money_date,
        due_date=request.due_date,
        note=request.note,
        schedule=[{
            "sequence": 1,
            "dueDate": request.due_date.isoformat(),
            "principalMinor": request.principal_minor,
            "interestMinor": term_data["totalInterestMinor"],
            "totalMinor": term_data["totalRepayableMinor"],
        }],
        total_interest_minor=term_data["totalInterestMinor"],
        total_repayable_minor=term_data["totalRepayableMinor"],
        state="proposed",
        proposed_by_participant_id=own.id,
        source_hash=source_hash,
        currency=request.currency,
    )
    db.add(term)
    db.flush()

    document = create_document(
        db,
        shared_record_id=record.id,
        kind="repayment_plan",
        title="Shared repayment plan",
        template_key="personal_loan_acknowledgement",
    )
    content = loan_document_content(
        lender_name=lender.display_name,
        borrower_name=borrower.display_name,
        principal_minor=term.principal_minor,
        currency=term.currency,
        money_date=term.money_date.isoformat(),
        due_date=term.due_date.isoformat(),
        interest_rate_bps=term.interest_rate_bps,
        interest_period=term.interest_period,
        interest_mode=term.interest_mode,
        interest_method=term.interest_method,
        calculation_basis=term.calculation_basis,
        rounding_policy=term.rounding_policy,
        total_interest_minor=term.total_interest_minor,
        total_repayable_minor=term.total_repayable_minor,
        note=term.note,
        security_items=_security_snapshot(assurance_items),
    )
    revision = create_revision(
        db,
        document=document,
        author=own,
        content=content,
        source_snapshot_hash=source_hash,
    )
    term.document_revision_id = revision.id
    attach_draft_assets(
        db,
        document=document,
        revision=revision,
        participant=own,
        user=user,
        asset_ids=request.asset_ids,
        settings=settings,
    )
    from .identity import mask
    creator_identifier = user.email or user.phone
    creator_channel = OtpChannel.EMAIL if user.email else OtpChannel.PHONE
    accept_revision(
        db,
        document=document,
        revision=revision,
        participant=own,
        actor_user_id=user.id,
        actor_identifier_masked=mask(creator_channel, creator_identifier) if creator_identifier else None,
        actor_timezone=user.timezone,
        request_ip_hash=request_ip_hash,
        user_agent_hash=user_agent_hash,
    )

    if intent in {"record_given", "record_received"}:
        db.add(LoanCashflow(
            agreement_id=agreement.id,
            kind="disbursement",
            state="proposed",
            amount_minor=term.principal_minor,
            principal_minor=term.principal_minor,
            interest_minor=0,
            occurred_on=term.money_date,
            initiated_by_participant_id=own.id,
            currency=term.currency,
            note="Recorded when the shared plan was created",
        ))
    invitation, raw_token = issue_invitation(
        db,
        record=record,
        participant=counterparty,
        channel=invitation_channel,
        raw_destination=invitation_destination,
    )
    destination = decrypt_destination(invitation.destination_ciphertext)
    queue_notification(
        db,
        record=record,
        recipient=counterparty,
        topic="shared_record.invitation",
        channel=OtpChannel(invitation.channel),
        destination=destination,
        payload={
            "senderName": user.display_name,
            "recipientName": counterparty.display_name,
            "recordKind": "personal_loan",
        },
        dedupe_key=f"loan-invite:{invitation.id}:1",
        secret_context=raw_token,
    )
    append_event(db, record, "loan.created", actor_participant_id=own.id, payload={
        "principalMinor": term.principal_minor,
        "currency": term.currency,
        "counterpartyName": counterparty.display_name,
    })
    append_event(db, record, "document.proposed", actor_participant_id=own.id, payload={
        "revisionId": str(revision.id),
        "revisionNumber": revision.revision_number,
        "contentHash": revision.content_hash,
        "manifestHash": revision.manifest_hash,
        "evidenceHash": revision.evidence_hash,
        "assetCount": len(request.asset_ids),
    })
    if requested_documents:
        append_event(db, record, "document.evidence_requested", actor_participant_id=own.id, payload={
            "requestCount": len(requested_documents),
            "requiredCount": sum(1 for item in requested_documents if item.required),
        })
    append_event(db, record, "invitation.queued", actor_participant_id=own.id, payload={
        "invitationId": str(invitation.id),
        "channel": invitation.channel,
        "destinationMasked": invitation.destination_masked,
    })
    rebuild_projection_for_participant(db, record=record, agreement=agreement, participant=own)
    finish_command(
        db,
        record=record,
        actor_user_id=user.id,
        command_type="personal_loan.create",
        idempotency_key=idempotency_key,
        request_payload=request_data,
        response_payload={"loanId": str(agreement.id)},
    )
    return agreement, raw_token, False


def redeem_loan_invitation(db: Session, *, raw_token: str, user: User) -> PersonalLoanAgreement:
    _invitation, participant, record = redeem_invitation(db, raw_token=raw_token, user=user)
    agreement = _agreement_for_record(db, record.id, lock=True)
    rebuild_projections(db, record, agreement)
    return agreement


def invitation_preview(db: Session, raw_token: str, user: User | None) -> dict[str, Any]:
    invitation = invitation_for_token(db, raw_token)
    if (
        invitation is None
        or invitation.revoked_at is not None
        or as_utc(invitation.expires_at) <= now_utc()
    ):
        return {"tokenValid": False, "canRedeem": False}
    participant = db.scalar(select(SharedRecordParticipant).where(SharedRecordParticipant.id == invitation.participant_id))
    record = db.scalar(select(SharedRecord).where(SharedRecord.id == invitation.shared_record_id))
    if participant is None or record is None:
        return {"tokenValid": False, "canRedeem": False}
    creator = db.get(User, record.created_by_user_id) if record.created_by_user_id else None
    return {
        "tokenValid": True,
        "senderName": creator.display_name if creator else "Someone you know",
        "recipientName": participant.display_name,
        "channel": invitation.channel,
        "destinationMasked": invitation.destination_masked,
        "expiresAt": invitation.expires_at,
        "canRedeem": bool(user and user_controls_invitation_destination(db, user, invitation)),
        "loan": None,
    }


def accept_current_terms(
    db: Session,
    *,
    agreement_id: UUID,
    user: User,
    expected_row_version: int,
    idempotency_key: str,
    request_ip_hash: str | None = None,
    user_agent_hash: str | None = None,
) -> tuple[PersonalLoanAgreement, bool]:
    record, participant, agreement = _loan_context(db, agreement_id, user.id, lock=True)
    request_data = {"expectedRowVersion": expected_row_version}
    prior = begin_command(db, actor_user_id=user.id, command_type="personal_loan.accept_terms", idempotency_key=idempotency_key, request_payload=request_data)
    if prior is not None:
        return agreement, True
    if record.row_version != expected_row_version:
        raise SharedRecordConflict("This plan changed. Review the latest version before agreeing.")
    latest = _latest_term(db, agreement)
    if latest.state != "proposed" or latest.document_revision_id is None:
        raise SharedRecordConflict("There is no repayment-plan change awaiting your agreement.")
    missing_required_documents = db.scalar(select(func.count()).select_from(DocumentRequest).where(
        DocumentRequest.shared_record_id == record.id,
        DocumentRequest.requested_from_participant_id == participant.id,
        DocumentRequest.required.is_(True),
        DocumentRequest.state == "requested",
    )) or 0
    if missing_required_documents:
        raise PersonalLoanError("Provide the required documents before acknowledging this agreement.")
    document = _document_for_record(db, record.id)
    revision = db.scalar(select(DocumentRevision).where(DocumentRevision.id == latest.document_revision_id).with_for_update())
    if revision is None or revision.source_snapshot_hash != latest.source_hash:
        raise SharedRecordConflict("The document does not match the proposed financial terms.")
    from .identity import mask
    actor_identifier = user.email or user.phone
    actor_channel = OtpChannel.EMAIL if user.email else OtpChannel.PHONE
    _acceptance, finalized = accept_revision(
        db,
        document=document,
        revision=revision,
        participant=participant,
        actor_user_id=user.id,
        actor_identifier_masked=mask(actor_channel, actor_identifier) if actor_identifier else None,
        actor_timezone=user.timezone,
        request_ip_hash=request_ip_hash,
        user_agent_hash=user_agent_hash,
    )
    participant.state = "accepted"
    append_event(db, record, "document.accepted", actor_participant_id=participant.id, payload={
        "revisionId": str(revision.id),
        "revisionNumber": revision.revision_number,
        "contentHash": revision.content_hash,
        "manifestHash": revision.manifest_hash,
        "evidenceHash": revision.evidence_hash,
    })
    if finalized:
        previous = _current_term(db, agreement)
        if previous.id != latest.id:
            previous.state = "superseded"
        latest.state = "accepted"
        latest.effective_at = now_utc()
        agreement.current_terms_version = latest.version
        recorded_funding = agreement.intent in {"record_given", "record_received"}
        agreement.status = "active" if recorded_funding else "funding_pending"
        record.status = agreement.status
        disbursement = db.scalar(select(LoanCashflow).where(
            LoanCashflow.agreement_id == agreement.id,
            LoanCashflow.kind == "disbursement",
        ).with_for_update())
        if disbursement is not None and disbursement.state == "proposed":
            disbursement.state = "confirmed"
            disbursement.confirmed_by_participant_id = participant.id
            disbursement.confirmed_at = now_utc()
            agreement.funding_status = "confirmed"
        for item in _security_items(db, agreement.id):
            if item.state == "proposed":
                item.state = "acknowledged"
        append_event(db, record, "loan.terms_activated", actor_participant_id=participant.id, payload={
            "termsVersion": latest.version,
            "dueDate": latest.due_date.isoformat(),
            "totalRepayableMinor": latest.total_repayable_minor,
        })
    record.row_version += 1
    rebuild_projections(db, record, agreement)
    finish_command(
        db,
        record=record,
        actor_user_id=user.id,
        command_type="personal_loan.accept_terms",
        idempotency_key=idempotency_key,
        request_payload=request_data,
        response_payload={"loanId": str(agreement.id)},
    )
    return agreement, False


def fulfill_document_requests(
    db: Session,
    *,
    agreement_id: UUID,
    user: User,
    request: FulfillDocumentRequestsIn,
    idempotency_key: str,
    settings: Settings,
    request_ip_hash: str | None = None,
    user_agent_hash: str | None = None,
) -> tuple[PersonalLoanAgreement, bool]:
    """Bind borrower-selected library files to one new immutable revision.

    Private library assets remain private. ``attach_draft_assets`` copies each
    selected object into the shared document before the borrower acknowledges
    that exact evidence manifest. The lender must then review and acknowledge
    the replacement revision independently.
    """
    record, participant, agreement = _loan_context(db, agreement_id, user.id, lock=True)
    request_data = request.model_dump(mode="json")
    prior = begin_command(
        db,
        actor_user_id=user.id,
        command_type="personal_loan.fulfill_document_requests",
        idempotency_key=idempotency_key,
        request_payload=request_data,
    )
    if prior is not None:
        return agreement, True
    if record.row_version != request.expected_row_version:
        raise SharedRecordConflict("This agreement changed. Review the latest document requests before continuing.")
    if participant.role != "borrower":
        raise PersonalLoanError("Only the borrower can provide documents requested from the borrower.")

    requested = {
        item.id: item
        for item in _document_requests(db, record.id)
        if item.requested_from_participant_id == participant.id and item.state == "requested"
    }
    item_ids = [item.request_id for item in request.items]
    asset_ids = [item.asset_id for item in request.items]
    if len(item_ids) != len(set(item_ids)):
        raise PersonalLoanError("Choose one document for each request.")
    if len(asset_ids) != len(set(asset_ids)):
        raise PersonalLoanError("Choose a different document for each request.")
    if any(item_id not in requested for item_id in item_ids):
        raise SharedRecordConflict("One of these document requests is no longer awaiting a response.")
    missing_required = [item for item in requested.values() if item.required and item.id not in set(item_ids)]
    if missing_required:
        raise PersonalLoanError("Provide every required document together so both people review one exact revision.")

    latest = _latest_term(db, agreement)
    if latest.state == "proposed" and latest.version != agreement.current_terms_version:
        raise SharedRecordConflict("A repayment-plan change is already awaiting a response.")
    current = _current_term(db, agreement)
    document = _document_for_record(db, record.id)
    base_revision = _latest_revision(db, document.id)
    parties = _participants(db, record.id)
    lender = _party_by_role(parties, "lender")
    borrower = _party_by_role(parties, "borrower")
    assurance_items = _security_items(db, agreement.id)

    version = latest.version + 1
    proposed = LoanTermVersion(
        agreement_id=agreement.id,
        version=version,
        principal_minor=current.principal_minor,
        interest_rate_bps=current.interest_rate_bps,
        interest_period=current.interest_period,
        interest_mode=current.interest_mode,
        interest_method=current.interest_method,
        calculation_basis=current.calculation_basis,
        rounding_policy=current.rounding_policy,
        money_date=current.money_date,
        due_date=current.due_date,
        note=current.note,
        schedule=current.schedule,
        total_interest_minor=current.total_interest_minor,
        total_repayable_minor=current.total_repayable_minor,
        state="proposed",
        proposed_by_participant_id=participant.id,
        source_hash=current.source_hash,
        currency=current.currency,
    )
    db.add(proposed)
    db.flush()
    revision = create_revision(
        db,
        document=document,
        author=participant,
        content=loan_document_content(
            lender_name=lender.display_name,
            borrower_name=borrower.display_name,
            principal_minor=proposed.principal_minor,
            currency=proposed.currency,
            money_date=proposed.money_date.isoformat(),
            due_date=proposed.due_date.isoformat(),
            interest_rate_bps=proposed.interest_rate_bps,
            interest_period=proposed.interest_period,
            interest_mode=proposed.interest_mode,
            interest_method=proposed.interest_method,
            calculation_basis=proposed.calculation_basis,
            rounding_policy=proposed.rounding_policy,
            total_interest_minor=proposed.total_interest_minor,
            total_repayable_minor=proposed.total_repayable_minor,
            note=proposed.note,
            security_items=_security_snapshot(assurance_items),
        ),
        source_snapshot_hash=proposed.source_hash,
        base_revision=base_revision,
    )
    carry_forward_revision_assets(db, base_revision_id=base_revision.id, revision=revision)
    bound_assets = attach_draft_assets(
        db,
        document=document,
        revision=revision,
        participant=participant,
        user=user,
        asset_ids=asset_ids,
        settings=settings,
    )
    bound_by_source = dict(zip(asset_ids, bound_assets))
    for item in request.items:
        document_request = requested[item.request_id]
        document_request.state = "fulfilled"
        document_request.fulfilled_asset_id = bound_by_source[item.asset_id].id
        document_request.fulfilled_revision_id = revision.id
        document_request.fulfilled_at = now_utc()
    proposed.document_revision_id = revision.id

    from .identity import mask
    actor_identifier = user.email or user.phone
    actor_channel = OtpChannel.EMAIL if user.email else OtpChannel.PHONE
    accept_revision(
        db,
        document=document,
        revision=revision,
        participant=participant,
        actor_user_id=user.id,
        actor_identifier_masked=mask(actor_channel, actor_identifier) if actor_identifier else None,
        actor_timezone=user.timezone,
        request_ip_hash=request_ip_hash,
        user_agent_hash=user_agent_hash,
    )
    record.row_version += 1
    append_event(db, record, "document.evidence_fulfilled", actor_participant_id=participant.id, payload={
        "requestIds": [str(item.request_id) for item in request.items],
        "revisionId": str(revision.id),
        "assetCount": len(bound_assets),
    })
    rebuild_projections(db, record, agreement)
    finish_command(
        db,
        record=record,
        actor_user_id=user.id,
        command_type="personal_loan.fulfill_document_requests",
        idempotency_key=idempotency_key,
        request_payload=request_data,
        response_payload={"loanId": str(agreement.id), "revisionId": str(revision.id)},
    )
    return agreement, False


def record_funding(
    db: Session,
    *,
    agreement_id: UUID,
    user: User,
    request: RecordLoanFundingIn,
    idempotency_key: str,
) -> tuple[LoanCashflow, bool]:
    record, participant, agreement = _loan_context(db, agreement_id, user.id, lock=True)
    request_data = request.model_dump(mode="json")
    prior = begin_command(db, actor_user_id=user.id, command_type="personal_loan.record_funding", idempotency_key=idempotency_key, request_payload=request_data)
    if prior is not None:
        cashflow = db.get(LoanCashflow, UUID(prior.response_payload["cashflowId"]))
        if cashflow is None:
            raise PersonalLoanError("The previously recorded funding is no longer available.")
        return cashflow, True
    if agreement.status != "funding_pending" or agreement.funding_status != "pending_confirmation":
        raise SharedRecordConflict("Funding can be recorded after both people accept an unfunded agreement.")
    if participant.role != "lender":
        raise PersonalLoanError("The lender records that the money was sent; the borrower confirms receipt.")
    existing = db.scalar(select(LoanCashflow).where(LoanCashflow.agreement_id == agreement.id, LoanCashflow.kind == "disbursement"))
    if existing is not None:
        raise SharedRecordConflict("Funding has already been recorded for confirmation.")
    term = _current_term(db, agreement)
    cashflow = LoanCashflow(
        agreement_id=agreement.id,
        kind="disbursement",
        state="proposed",
        amount_minor=term.principal_minor,
        principal_minor=term.principal_minor,
        interest_minor=0,
        occurred_on=request.occurred_on,
        initiated_by_participant_id=participant.id,
        note=request.note,
        currency=agreement.currency,
    )
    db.add(cashflow)
    record.row_version += 1
    db.flush()
    append_event(db, record, "funding.recorded", actor_participant_id=participant.id, payload={"cashflowId": str(cashflow.id), "amountMinor": cashflow.amount_minor, "occurredOn": cashflow.occurred_on.isoformat()})
    rebuild_projections(db, record, agreement)
    finish_command(db, record=record, actor_user_id=user.id, command_type="personal_loan.record_funding", idempotency_key=idempotency_key, request_payload=request_data, response_payload={"cashflowId": str(cashflow.id), "loanId": str(agreement.id)})
    return cashflow, False


def propose_terms(
    db: Session,
    *,
    agreement_id: UUID,
    user: User,
    request: LoanTermProposalIn,
    idempotency_key: str,
    request_ip_hash: str | None = None,
    user_agent_hash: str | None = None,
) -> tuple[PersonalLoanAgreement, bool]:
    record, participant, agreement = _loan_context(db, agreement_id, user.id, lock=True)
    request_data = request.model_dump(mode="json")
    prior = begin_command(db, actor_user_id=user.id, command_type="personal_loan.propose_terms", idempotency_key=idempotency_key, request_payload=request_data)
    if prior is not None:
        return agreement, True
    if agreement.status not in {"active", "pending_acceptance"}:
        raise SharedRecordConflict("That loan is not open for a repayment-plan change.")
    if record.row_version != request.expected_row_version:
        raise SharedRecordConflict("This plan changed. Review the latest version before proposing changes.")
    current = _current_term(db, agreement)
    if request.due_date < current.money_date:
        raise PersonalLoanError("The return date cannot be before the money date.")
    latest = _latest_term(db, agreement)
    if latest.state == "proposed" and latest.version != agreement.current_terms_version:
        raise SharedRecordConflict("A repayment-plan change is already awaiting a response.")

    version = latest.version + 1
    data = _term_payload(
        principal_minor=current.principal_minor,
        currency=current.currency,
        money_date=current.money_date,
        due_date=request.due_date,
        interest_rate_bps=request.interest_rate_bps,
        interest_period=request.interest_period,
        interest_mode=request.interest_mode,
        note=request.note,
    )
    assurance_items = _security_items(db, agreement.id)
    data["securityItems"] = _security_snapshot(assurance_items)
    source_hash = payload_hash(data)
    proposed = LoanTermVersion(
        agreement_id=agreement.id,
        version=version,
        principal_minor=current.principal_minor,
        interest_rate_bps=request.interest_rate_bps,
        interest_period=request.interest_period,
        interest_mode=request.interest_mode,
        interest_method=data["interestMethod"],
        calculation_basis=data["calculationBasis"],
        rounding_policy=data["roundingPolicy"],
        money_date=current.money_date,
        due_date=request.due_date,
        note=request.note,
        schedule=[{
            "sequence": 1,
            "dueDate": request.due_date.isoformat(),
            "principalMinor": current.principal_minor,
            "interestMinor": data["totalInterestMinor"],
            "totalMinor": data["totalRepayableMinor"],
        }],
        total_interest_minor=data["totalInterestMinor"],
        total_repayable_minor=data["totalRepayableMinor"],
        state="proposed",
        proposed_by_participant_id=participant.id,
        source_hash=source_hash,
        currency=current.currency,
    )
    db.add(proposed)
    db.flush()
    parties = _participants(db, record.id)
    lender = _party_by_role(parties, "lender")
    borrower = _party_by_role(parties, "borrower")
    document = _document_for_record(db, record.id)
    base_revision = db.scalar(select(DocumentRevision).where(
        DocumentRevision.document_id == document.id,
        DocumentRevision.revision_number == document.current_revision_number,
    ))
    if base_revision is None:
        base_revision = _latest_revision(db, document.id)
    revision = create_revision(
        db,
        document=document,
        author=participant,
        content=loan_document_content(
            lender_name=lender.display_name,
            borrower_name=borrower.display_name,
            principal_minor=proposed.principal_minor,
            currency=proposed.currency,
            money_date=proposed.money_date.isoformat(),
            due_date=proposed.due_date.isoformat(),
            interest_rate_bps=proposed.interest_rate_bps,
            interest_period=proposed.interest_period,
            interest_mode=proposed.interest_mode,
            interest_method=proposed.interest_method,
            calculation_basis=proposed.calculation_basis,
            rounding_policy=proposed.rounding_policy,
            total_interest_minor=proposed.total_interest_minor,
            total_repayable_minor=proposed.total_repayable_minor,
            note=proposed.note,
            security_items=_security_snapshot(assurance_items),
        ),
        source_snapshot_hash=source_hash,
        base_revision=base_revision,
    )
    carry_forward_revision_assets(db, base_revision_id=base_revision.id, revision=revision)
    proposed.document_revision_id = revision.id
    accept_revision(
        db,
        document=document,
        revision=revision,
        participant=participant,
        actor_user_id=user.id,
        request_ip_hash=request_ip_hash,
        user_agent_hash=user_agent_hash,
    )
    record.row_version += 1
    append_event(db, record, "loan.terms_proposed", actor_participant_id=participant.id, payload={
        "termsVersion": version,
        "revisionId": str(revision.id),
        "changeCount": len(revision.change_summary),
    })
    rebuild_projections(db, record, agreement)
    finish_command(db, record=record, actor_user_id=user.id, command_type="personal_loan.propose_terms", idempotency_key=idempotency_key, request_payload=request_data, response_payload={"loanId": str(agreement.id)})
    return agreement, False


def record_payment(
    db: Session,
    *,
    agreement_id: UUID,
    user: User,
    request: RecordLoanPaymentIn,
    idempotency_key: str,
) -> tuple[LoanCashflow, bool]:
    record, participant, agreement = _loan_context(db, agreement_id, user.id, lock=True)
    request_data = request.model_dump(mode="json")
    prior = begin_command(db, actor_user_id=user.id, command_type="personal_loan.record_payment", idempotency_key=idempotency_key, request_payload=request_data)
    if prior is not None:
        cashflow = db.get(LoanCashflow, UUID(prior.response_payload["cashflowId"]))
        if cashflow is None:
            raise PersonalLoanError("The previously recorded payment is no longer available.")
        return cashflow, True
    if agreement.status not in {"active", "settlement_pending"}:
        raise SharedRecordConflict("Payments can be recorded after both people agree to the plan.")
    term = _current_term(db, agreement)
    principal_remaining, interest_remaining, _paid = _remaining(db, agreement, term)
    total_remaining = principal_remaining + interest_remaining
    if request.amount_minor > total_remaining:
        raise PersonalLoanError(f"The payment is more than the remaining amount of {total_remaining} minor units.")
    interest = min(request.amount_minor, interest_remaining)
    principal = request.amount_minor - interest
    cashflow = LoanCashflow(
        agreement_id=agreement.id,
        kind="repayment",
        state="proposed",
        amount_minor=request.amount_minor,
        principal_minor=principal,
        interest_minor=interest,
        occurred_on=request.occurred_on,
        initiated_by_participant_id=participant.id,
        note=request.note,
        currency=agreement.currency,
    )
    db.add(cashflow)
    record.row_version += 1
    db.flush()
    append_event(db, record, "payment.recorded", actor_participant_id=participant.id, payload={
        "cashflowId": str(cashflow.id),
        "amountMinor": cashflow.amount_minor,
        "occurredOn": cashflow.occurred_on.isoformat(),
    })
    rebuild_projections(db, record, agreement)
    finish_command(db, record=record, actor_user_id=user.id, command_type="personal_loan.record_payment", idempotency_key=idempotency_key, request_payload=request_data, response_payload={"cashflowId": str(cashflow.id), "loanId": str(agreement.id)})
    return cashflow, False


def confirm_payment(
    db: Session,
    *,
    cashflow_id: UUID,
    user: User,
    expected_row_version: int,
    idempotency_key: str,
) -> tuple[PersonalLoanAgreement, bool]:
    cashflow = db.scalar(select(LoanCashflow).where(LoanCashflow.id == cashflow_id).with_for_update())
    if cashflow is None:
        raise SharedRecordNotFound("Payment not found")
    agreement = db.scalar(select(PersonalLoanAgreement).where(PersonalLoanAgreement.id == cashflow.agreement_id).with_for_update())
    if agreement is None:
        raise SharedRecordNotFound("Payment not found")
    record, participant = record_for_user(db, agreement.shared_record_id, user.id, lock=True)
    request_data = {"cashflowId": str(cashflow_id), "expectedRowVersion": expected_row_version}
    prior = begin_command(db, actor_user_id=user.id, command_type="personal_loan.confirm_payment", idempotency_key=idempotency_key, request_payload=request_data)
    if prior is not None:
        return agreement, True
    if record.row_version != expected_row_version:
        raise SharedRecordConflict("This payment record changed. Review it again before confirming.")
    if cashflow.state == "confirmed":
        raise SharedRecordConflict("That payment is already confirmed.")
    if cashflow.state != "proposed":
        raise SharedRecordConflict("That payment is no longer awaiting confirmation.")
    if cashflow.initiated_by_participant_id == participant.id:
        raise PersonalLoanError("The other person must confirm a payment you recorded.")
    cashflow.state = "confirmed"
    cashflow.confirmed_by_participant_id = participant.id
    cashflow.confirmed_at = now_utc()
    record.row_version += 1
    event_type = "funding.confirmed" if cashflow.kind == "disbursement" else "payment.confirmed"
    append_event(db, record, event_type, actor_participant_id=participant.id, payload={
        "cashflowId": str(cashflow.id),
        "amountMinor": cashflow.amount_minor,
    })
    if cashflow.kind == "disbursement":
        agreement.funding_status = "confirmed"
        agreement.status = "active"
        record.status = "active"
    else:
        principal, interest, _paid = _remaining(db, agreement)
    if cashflow.kind == "repayment" and principal + interest == 0:
        agreement.status = "settlement_pending"
        record.status = "settlement_pending"
        append_event(db, record, "loan.ready_to_close", actor_participant_id=participant.id, payload={})
    rebuild_projections(db, record, agreement)
    finish_command(db, record=record, actor_user_id=user.id, command_type="personal_loan.confirm_payment", idempotency_key=idempotency_key, request_payload=request_data, response_payload={"loanId": str(agreement.id)})
    return agreement, False


def send_reminder(
    db: Session,
    *,
    agreement_id: UUID,
    user: User,
    request: SendLoanReminderIn,
    idempotency_key: str,
) -> tuple[LoanReminder, NotificationOutbox, str, bool]:
    record, participant, agreement = _loan_context(db, agreement_id, user.id, lock=True)
    request_data = request.model_dump(mode="json")
    prior = begin_command(db, actor_user_id=user.id, command_type="personal_loan.send_reminder", idempotency_key=idempotency_key, request_payload=request_data)
    if prior is not None:
        reminder = db.get(LoanReminder, UUID(prior.response_payload["reminderId"]))
        outbox = db.get(NotificationOutbox, UUID(prior.response_payload["outboxId"]))
        if reminder is None or outbox is None:
            raise PersonalLoanError("The previous reminder record is no longer available.")
        return reminder, outbox, outbox.destination_masked, True
    if agreement.status not in {"active", "settlement_pending"}:
        raise SharedRecordConflict("Reminders are available after both people agree to the plan.")
    previous = db.scalar(
        select(LoanReminder)
        .where(
            LoanReminder.agreement_id == agreement.id,
            LoanReminder.requested_by_participant_id == participant.id,
        )
        .order_by(LoanReminder.created_at.desc())
        .limit(1)
    )
    if previous is not None and (now_utc() - as_utc(previous.created_at)).total_seconds() < 12 * 60 * 60:
        raise SharedRecordConflict("A reminder was sent recently. Try again after 12 hours.")
    recipient = other_participant(db, record.id, participant.id)
    channel, destination, masked = _preferred_delivery(db, recipient)
    reminder = LoanReminder(
        agreement_id=agreement.id,
        requested_by_participant_id=participant.id,
        recipient_participant_id=recipient.id,
        tone=request.tone,
        note=request.note,
        state="queued",
        scheduled_at=now_utc(),
    )
    db.add(reminder)
    db.flush()
    term = _current_term(db, agreement)
    outbox = queue_notification(
        db,
        record=record,
        recipient=recipient,
        topic="personal_loan.reminder",
        channel=channel,
        destination=destination,
        payload={
            "senderName": participant.display_name,
            "recipientName": recipient.display_name,
            "loanId": str(agreement.id),
            "tone": request.tone,
            "note": request.note,
            "dueDate": term.due_date.isoformat(),
        },
        dedupe_key=f"loan-reminder:{reminder.id}",
    )
    append_event(db, record, "reminder.queued", actor_participant_id=participant.id, payload={
        "reminderId": str(reminder.id),
        "tone": reminder.tone,
        "channel": channel.value,
        "destinationMasked": masked,
    })
    rebuild_projections(db, record, agreement)
    finish_command(db, record=record, actor_user_id=user.id, command_type="personal_loan.send_reminder", idempotency_key=idempotency_key, request_payload=request_data, response_payload={"reminderId": str(reminder.id), "outboxId": str(outbox.id), "loanId": str(agreement.id)})
    return reminder, outbox, masked, False


def close_loan(
    db: Session,
    *,
    agreement_id: UUID,
    user: User,
    idempotency_key: str,
) -> tuple[PersonalLoanAgreement, bool]:
    record, participant, agreement = _loan_context(db, agreement_id, user.id, lock=True)
    request_data = {"loanId": str(agreement_id)}
    prior = begin_command(db, actor_user_id=user.id, command_type="personal_loan.close", idempotency_key=idempotency_key, request_payload=request_data)
    if prior is not None:
        return agreement, True
    principal, interest, _paid = _remaining(db, agreement)
    if principal + interest != 0:
        raise PersonalLoanError("The shared confirmed balance must be zero before closing.")
    last_proposal = db.scalar(
        select(SharedRecordEvent)
        .where(
            SharedRecordEvent.shared_record_id == record.id,
            SharedRecordEvent.event_type == "loan.closure_proposed",
        )
        .order_by(SharedRecordEvent.sequence.desc())
        .limit(1)
    )
    assurance_items = _security_items(db, agreement.id)
    if last_proposal is None:
        if assurance_items and participant.role != "lender":
            raise PersonalLoanError("The person holding the assurance item must record its return before closure.")
        agreement.status = "settlement_pending"
        record.status = "settlement_pending"
        for item in assurance_items:
            if item.state != "returned":
                item.state = "return_pending_confirmation"
        if assurance_items:
            append_event(db, record, "security.return_proposed", actor_participant_id=participant.id, payload={
                "itemIds": [str(item.id) for item in assurance_items],
            })
        append_event(db, record, "loan.closure_proposed", actor_participant_id=participant.id, payload={})
    elif last_proposal.actor_participant_id == participant.id:
        raise SharedRecordConflict("The other person must confirm closure.")
    else:
        for item in assurance_items:
            item.state = "returned"
            item.returned_at = now_utc()
            item.return_confirmed_by_participant_id = participant.id
        agreement.status = "closed"
        record.status = "closed"
        append_event(db, record, "loan.closed", actor_participant_id=participant.id, payload={})
    record.row_version += 1
    rebuild_projections(db, record, agreement)
    finish_command(db, record=record, actor_user_id=user.id, command_type="personal_loan.close", idempotency_key=idempotency_key, request_payload=request_data, response_payload={"loanId": str(agreement.id), "status": agreement.status})
    return agreement, False


def list_personal_loans(db: Session, user: User) -> list[PersonalLoanAgreement]:
    return list(db.scalars(
        select(PersonalLoanAgreement)
        .join(SharedRecord, SharedRecord.id == PersonalLoanAgreement.shared_record_id)
        .join(SharedRecordParticipant, SharedRecordParticipant.shared_record_id == SharedRecord.id)
        .where(
            SharedRecordParticipant.member_user_id == user.id,
            SharedRecordParticipant.hidden_at.is_(None),
        )
        .order_by(SharedRecord.updated_at.desc())
    ))


def _document_dict(db: Session, revision: DocumentRevision) -> dict[str, Any]:
    document = db.scalar(select(SharedDocument).where(SharedDocument.id == revision.document_id))
    participants = {item.id: item for item in _participants(db, document.shared_record_id)} if document else {}
    changes = list(db.scalars(select(DocumentChange).where(DocumentChange.revision_id == revision.id).order_by(DocumentChange.created_at)))
    acceptances = list(db.scalars(select(DocumentAcceptance).where(DocumentAcceptance.revision_id == revision.id).order_by(DocumentAcceptance.accepted_at)))
    assets = revision_assets(db, revision.id)
    return {
        "id": revision.id,
        "documentId": revision.document_id,
        "documentTitle": document.title if document else "Shared document",
        "revisionNumber": revision.revision_number,
        "baseRevisionId": revision.base_revision_id,
        "state": revision.state,
        "authoredBy": participants[revision.authored_by_participant_id].display_name if revision.authored_by_participant_id in participants else "Former participant",
        "content": revision.content,
        "changeSummary": revision.change_summary,
        "sourceSnapshotHash": revision.source_snapshot_hash,
        "contentHash": revision.content_hash,
        "manifestHash": revision.manifest_hash,
        "evidenceHash": revision.evidence_hash,
        "proposedAt": revision.proposed_at,
        "finalizedAt": revision.finalized_at,
        "changes": [{
            "id": change.id,
            "fieldPath": change.field_path,
            "beforeValue": change.before_value,
            "afterValue": change.after_value,
            "summary": change.summary,
            "authoredBy": participants[change.authored_by_participant_id].display_name if change.authored_by_participant_id in participants else "Former participant",
            "createdAt": change.created_at,
        } for change in changes],
        "acceptances": [{
            "participantId": acceptance.participant_id,
            "participantName": participants[acceptance.participant_id].display_name if acceptance.participant_id in participants else "Former participant",
            "action": acceptance.action,
            "contentHash": acceptance.content_hash,
            "manifestHash": acceptance.manifest_hash,
            "evidenceHash": acceptance.evidence_hash,
            "acceptedAt": acceptance.accepted_at,
            "statementVersion": acceptance.statement_version,
            "statementText": acceptance.statement_text,
            "authMethod": acceptance.auth_method,
            "actorIdentifierMasked": acceptance.actor_identifier_masked,
            "actorTimezone": acceptance.actor_timezone,
            "requestIpHash": acceptance.request_ip_hash,
            "userAgentHash": acceptance.user_agent_hash,
        } for acceptance in acceptances],
        "assets": [asset_dict(asset) for asset in assets],
    }


def detail_payload(db: Session, agreement: PersonalLoanAgreement, user: User, *, invitation_token: str | None = None) -> dict[str, Any]:
    record, participant = record_for_user(db, agreement.shared_record_id, user.id)
    parties = _participants(db, record.id)
    counterparty = next(item for item in parties if item.id != participant.id)
    term = _current_term(db, agreement)
    latest_term = _latest_term(db, agreement)
    document = _document_for_record(db, record.id)
    revision = db.scalar(select(DocumentRevision).where(DocumentRevision.id == latest_term.document_revision_id)) or _latest_revision(db, document.id)
    outstanding, interest, paid = _remaining(db, agreement, term)
    verification = None
    if counterparty.verification_channel:
        verification = f"{counterparty.verification_channel}_verified"
    invitation = db.scalar(
        select(SharedRecordInvitation)
        .where(SharedRecordInvitation.participant_id == counterparty.id)
        .order_by(SharedRecordInvitation.created_at.desc())
        .limit(1)
    )
    cashflows = list(db.scalars(select(LoanCashflow).where(
        LoanCashflow.agreement_id == agreement.id,
        LoanCashflow.kind == "repayment",
    ).order_by(LoanCashflow.occurred_on.desc(), LoanCashflow.created_at.desc())))
    funding_cashflow = db.scalar(select(LoanCashflow).where(
        LoanCashflow.agreement_id == agreement.id,
        LoanCashflow.kind == "disbursement",
    ))
    participants_by_id = {item.id: item for item in parties}
    document_requests = _document_requests(db, record.id)
    assurance_items = _security_items(db, agreement.id)
    events = list(db.scalars(select(SharedRecordEvent).where(
        SharedRecordEvent.shared_record_id == record.id
    ).order_by(SharedRecordEvent.sequence.desc())))
    summary = {
        "id": agreement.id,
        "sharedRecordId": record.id,
        "direction": "lent" if participant.role == "lender" else "borrowed",
        "counterpartyName": counterparty.display_name,
        "counterpartyVerification": verification,
        "status": agreement.status,
        "fundingStatus": agreement.funding_status,
        "intent": agreement.intent,
        "principalMinor": term.principal_minor,
        "outstandingPrincipalMinor": outstanding,
        "accruedInterestMinor": interest,
        "totalRepayableMinor": term.total_repayable_minor,
        "paidMinor": paid,
        "currency": agreement.currency,
        "moneyDate": term.money_date,
        "dueDate": term.due_date,
        "nextDueMinor": outstanding + interest if outstanding + interest else None,
        "responseNeeded": _response_needed(db, agreement, participant),
        "rowVersion": record.row_version,
        "createdAt": record.created_at,
        "updatedAt": record.updated_at,
    }
    return {
        **summary,
        "note": term.note,
        "interestRateBps": term.interest_rate_bps,
        "interestPeriod": term.interest_period,
        "interestMode": term.interest_mode,
        "currentTerms": {
            "id": term.id,
            "version": term.version,
            "principalMinor": term.principal_minor,
            "currency": term.currency,
            "interestRateBps": term.interest_rate_bps,
            "interestPeriod": term.interest_period,
            "interestMode": term.interest_mode,
            "annualizedRateBps": _annualized_rate_bps(term.interest_rate_bps, term.interest_period),
            "interestMethod": term.interest_method,
            "calculationBasis": term.calculation_basis,
            "roundingPolicy": term.rounding_policy,
            "moneyDate": term.money_date,
            "dueDate": term.due_date,
            "note": term.note,
            "totalInterestMinor": term.total_interest_minor,
            "totalRepayableMinor": term.total_repayable_minor,
            "state": term.state,
            "sourceHash": term.source_hash,
            "documentRevisionId": term.document_revision_id,
            "effectiveAt": term.effective_at,
        },
        "participants": [{
            "id": item.id,
            "role": item.role,
            "displayName": item.display_name,
            "state": item.state,
            "isCurrentUser": item.id == participant.id,
            "verificationChannel": item.verification_channel,
            "verificationClaim": item.verification_claim,
            "claimedAt": item.claimed_at,
        } for item in parties],
        "invitation": ({
            "id": invitation.id,
            "channel": invitation.channel,
            "destinationMasked": invitation.destination_masked,
            "expiresAt": invitation.expires_at,
            "redeemedAt": invitation.redeemed_at,
            "revokedAt": invitation.revoked_at,
            "sharePath": f"/loan-invitations/{invitation_token}" if invitation_token else None,
        } if invitation else None),
        "documentRevision": _document_dict(db, revision),
        "cashflows": [{
            "id": item.id,
            "kind": item.kind,
            "state": item.state,
            "amountMinor": item.amount_minor,
            "principalMinor": item.principal_minor,
            "interestMinor": item.interest_minor,
            "currency": item.currency,
            "occurredOn": item.occurred_on,
            "initiatedBy": participants_by_id[item.initiated_by_participant_id].display_name,
            "confirmedBy": participants_by_id[item.confirmed_by_participant_id].display_name if item.confirmed_by_participant_id else None,
            "note": item.note,
            "createdAt": item.created_at,
            "confirmedAt": item.confirmed_at,
        } for item in cashflows],
        "fundingCashflow": ({
            "id": funding_cashflow.id,
            "kind": funding_cashflow.kind,
            "state": funding_cashflow.state,
            "amountMinor": funding_cashflow.amount_minor,
            "principalMinor": funding_cashflow.principal_minor,
            "interestMinor": funding_cashflow.interest_minor,
            "currency": funding_cashflow.currency,
            "occurredOn": funding_cashflow.occurred_on,
            "initiatedBy": participants_by_id[funding_cashflow.initiated_by_participant_id].display_name,
            "confirmedBy": participants_by_id[funding_cashflow.confirmed_by_participant_id].display_name if funding_cashflow.confirmed_by_participant_id else None,
            "note": funding_cashflow.note,
            "createdAt": funding_cashflow.created_at,
            "confirmedAt": funding_cashflow.confirmed_at,
        } if funding_cashflow else None),
        "documentRequests": [{
            "id": item.id,
            "label": item.label,
            "classification": item.classification,
            "instructions": item.instructions,
            "required": item.required,
            "state": item.state,
            "requestedBy": participants_by_id[item.requested_by_participant_id].display_name,
            "requestedFrom": participants_by_id[item.requested_from_participant_id].display_name,
            "requestedFromCurrentUser": item.requested_from_participant_id == participant.id,
            "fulfilledAsset": asset_dict(asset) if item.fulfilled_asset_id and (asset := db.get(DocumentAsset, item.fulfilled_asset_id)) else None,
            "fulfilledRevisionId": item.fulfilled_revision_id,
            "fulfilledAt": item.fulfilled_at,
        } for item in document_requests],
        "securityItems": [{
            "id": item.id,
            "kind": item.kind,
            "description": item.description,
            "maskedIdentifier": item.masked_identifier,
            "statedValueMinor": item.stated_value_minor,
            "currency": item.currency,
            "providedBy": participants_by_id[item.provided_by_participant_id].display_name,
            "heldBy": participants_by_id[item.held_by_participant_id].display_name,
            "state": item.state,
            "returnedAt": item.returned_at,
            "returnConfirmedBy": participants_by_id[item.return_confirmed_by_participant_id].display_name if item.return_confirmed_by_participant_id else None,
        } for item in assurance_items],
        "activity": [{
            "id": item.id,
            "sequence": item.sequence,
            "eventType": item.event_type,
            "actorParticipantId": item.actor_participant_id,
            "actorName": participants_by_id[item.actor_participant_id].display_name if item.actor_participant_id in participants_by_id else None,
            "payload": item.payload,
            "eventHash": item.event_hash,
            "createdAt": item.created_at,
        } for item in events],
    }


def summary_payload(db: Session, agreement: PersonalLoanAgreement, user: User) -> dict[str, Any]:
    detail = detail_payload(db, agreement, user)
    return {key: value for key, value in detail.items() if key in {
        "id", "sharedRecordId", "direction", "counterpartyName", "counterpartyVerification",
        "status", "fundingStatus", "intent", "principalMinor", "outstandingPrincipalMinor",
        "accruedInterestMinor", "totalRepayableMinor", "paidMinor", "currency",
        "moneyDate", "dueDate", "nextDueMinor", "responseNeeded", "rowVersion",
        "createdAt", "updatedAt",
    }}


def verify_projection_integrity(db: Session, agreement_id: UUID) -> dict[str, Any]:
    agreement = db.scalar(select(PersonalLoanAgreement).where(PersonalLoanAgreement.id == agreement_id))
    if agreement is None:
        raise SharedRecordNotFound("Loan not found")
    record = db.scalar(select(SharedRecord).where(SharedRecord.id == agreement.shared_record_id))
    if record is None:
        raise SharedRecordNotFound("Loan not found")
    expected_sequence = _latest_event_sequence(db, record.id)
    mismatches: list[dict[str, Any]] = []
    term = _current_term(db, agreement)
    outstanding, interest, _paid = _remaining(db, agreement, term)
    for participant in _participants(db, record.id):
        if participant.member_user_id is None:
            continue
        projection = db.scalar(select(Loan).where(
            Loan.user_id == participant.member_user_id,
            Loan.shared_record_id == record.id,
        ))
        if projection is None:
            mismatches.append({"participantId": str(participant.id), "reason": "missing_projection"})
            continue
        expected = (outstanding, interest, expected_sequence)
        actual = (projection.outstanding_principal_minor, projection.accrued_interest_minor, projection.last_projected_event_sequence)
        if expected != actual:
            mismatches.append({"participantId": str(participant.id), "expected": expected, "actual": actual})
    return {"valid": not mismatches, "mismatches": mismatches, "eventSequence": expected_sequence}

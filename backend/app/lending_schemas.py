"""Public contracts for the modular shared-record and personal-loan feature."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import DEFAULT_CURRENCY


def _camel_case(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.title() for part in tail)


class LendingContract(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        alias_generator=_camel_case,
        serialize_by_alias=True,
    )


class LoanSecurityItemIn(LendingContract):
    kind: Literal["gold", "post_dated_cheque", "cancelled_cheque", "document", "other"]
    description: str = Field(min_length=1, max_length=240)
    masked_identifier: str | None = Field(default=None, max_length=120)
    stated_value_minor: int | None = Field(default=None, ge=0)

    @field_validator("description", "masked_identifier", mode="before")
    @classmethod
    def normalize_security_text(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = " ".join(str(value).split())
        return normalized or None


class DocumentRequestIn(LendingContract):
    label: str = Field(min_length=2, max_length=120)
    classification: Literal["external_agreement", "assurance_item", "transfer_receipt", "identity_evidence", "witness_statement", "supporting_evidence"] = "supporting_evidence"
    instructions: str | None = Field(default=None, max_length=500)
    required: bool = True

    @field_validator("label", "instructions", mode="before")
    @classmethod
    def normalize_request_text(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = " ".join(str(value).split())
        return normalized or None


class CreatePersonalLoanIn(LendingContract):
    direction: Literal["lent", "borrowed"]
    intent: Literal["record_given", "record_received", "offer_to_lend", "request_to_borrow"] | None = None
    counterparty_name: str = Field(min_length=1, max_length=120)
    invite_channel: Literal["phone", "email"]
    invite_value: str = Field(min_length=3, max_length=320)
    principal_minor: int = Field(gt=0, le=1_000_000_000_00)
    currency: str = Field(default=DEFAULT_CURRENCY, min_length=3, max_length=3)
    money_date: date
    due_date: date
    interest_rate_bps: int = Field(ge=0, le=10_000)
    interest_period: Literal["monthly", "yearly"]
    interest_mode: Literal["simple", "compound"]
    note: str | None = Field(default=None, max_length=2_000)
    security_items: list[LoanSecurityItemIn] = Field(default_factory=list, max_length=5)
    document_requests: list[DocumentRequestIn] = Field(default_factory=list, max_length=8)
    asset_ids: list[UUID] = Field(default_factory=list, max_length=8)

    @field_validator("counterparty_name", "note", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = " ".join(str(value).split())
        return normalized or None

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: Any) -> str:
        return str(value).strip().upper()

    @model_validator(mode="after")
    def valid_dates(self) -> "CreatePersonalLoanIn":
        if self.due_date < self.money_date:
            raise ValueError("The return date cannot be before the money date")
        return self


class LoanTermProposalIn(LendingContract):
    due_date: date
    interest_rate_bps: int = Field(ge=0, le=10_000)
    interest_period: Literal["monthly", "yearly"]
    interest_mode: Literal["simple", "compound"]
    note: str | None = Field(default=None, max_length=2_000)
    expected_row_version: int = Field(gt=0)

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = " ".join(str(value).split())
        return normalized or None


class RecordLoanPaymentIn(LendingContract):
    amount_minor: int = Field(gt=0)
    occurred_on: date
    note: str | None = Field(default=None, max_length=500)

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = " ".join(str(value).split())
        return normalized or None


class RecordLoanFundingIn(LendingContract):
    occurred_on: date
    note: str | None = Field(default=None, max_length=500)

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = " ".join(str(value).split())
        return normalized or None


class ConfirmLoanPaymentIn(LendingContract):
    expected_row_version: int = Field(gt=0)


class DocumentRequestFulfillmentItemIn(LendingContract):
    request_id: UUID
    asset_id: UUID


class FulfillDocumentRequestsIn(LendingContract):
    items: list[DocumentRequestFulfillmentItemIn] = Field(min_length=1, max_length=8)
    expected_row_version: int = Field(gt=0)


class SendLoanReminderIn(LendingContract):
    tone: Literal["friendly", "update", "due"] = "friendly"
    note: str | None = Field(default=None, max_length=500)

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = " ".join(str(value).split())
        return normalized or None


class LoanParticipantOut(LendingContract):
    id: UUID
    role: Literal["lender", "borrower"]
    display_name: str
    state: str
    is_current_user: bool
    verification_channel: str | None = None
    verification_claim: str | None = None
    claimed_at: datetime | None = None


class LoanInvitationOut(LendingContract):
    id: UUID
    channel: Literal["phone", "email"]
    destination_masked: str
    expires_at: datetime
    redeemed_at: datetime | None = None
    revoked_at: datetime | None = None
    share_path: str | None = None


class LoanTermOut(LendingContract):
    id: UUID
    version: int
    principal_minor: int
    currency: str
    interest_rate_bps: int
    interest_period: Literal["monthly", "yearly"]
    interest_mode: Literal["simple", "compound"]
    annualized_rate_bps: int
    interest_method: str
    calculation_basis: str
    rounding_policy: str
    money_date: date
    due_date: date
    note: str | None = None
    total_interest_minor: int
    total_repayable_minor: int
    state: str
    source_hash: str
    document_revision_id: UUID | None = None
    effective_at: datetime | None = None


class DocumentChangeOut(LendingContract):
    id: UUID
    field_path: str
    before_value: dict[str, Any] | None = None
    after_value: dict[str, Any] | None = None
    summary: str
    authored_by: str
    created_at: datetime


class DocumentAcceptanceOut(LendingContract):
    participant_id: UUID
    participant_name: str
    action: str
    content_hash: str
    manifest_hash: str
    evidence_hash: str
    accepted_at: datetime
    statement_version: int
    statement_text: str
    auth_method: str
    actor_identifier_masked: str | None = None
    actor_timezone: str
    request_ip_hash: str | None = None
    user_agent_hash: str | None = None


class DocumentAssetOut(LendingContract):
    id: UUID
    original_filename: str
    media_type: Literal["application/pdf", "image/png", "image/jpeg"]
    byte_size: int
    sha256: str
    state: str
    classification: str
    description: str | None = None
    created_at: datetime


class DocumentRequestOut(LendingContract):
    id: UUID
    label: str
    classification: str
    instructions: str | None = None
    required: bool
    state: str
    requested_by: str
    requested_from: str
    requested_from_current_user: bool
    fulfilled_asset: DocumentAssetOut | None = None
    fulfilled_revision_id: UUID | None = None
    fulfilled_at: datetime | None = None


class DocumentRevisionOut(LendingContract):
    id: UUID
    document_id: UUID
    document_title: str
    revision_number: int
    base_revision_id: UUID | None = None
    state: str
    authored_by: str
    content: dict[str, Any]
    change_summary: list[dict[str, Any]]
    source_snapshot_hash: str
    content_hash: str
    manifest_hash: str
    evidence_hash: str
    proposed_at: datetime
    finalized_at: datetime | None = None
    changes: list[DocumentChangeOut] = Field(default_factory=list)
    acceptances: list[DocumentAcceptanceOut] = Field(default_factory=list)
    assets: list[DocumentAssetOut] = Field(default_factory=list)


class LoanCashflowOut(LendingContract):
    id: UUID
    kind: str
    state: str
    amount_minor: int
    principal_minor: int
    interest_minor: int
    currency: str
    occurred_on: date
    initiated_by: str
    confirmed_by: str | None = None
    note: str | None = None
    created_at: datetime
    confirmed_at: datetime | None = None


class LoanSecurityItemOut(LendingContract):
    id: UUID
    kind: str
    description: str
    masked_identifier: str | None = None
    stated_value_minor: int | None = None
    currency: str
    provided_by: str
    held_by: str
    state: str
    returned_at: datetime | None = None
    return_confirmed_by: str | None = None


class SharedRecordEventOut(LendingContract):
    id: UUID
    sequence: int
    event_type: str
    actor_participant_id: UUID | None = None
    actor_name: str | None = None
    payload: dict[str, Any]
    event_hash: str
    created_at: datetime


class PersonalLoanSummaryOut(LendingContract):
    id: UUID
    shared_record_id: UUID
    direction: Literal["lent", "borrowed"]
    counterparty_name: str
    counterparty_verification: str | None = None
    status: str
    funding_status: str
    intent: str
    principal_minor: int
    outstanding_principal_minor: int
    accrued_interest_minor: int
    total_repayable_minor: int
    paid_minor: int
    currency: str
    money_date: date
    due_date: date
    next_due_minor: int | None = None
    response_needed: bool
    row_version: int
    created_at: datetime
    updated_at: datetime


class PersonalLoanDetailOut(PersonalLoanSummaryOut):
    note: str | None = None
    interest_rate_bps: int
    interest_period: Literal["monthly", "yearly"]
    interest_mode: Literal["simple", "compound"]
    current_terms: LoanTermOut
    participants: list[LoanParticipantOut]
    invitation: LoanInvitationOut | None = None
    document_revision: DocumentRevisionOut
    cashflows: list[LoanCashflowOut]
    funding_cashflow: LoanCashflowOut | None = None
    document_requests: list[DocumentRequestOut]
    security_items: list[LoanSecurityItemOut]
    activity: list[SharedRecordEventOut]


class PersonalLoanListOut(LendingContract):
    money_i_gave_minor: int
    money_i_received_minor: int
    needs_response_count: int
    items: list[PersonalLoanSummaryOut]


class InvitationPreviewOut(LendingContract):
    token_valid: bool
    sender_name: str | None = None
    recipient_name: str | None = None
    channel: str | None = None
    destination_masked: str | None = None
    expires_at: datetime | None = None
    can_redeem: bool = False
    loan: PersonalLoanSummaryOut | None = None


class ReminderOut(LendingContract):
    id: UUID
    state: str
    channel: str
    destination_masked: str
    queued_at: datetime


class LoanCommandOut(LendingContract):
    loan: PersonalLoanDetailOut
    replayed: bool = False

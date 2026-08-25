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


class CreatePersonalLoanIn(LendingContract):
    direction: Literal["lent", "borrowed"]
    counterparty_name: str = Field(min_length=1, max_length=120)
    invite_channel: Literal["phone", "email"]
    invite_value: str = Field(min_length=3, max_length=320)
    principal_minor: int = Field(gt=0, le=1_000_000_000_00)
    currency: str = Field(default=DEFAULT_CURRENCY, min_length=3, max_length=3)
    money_date: date
    due_date: date
    annual_rate_bps: int = Field(default=0, ge=0, le=10_000)
    note: str | None = Field(default=None, max_length=2_000)
    security_items: list[LoanSecurityItemIn] = Field(default_factory=list, max_length=5)

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
    annual_rate_bps: int = Field(ge=0, le=10_000)
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


class ConfirmLoanPaymentIn(LendingContract):
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
    annual_rate_bps: int
    interest_method: str
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
    accepted_at: datetime


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
    proposed_at: datetime
    finalized_at: datetime | None = None
    changes: list[DocumentChangeOut] = Field(default_factory=list)
    acceptances: list[DocumentAcceptanceOut] = Field(default_factory=list)


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
    annual_rate_bps: int
    current_terms: LoanTermOut
    participants: list[LoanParticipantOut]
    invitation: LoanInvitationOut | None = None
    document_revision: DocumentRevisionOut
    cashflows: list[LoanCashflowOut]
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

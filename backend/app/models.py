from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import DEFAULT_CURRENCY, DEFAULT_TIMEZONE
from .database import Base
from .domain import ACTIVE_STATUS, AnalysisToolStatus, DraftState, FinancialSourceType, IdentitySource, ImportStatus, ObservationProcessingState, SpendNature, TaxonomyScope, TransactionStatus, TransactionType
from .event_time import as_utc, now_utc


def uuid4() -> uuid.UUID:
    return uuid.uuid4()


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid4)


class UserOwnedMixin:
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )


class CurrencyMixin:
    currency: Mapped[str] = mapped_column(String(3), default=DEFAULT_CURRENCY)


class ConversationChildMixin:
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
    )


class TransactionChildMixin:
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"),
        index=True,
    )


class ConfidenceMixin:
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(4, 3),
        default=Decimal("1"),
    )


SCOPE_OWNER_CHECK = (
    "(scope = 'system' AND owner_user_id IS NULL) OR "
    "(scope = 'user' AND owner_user_id IS NOT NULL)"
)


class ScopedOwnershipMixin:
    scope: Mapped[str] = mapped_column(
        String(20),
        default=TaxonomyScope.SYSTEM.value,
        index=True,
    )
    owner_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now(), onupdate=now_utc)


class User(UUIDPrimaryKeyMixin, CurrencyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    # An account can be reached by phone, by email, or by both, so neither
    # column can be required. `user_identities` is the source of truth for who
    # owns an identifier; these two mirror the current values so the profile and
    # the data export can be answered without a join.
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True)
    phone: Mapped[Optional[str]] = mapped_column(String(20), unique=True)
    display_name: Mapped[str] = mapped_column(String(120), default="You")
    timezone: Mapped[str] = mapped_column(String(80), default=DEFAULT_TIMEZONE)


class UserIdentity(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    """One verified way to sign in to one account.

    The unique constraint over (provider, identifier) is the rule that a phone
    number or an email address belongs to exactly one account: a second account
    cannot claim it while the first still exists.
    """

    __tablename__ = "user_identities"
    provider: Mapped[str] = mapped_column(String(20), index=True)
    # E.164 for phone, lowercased address for email, the Google subject for google.
    identifier: Mapped[str] = mapped_column(String(320))
    # Google's email is informational; the email row created alongside it is
    # what actually reserves the address.
    email: Mapped[Optional[str]] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(20), default=IdentitySource.OTP.value)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("provider", "identifier", name="uq_identity_provider_identifier"),
        UniqueConstraint("user_id", "provider", name="uq_identity_user_provider"),
    )


class UserSession(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    """A signed-in browser. Only the hash of the cookie value is stored, so a
    database copy cannot be replayed as a live session."""

    __tablename__ = "user_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class OtpChallenge(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A one-time code in flight.

    `user_id` is null while signing in, because the account is not known until
    the code is verified, and set while linking, so one account's challenge can
    never be completed by another.
    """

    __tablename__ = "otp_challenges"
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    purpose: Mapped[str] = mapped_column(String(20))
    channel: Mapped[str] = mapped_column(String(20))
    destination: Mapped[str] = mapped_column(String(320), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts_remaining: Mapped[int] = mapped_column(Integer, default=5)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_otp_destination_window", "destination", "purpose", "created_at"),
    )


class Account(UUIDPrimaryKeyMixin, CurrencyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "accounts"
    name: Mapped[str] = mapped_column(String(120))
    account_type: Mapped[str] = mapped_column(String(40), default="bank")
    institution: Mapped[Optional[str]] = mapped_column(String(120))
    mask: Mapped[Optional[str]] = mapped_column(String(12))
    balance_minor: Mapped[int] = mapped_column(Integer, default=0)


class AccountBalanceSnapshot(UUIDPrimaryKeyMixin, CurrencyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "account_balance_snapshots"
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    balance_minor: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now(), index=True)
    source_type: Mapped[str] = mapped_column(String(30), default=FinancialSourceType.MANUAL.value)
    __table_args__ = (
        UniqueConstraint("account_id", "observed_at", "source_type", name="uq_account_balance_observation"),
        Index("ix_account_balance_history", "user_id", "observed_at", "account_id"),
    )


class InvestmentHolding(UUIDPrimaryKeyMixin, CurrencyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "investment_holdings"
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    symbol: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    asset_type: Mapped[str] = mapped_column(String(40), default="other", index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=Decimal("0"))
    cost_basis_minor: Mapped[int] = mapped_column(Integer, default=0)
    current_value_minor: Mapped[int] = mapped_column(Integer, default=0)
    valued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())
    status: Mapped[str] = mapped_column(String(20), default=ACTIVE_STATUS, index=True)


class InvestmentValuationSnapshot(UUIDPrimaryKeyMixin, CurrencyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "investment_valuation_snapshots"
    holding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_holdings.id", ondelete="CASCADE"), index=True)
    market_value_minor: Mapped[int] = mapped_column(Integer)
    cost_basis_minor: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now(), index=True)
    source_type: Mapped[str] = mapped_column(String(30), default=FinancialSourceType.MANUAL.value)
    __table_args__ = (
        UniqueConstraint("holding_id", "observed_at", "source_type", name="uq_investment_valuation_observation"),
        Index("ix_investment_value_history", "user_id", "observed_at", "holding_id"),
    )


class Category(UUIDPrimaryKeyMixin, ScopedOwnershipMixin, TimestampMixin, Base):
    __tablename__ = "categories"
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    icon: Mapped[Optional[str]] = mapped_column(String(40))
    __table_args__ = (
        CheckConstraint(
            SCOPE_OWNER_CHECK,
            name="ck_category_scope_owner",
        ),
    )


class Subcategory(UUIDPrimaryKeyMixin, ScopedOwnershipMixin, TimestampMixin, Base):
    __tablename__ = "subcategories"
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(80))
    __table_args__ = (
        UniqueConstraint("category_id", "slug"),
        CheckConstraint(
            SCOPE_OWNER_CHECK,
            name="ck_subcategory_scope_owner",
        ),
    )


class TransactionCategoryHint(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    """An explicit merchant-to-taxonomy instruction from one user.

    Learned transaction history remains evidence; a hint is stronger because
    the user deliberately stated the mapping. The normalized value is the
    lookup key and the original pattern is retained for display and editing.
    """

    __tablename__ = "transaction_category_hints"
    merchant_pattern: Mapped[str] = mapped_column(String(160))
    normalized_pattern: Mapped[str] = mapped_column(String(160))
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), index=True)
    subcategory_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("subcategories.id", ondelete="SET NULL"), index=True)
    __table_args__ = (
        UniqueConstraint("user_id", "normalized_pattern", name="uq_user_transaction_category_hint"),
    )


class Merchant(UUIDPrimaryKeyMixin, ScopedOwnershipMixin, TimestampMixin, Base):
    __tablename__ = "merchants"
    canonical_name: Mapped[str] = mapped_column(String(160), index=True)
    normalized_name: Mapped[str] = mapped_column(String(160), index=True)
    __table_args__ = (
        CheckConstraint(
            SCOPE_OWNER_CHECK,
            name="ck_merchant_scope_owner",
        ),
    )


class MerchantAlias(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "merchant_aliases"
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    raw_alias: Mapped[str] = mapped_column(String(255))
    normalized_alias: Mapped[str] = mapped_column(String(255), index=True)
    context: Mapped[Optional[str]] = mapped_column(String(80))


class Conversation(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    title: Mapped[str] = mapped_column(String(160), default="New conversation")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    active_analysis_state: Mapped[Optional[dict]] = mapped_column(JSON)
    active_data_scope: Mapped[Optional[dict]] = mapped_column(JSON)
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by=lambda: (Message.created_at, Message.id),
    )


class Message(UUIDPrimaryKeyMixin, ConversationChildMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default="")
    widgets: Mapped[list] = mapped_column(JSON, default=list)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class TransactionDraft(UUIDPrimaryKeyMixin, CurrencyMixin, UserOwnedMixin, ConversationChildMixin, TimestampMixin, Base):
    __tablename__ = "transaction_drafts"
    raw_text: Mapped[str] = mapped_column(Text)
    transaction_type: Mapped[str] = mapped_column(String(30), default=TransactionType.UNKNOWN.value)
    amount_minor: Mapped[Optional[int]] = mapped_column(Integer)
    merchant_name: Mapped[Optional[str]] = mapped_column(String(160))
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("accounts.id"))
    destination_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("accounts.id"))
    source_account_name: Mapped[Optional[str]] = mapped_column(String(120))
    destination_account_name: Mapped[Optional[str]] = mapped_column(String(120))
    category_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("categories.id"))
    subcategory_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("subcategories.id"))
    transaction_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now(), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    latitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 6))
    location_accuracy: Mapped[Optional[int]] = mapped_column(Integer)
    location_source: Mapped[Optional[str]] = mapped_column(String(40))
    location_label: Mapped[Optional[str]] = mapped_column(String(160))
    tags: Mapped[list] = mapped_column(JSON, default=list)
    spend_nature: Mapped[str] = mapped_column(String(30), default=SpendNature.UNKNOWN.value)
    field_provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("0.5"))
    missing_fields: Mapped[list] = mapped_column(JSON, default=list)
    inferred_fields: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(40), default=DraftState.RECEIVED.value, index=True)


class Transaction(UUIDPrimaryKeyMixin, CurrencyMixin, UserOwnedMixin, ConfidenceMixin, TimestampMixin, Base):
    __tablename__ = "transactions"
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("accounts.id"), index=True)
    destination_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("accounts.id"), index=True)
    transaction_type: Mapped[str] = mapped_column(String(30), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    merchant_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("merchants.id"), index=True)
    merchant_name: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    category_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("categories.id"), index=True)
    subcategory_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("subcategories.id"))
    transaction_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now(), index=True)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    latitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 6))
    location_accuracy: Mapped[Optional[int]] = mapped_column(Integer)
    location_source: Mapped[Optional[str]] = mapped_column(String(40))
    location_label: Mapped[Optional[str]] = mapped_column(String(160))
    description: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    spend_nature: Mapped[str] = mapped_column(String(30), default=SpendNature.UNKNOWN.value, index=True)
    status: Mapped[str] = mapped_column(String(30), default=TransactionStatus.PROVISIONAL.value, index=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    sources: Mapped[list["TransactionSource"]] = relationship(back_populates="transaction", cascade="all, delete-orphan")
    __table_args__ = (
        Index("ix_reconciliation_window", "user_id", "account_id", "amount_minor", "currency", "transaction_type", "transaction_at"),
        Index("ix_transaction_user_at_type", "user_id", "transaction_at", "transaction_type"),
        # Covers the evidence window the category recommender reads per draft.
        Index("ix_transactions_user_at_category", "user_id", "transaction_at", "category_id"),
    )


class Tag(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "tags"
    name: Mapped[str] = mapped_column(String(80))
    normalized_name: Mapped[str] = mapped_column(String(80))
    color: Mapped[Optional[str]] = mapped_column(String(20))
    __table_args__ = (UniqueConstraint("user_id", "normalized_name", name="uq_user_tag_name"),)


class TransactionTag(UUIDPrimaryKeyMixin, TransactionChildMixin, ConfidenceMixin, TimestampMixin, Base):
    __tablename__ = "transaction_tags"
    tag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(30), default="user")
    __table_args__ = (UniqueConstraint("transaction_id", "tag_id", name="uq_transaction_tag"),)


class TransactionFieldValue(UUIDPrimaryKeyMixin, TransactionChildMixin, ConfidenceMixin, TimestampMixin, Base):
    __tablename__ = "transaction_field_values"
    field_name: Mapped[str] = mapped_column(String(60), index=True)
    value: Mapped[dict] = mapped_column(JSON)
    origin: Mapped[str] = mapped_column(String(40))
    source_observation_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("financial_observations.id", ondelete="SET NULL"))
    user_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class Loan(UUIDPrimaryKeyMixin, CurrencyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "loans"
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(140))
    loan_type: Mapped[str] = mapped_column(String(40), default="home")
    lender: Mapped[Optional[str]] = mapped_column(String(140))
    outstanding_principal_minor: Mapped[int] = mapped_column(Integer)
    annual_rate_percent: Mapped[Decimal] = mapped_column(Numeric(7, 4))
    rate_type: Mapped[str] = mapped_column(String(20), default="floating")
    remaining_tenure_months: Mapped[int] = mapped_column(Integer)
    current_emi_minor: Mapped[Optional[int]] = mapped_column(Integer)
    prepayment_fee_percent: Mapped[Decimal] = mapped_column(Numeric(7, 4), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(20), default=ACTIVE_STATUS, index=True)


class LoanScenario(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "loan_scenarios"
    loan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("loans.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    objective: Mapped[str] = mapped_column(String(40))
    inputs: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)


class AnalysisTool(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    """A generated, declarative, read-only finance analysis capability.

    Generated tools contain data, not executable Python or arbitrary SQL. The
    harness validates and compiles their specification through the governed
    semantic layer before a tool can become active.
    """

    __tablename__ = "analysis_tools"
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    intent_signature: Mapped[str] = mapped_column(String(160), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default=AnalysisToolStatus.DRAFT.value, index=True)
    specification: Mapped[dict] = mapped_column(JSON)
    specification_hash: Mapped[str] = mapped_column(String(64), index=True)
    validation_report: Mapped[dict] = mapped_column(JSON, default=dict)
    retrieval_embedding: Mapped[Optional[list]] = mapped_column(JSON)
    retrieval_embedding_model: Mapped[Optional[str]] = mapped_column(String(80))
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("user_id", "specification_hash", name="uq_analysis_tool_specification"),
        Index("ix_analysis_tool_discovery", "user_id", "status", "intent_signature"),
    )


class AnalysisToolRun(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    """Privacy-minimized execution audit for a generated analysis tool."""

    __tablename__ = "analysis_tool_runs"
    tool_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analysis_tools.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    trace: Mapped[list] = mapped_column(JSON, default=list)
    result_hash: Mapped[Optional[str]] = mapped_column(String(64))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[Optional[str]] = mapped_column(String(80))


class FinancialObservation(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "financial_observations"
    source_type: Mapped[str] = mapped_column(String(30), index=True)
    source_message_id: Mapped[Optional[str]] = mapped_column(String(255))
    source_hash: Mapped[str] = mapped_column(String(64))
    external_transaction_id: Mapped[Optional[str]] = mapped_column(String(255))
    source_account: Mapped[Optional[str]] = mapped_column(String(120))
    transaction_type: Mapped[str] = mapped_column(String(30))
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    merchant_raw: Mapped[Optional[str]] = mapped_column(String(255))
    merchant_normalized: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    transaction_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now(), index=True)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reference_number: Mapped[Optional[str]] = mapped_column(String(120))
    description: Mapped[Optional[str]] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())
    raw_reference: Mapped[Optional[str]] = mapped_column(String(255))
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("0.8"))
    processing_state: Mapped[str] = mapped_column(String(30), default=ObservationProcessingState.RECEIVED.value)
    __table_args__ = (
        UniqueConstraint("user_id", "source_type", "source_hash", name="uq_observation_source_hash"),
        UniqueConstraint("user_id", "source_type", "source_message_id", name="uq_observation_message_id"),
        UniqueConstraint("user_id", "source_type", "external_transaction_id", name="uq_observation_external_transaction_id"),
        Index("ix_observation_candidates", "user_id", "amount_minor", "currency", "transaction_type", "transaction_at"),
    )


class TransactionSource(UUIDPrimaryKeyMixin, TransactionChildMixin, ConfidenceMixin, TimestampMixin, Base):
    __tablename__ = "transaction_sources"
    observation_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("financial_observations.id"), unique=True)
    source_type: Mapped[str] = mapped_column(String(30), index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(255))
    source_account: Mapped[Optional[str]] = mapped_column(String(120))
    source_message_id: Mapped[Optional[str]] = mapped_column(String(255))
    source_hash: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())
    raw_reference: Mapped[Optional[str]] = mapped_column(String(255))
    field_values: Mapped[dict] = mapped_column(JSON, default=dict)
    transaction: Mapped[Transaction] = relationship(back_populates="sources")
    __table_args__ = (UniqueConstraint("source_type", "source_hash", name="uq_transaction_source_hash"),)


class ReconciliationCandidate(UUIDPrimaryKeyMixin, UserOwnedMixin, TransactionChildMixin, TimestampMixin, Base):
    __tablename__ = "reconciliation_candidates"
    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("financial_observations.id", ondelete="CASCADE"), index=True)
    score: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    matching_signals: Mapped[dict] = mapped_column(JSON)
    decision: Mapped[str] = mapped_column(String(30), index=True)


class ReconciliationDecision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "reconciliation_decisions"
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reconciliation_candidates.id", ondelete="CASCADE"), index=True)
    decision: Mapped[str] = mapped_column(String(30))
    decided_by: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text)


class Budget(UUIDPrimaryKeyMixin, CurrencyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "budgets"
    category_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("categories.id"))
    name: Mapped[str] = mapped_column(String(120))
    amount_minor: Mapped[int] = mapped_column(Integer)
    period: Mapped[str] = mapped_column(String(30), default="monthly")


class Goal(UUIDPrimaryKeyMixin, CurrencyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "goals"
    name: Mapped[str] = mapped_column(String(120))
    target_minor: Mapped[int] = mapped_column(Integer)
    current_minor: Mapped[int] = mapped_column(Integer, default=0)
    target_date: Mapped[Optional[date]] = mapped_column(Date)


class GoalContribution(UUIDPrimaryKeyMixin, CurrencyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "goal_contributions"
    goal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("transactions.id", ondelete="SET NULL"), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    contribution_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now(), index=True)
    note: Mapped[Optional[str]] = mapped_column(String(240))
    __table_args__ = (Index("ix_goal_contribution_history", "user_id", "goal_id", "contribution_at"),)


class SavedAnalysis(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "saved_analyses"
    title: Mapped[str] = mapped_column(String(180))
    analysis_type: Mapped[str] = mapped_column(String(60))
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class UserPreference(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "user_preferences"
    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[dict] = mapped_column(JSON)
    authority: Mapped[str] = mapped_column(String(40), default="user")
    __table_args__ = (UniqueConstraint("user_id", "key"),)


class AIAction(UUIDPrimaryKeyMixin, UserOwnedMixin, ConversationChildMixin, TimestampMixin, Base):
    __tablename__ = "ai_actions"
    action_type: Mapped[str] = mapped_column(String(80))
    payload_redacted: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30))


class RecurringTransaction(UUIDPrimaryKeyMixin, CurrencyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "recurring_transactions"
    merchant_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("merchants.id"), index=True)
    expected_amount_minor: Mapped[int] = mapped_column(Integer)
    cadence: Mapped[str] = mapped_column(String(30))
    next_expected_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("0.5"))


class Subscription(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"
    recurring_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("recurring_transactions.id"), unique=True)
    name: Mapped[str] = mapped_column(String(140))
    status: Mapped[str] = mapped_column(String(30), default=ACTIVE_STATUS)


class FinancialInsight(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "financial_insights"
    insight_type: Mapped[str] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(180))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Import(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "imports"
    source_type: Mapped[str] = mapped_column(String(30))
    filename: Mapped[Optional[str]] = mapped_column(String(255))
    file_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default=ImportStatus.PROCESSING.value, index=True)
    total_records: Mapped[int] = mapped_column(Integer, default=0)
    high_confidence_records: Mapped[int] = mapped_column(Integer, default=0)
    review_records: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_records: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("user_id", "source_type", "file_hash", name="uq_import_file_hash"),)


class ImportRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "import_records"
    import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("imports.id", ondelete="CASCADE"), index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    observation_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("financial_observations.id"), index=True)
    status: Mapped[str] = mapped_column(String(30))
    errors: Mapped[list] = mapped_column(JSON, default=list)
    observation_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("import_id", "row_number"),)


class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_logs"
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(index=True)
    metadata_redacted: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now(), index=True)

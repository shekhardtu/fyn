from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
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
from .domain import ACTIVE_STATUS, AgentInterruptStatus, AgentRunStatus, AnalysisToolStatus, CONVERSATION_TITLE_MAX, DraftState, FinancialSourceType, IdentitySource, ImportStatus, ObservationProcessingState, SpendNature, TaxonomyScope, TransactionStatus, TransactionType
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
    title: Mapped[str] = mapped_column(String(CONVERSATION_TITLE_MAX), default="New conversation")
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
    # Assistant rows are reserved when a turn is admitted so created_at can
    # preserve transcript order. Delivery is a distinct instant: the point at
    # which the completed message became available to the client.
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
        server_default=func.now(),
    )
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class AgentRun(UUIDPrimaryKeyMixin, UserOwnedMixin, ConversationChildMixin, TimestampMixin, Base):
    """One durable AG-UI execution, independent of an HTTP connection."""

    __tablename__ = "agent_runs"
    parent_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        index=True,
    )
    blocked_by_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        index=True,
    )
    client_message_id: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    final_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(24), default=AgentRunStatus.QUEUED.value, index=True)
    task_status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    failure_stage: Mapped[Optional[str]] = mapped_column(String(80))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    input_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    delivery_mode: Mapped[str] = mapped_column(String(32), default="verified_final")
    last_sequence: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    first_response_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[Optional[str]] = mapped_column(String(80))
    # Only safe, read-only work may install a recovery phase. In particular,
    # post-answer enrichment can resume without replaying the financial turn
    # that produced the canonical message.
    recovery_phase: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    recovery_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    recovery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    recovery_claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )
    __table_args__ = (
        Index("ix_agent_run_thread_status_created", "conversation_id", "status", "created_at"),
        Index(
            "ix_agent_run_recovery_queue",
            "status",
            "created_at",
            "id",
            "recovery_claimed_at",
        ),
        UniqueConstraint("user_id", "client_message_id", name="uq_agent_run_user_client_message"),
    )

    @property
    def duration_ms(self) -> Optional[float]:
        if self.started_at is None or self.finished_at is None:
            return None
        return round((as_utc(self.finished_at) - as_utc(self.started_at)).total_seconds() * 1000, 1)

    @property
    def time_to_first_response_ms(self) -> Optional[float]:
        if self.started_at is None or self.first_response_at is None:
            return None
        return round((as_utc(self.first_response_at) - as_utc(self.started_at)).total_seconds() * 1000, 1)


class AgentEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An ordered AG-UI event retained for reconnect and audit."""

    __tablename__ = "agent_events"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_agent_event_run_sequence"),)


class AgentInterrupt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A resumable human decision emitted by a completed AG-UI run."""

    __tablename__ = "agent_interrupts"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    resolved_by_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        index=True,
    )
    tool_call_id: Mapped[str] = mapped_column(String(120), unique=True)
    widget_id: Mapped[str] = mapped_column(String(120), index=True)
    reason: Mapped[str] = mapped_column(String(120))
    message: Mapped[Optional[str]] = mapped_column(Text)
    response_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    metadata_payload: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default=AgentInterruptStatus.OPEN.value, index=True)
    response_payload: Mapped[Optional[dict]] = mapped_column(JSON)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (Index("ix_agent_interrupt_run_status", "run_id", "status"),)


class TransactionDraft(UUIDPrimaryKeyMixin, CurrencyMixin, UserOwnedMixin, ConversationChildMixin, TimestampMixin, Base):
    __tablename__ = "transaction_drafts"
    raw_text: Mapped[str] = mapped_column(Text)
    transaction_type: Mapped[str] = mapped_column(String(30), default=TransactionType.UNKNOWN.value)
    amount_minor: Mapped[Optional[int]] = mapped_column(BigInteger)
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
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
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
        CheckConstraint("row_version > 0", name="ck_transaction_positive_version"),
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


class TransactionRevision(UUIDPrimaryKeyMixin, TransactionChildMixin, TimestampMixin, Base):
    """Immutable, user-visible history for one canonical transaction.

    ``row_version`` protects the live row from stale writes; this table explains
    how each version came to exist. Snapshots make an amendment independently
    auditable even when taxonomy labels or related records change later.
    """

    __tablename__ = "transaction_revisions"
    revision_number: Mapped[int] = mapped_column(Integer)
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
    )
    source: Mapped[str] = mapped_column(String(40), index=True)
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"),
        index=True,
    )
    widget_id: Mapped[Optional[str]] = mapped_column(String(160))
    reason: Mapped[Optional[str]] = mapped_column(Text)
    before_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    after_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        CheckConstraint("revision_number > 0", name="ck_transaction_revision_positive_number"),
        UniqueConstraint("transaction_id", "revision_number", name="uq_transaction_revision_number"),
        Index("ix_transaction_revision_history", "transaction_id", "revision_number"),
    )


class SharedRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Reusable collaboration root for a two-sided financial record.

    Product modules attach their own typed aggregate to this row. Participants,
    invitations, documents, activity, and notification delivery can therefore
    be reused without teaching them what a loan, reimbursement, or rent plan is.
    """

    __tablename__ = "shared_records"
    kind: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
    )
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        CheckConstraint("row_version > 0", name="ck_shared_record_positive_version"),
    )


class SharedRecordParticipant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "shared_record_participants"
    shared_record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_records.id", ondelete="CASCADE"),
        index=True,
    )
    # This is membership, not ownership: deleting an account detaches it while
    # leaving an acknowledged record available to the other participant.
    member_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
    )
    role: Mapped[str] = mapped_column(String(30))
    display_name: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(30), default="invited", index=True)
    verification_channel: Mapped[Optional[str]] = mapped_column(String(20))
    verification_claim: Mapped[Optional[str]] = mapped_column(String(40))
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    hidden_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("shared_record_id", "role", name="uq_shared_record_participant_role"),
        UniqueConstraint("shared_record_id", "member_user_id", name="uq_shared_record_participant_member"),
    )


class SharedRecordInvitation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "shared_record_invitations"
    shared_record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_records.id", ondelete="CASCADE"),
        index=True,
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_record_participants.id", ondelete="CASCADE"),
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(20))
    destination_hash: Mapped[str] = mapped_column(String(64), index=True)
    destination_ciphertext: Mapped[str] = mapped_column(Text)
    destination_masked: Mapped[str] = mapped_column(String(320))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    exchanged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    redeemed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    send_count: Mapped[int] = mapped_column(Integer, default=0)
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("send_count >= 0", name="ck_shared_invitation_send_count"),
    )


class SharedDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "shared_documents"
    shared_record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_records.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    current_revision_number: Mapped[Optional[int]] = mapped_column(Integer)
    template_key: Mapped[str] = mapped_column(String(80), default="personal_loan_acknowledgement")
    template_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        CheckConstraint("template_version > 0", name="ck_shared_document_template_version"),
        CheckConstraint("current_revision_number IS NULL OR current_revision_number > 0", name="ck_shared_document_current_revision"),
    )


class DocumentRevision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "document_revisions"
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_documents.id", ondelete="CASCADE"),
        index=True,
    )
    revision_number: Mapped[int] = mapped_column(Integer)
    base_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("document_revisions.id", ondelete="SET NULL"),
        index=True,
    )
    authored_by_participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_record_participants.id"),
        index=True,
    )
    state: Mapped[str] = mapped_column(String(30), default="proposed", index=True)
    content: Mapped[dict] = mapped_column(JSON)
    change_summary: Mapped[list] = mapped_column(JSON, default=list)
    content_schema_version: Mapped[int] = mapped_column(Integer, default=1)
    source_snapshot_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    # The content and every supporting file are two different authenticity
    # surfaces.  Keeping both hashes, plus their combined evidence hash, makes
    # it impossible to replace an attachment without producing a new revision.
    manifest_hash: Mapped[str] = mapped_column(String(64), index=True)
    evidence_hash: Mapped[str] = mapped_column(String(64), index=True)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("document_id", "revision_number", name="uq_document_revision_number"),
        CheckConstraint("revision_number > 0", name="ck_document_revision_positive_number"),
        CheckConstraint("content_schema_version > 0", name="ck_document_revision_schema_version"),
    )


class DocumentChange(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "document_changes"
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_revisions.id", ondelete="CASCADE"),
        index=True,
    )
    authored_by_participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_record_participants.id"),
        index=True,
    )
    field_path: Mapped[str] = mapped_column(String(160))
    before_value: Mapped[Optional[dict]] = mapped_column(JSON)
    after_value: Mapped[Optional[dict]] = mapped_column(JSON)
    summary: Mapped[str] = mapped_column(String(240))


class DocumentAcceptance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "document_acceptances"
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_revisions.id", ondelete="CASCADE"),
        index=True,
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_record_participants.id"),
        index=True,
    )
    content_hash: Mapped[str] = mapped_column(String(64))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    evidence_hash: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(20), default="accepted")
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
    )
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now())
    statement_version: Mapped[int] = mapped_column(Integer, default=1)
    statement_text: Mapped[str] = mapped_column(String(500))
    auth_method: Mapped[str] = mapped_column(String(40), default="verified_session")
    actor_identifier_masked: Mapped[Optional[str]] = mapped_column(String(320))
    actor_timezone: Mapped[str] = mapped_column(String(80), default=DEFAULT_TIMEZONE)
    request_ip_hash: Mapped[Optional[str]] = mapped_column(String(64))
    user_agent_hash: Mapped[Optional[str]] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("revision_id", "participant_id", name="uq_document_acceptance_participant"),
        CheckConstraint("statement_version > 0", name="ck_document_acceptance_statement_version"),
    )


class DocumentAsset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One private, validated file that can be bound to a document revision.

    The bytes live in object storage (a private filesystem adapter in local
    development); PostgreSQL owns authorization, integrity, and lifecycle.
    Draft assets are user-owned until they are atomically attached to a shared
    document, after which both record participants may read but not replace it.
    """

    __tablename__ = "document_assets"
    owner_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
    )
    document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("shared_documents.id", ondelete="CASCADE"),
        index=True,
    )
    uploaded_by_participant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("shared_record_participants.id", ondelete="SET NULL"),
        index=True,
    )
    original_filename: Mapped[str] = mapped_column(String(240))
    media_type: Mapped[str] = mapped_column(String(80))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_key: Mapped[str] = mapped_column(String(160), unique=True)
    state: Mapped[str] = mapped_column(String(30), default="quarantined", index=True)
    classification: Mapped[str] = mapped_column(String(50), default="supporting_evidence", index=True)
    description: Mapped[Optional[str]] = mapped_column(String(240))
    validated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("byte_size > 0", name="ck_document_asset_positive_size"),
    )


class DocumentRevisionAsset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "document_revision_assets"
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_revisions.id", ondelete="CASCADE"),
        index=True,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_assets.id", ondelete="RESTRICT"),
        index=True,
    )
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        UniqueConstraint("revision_id", "asset_id", name="uq_document_revision_asset"),
        CheckConstraint("display_order >= 0", name="ck_document_revision_asset_order"),
    )


class DocumentRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A reusable participant-to-participant request for revision evidence."""

    __tablename__ = "document_requests"
    shared_record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_records.id", ondelete="CASCADE"),
        index=True,
    )
    requested_by_participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_record_participants.id", ondelete="CASCADE"),
        index=True,
    )
    requested_from_participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_record_participants.id", ondelete="CASCADE"),
        index=True,
    )
    label: Mapped[str] = mapped_column(String(120))
    classification: Mapped[str] = mapped_column(String(50), default="supporting_evidence")
    instructions: Mapped[Optional[str]] = mapped_column(String(500))
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[str] = mapped_column(String(30), default="requested", index=True)
    fulfilled_asset_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("document_assets.id", ondelete="SET NULL"),
        index=True,
    )
    fulfilled_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("document_revisions.id", ondelete="SET NULL"),
        index=True,
    )
    fulfilled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("requested_by_participant_id <> requested_from_participant_id", name="ck_document_request_distinct_participants"),
    )


class PersonalLoanAgreement(UUIDPrimaryKeyMixin, CurrencyMixin, TimestampMixin, Base):
    __tablename__ = "personal_loan_agreements"
    shared_record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_records.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(30), default="pending_acceptance", index=True)
    funding_status: Mapped[str] = mapped_column(String(30), default="pending_confirmation", index=True)
    intent: Mapped[str] = mapped_column(String(40), default="record_given", index=True)
    current_terms_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        CheckConstraint("current_terms_version > 0", name="ck_personal_loan_terms_version"),
    )


class LoanSecurityItem(UUIDPrimaryKeyMixin, CurrencyMixin, TimestampMixin, Base):
    """A descriptive assurance item; Fyn never values, enforces, or holds it."""

    __tablename__ = "loan_security_items"
    agreement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("personal_loan_agreements.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(40), index=True)
    description: Mapped[str] = mapped_column(String(240))
    masked_identifier: Mapped[Optional[str]] = mapped_column(String(120))
    stated_value_minor: Mapped[Optional[int]] = mapped_column(Integer)
    provided_by_participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_record_participants.id"),
        index=True,
    )
    held_by_participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_record_participants.id"),
        index=True,
    )
    state: Mapped[str] = mapped_column(String(40), default="acknowledged", index=True)
    returned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    return_confirmed_by_participant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("shared_record_participants.id"),
        index=True,
    )
    __table_args__ = (
        CheckConstraint(
            "stated_value_minor IS NULL OR stated_value_minor >= 0",
            name="ck_loan_security_nonnegative_value",
        ),
        CheckConstraint(
            "provided_by_participant_id <> held_by_participant_id",
            name="ck_loan_security_distinct_custody",
        ),
    )


class LoanTermVersion(UUIDPrimaryKeyMixin, CurrencyMixin, TimestampMixin, Base):
    __tablename__ = "loan_term_versions"
    agreement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("personal_loan_agreements.id", ondelete="CASCADE"),
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer)
    principal_minor: Mapped[int] = mapped_column(Integer)
    interest_rate_bps: Mapped[int] = mapped_column(Integer, default=0)
    interest_period: Mapped[str] = mapped_column(String(16), default="yearly")
    interest_mode: Mapped[str] = mapped_column(String(16), default="simple")
    interest_method: Mapped[str] = mapped_column(String(30), default="none")
    calculation_basis: Mapped[str] = mapped_column(String(30), default="actual_365")
    rounding_policy: Mapped[str] = mapped_column(String(30), default="half_up_minor_unit")
    money_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    note: Mapped[Optional[str]] = mapped_column(Text)
    schedule: Mapped[list] = mapped_column(JSON, default=list)
    total_interest_minor: Mapped[int] = mapped_column(Integer, default=0)
    total_repayable_minor: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(30), default="proposed", index=True)
    proposed_by_participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_record_participants.id"),
        index=True,
    )
    document_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("document_revisions.id", ondelete="SET NULL"),
        index=True,
    )
    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    effective_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("agreement_id", "version", name="uq_loan_term_version"),
        CheckConstraint("version > 0", name="ck_loan_term_positive_version"),
        CheckConstraint("principal_minor > 0", name="ck_loan_term_positive_principal"),
        CheckConstraint("interest_rate_bps >= 0", name="ck_loan_term_nonnegative_rate"),
        CheckConstraint("interest_period IN ('monthly', 'yearly')", name="ck_loan_term_interest_period"),
        CheckConstraint("interest_mode IN ('simple', 'compound')", name="ck_loan_term_interest_mode"),
        CheckConstraint("total_interest_minor >= 0", name="ck_loan_term_nonnegative_interest"),
        CheckConstraint("total_repayable_minor >= principal_minor", name="ck_loan_term_total_repayable"),
        CheckConstraint("due_date >= money_date", name="ck_loan_term_date_order"),
    )


class LoanCashflow(UUIDPrimaryKeyMixin, CurrencyMixin, TimestampMixin, Base):
    __tablename__ = "loan_cashflows"
    agreement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("personal_loan_agreements.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(30), index=True)
    state: Mapped[str] = mapped_column(String(30), default="proposed", index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    principal_minor: Mapped[int] = mapped_column(Integer)
    interest_minor: Mapped[int] = mapped_column(Integer, default=0)
    occurred_on: Mapped[date] = mapped_column(Date, index=True)
    initiated_by_participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_record_participants.id"),
        index=True,
    )
    confirmed_by_participant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("shared_record_participants.id"),
        index=True,
    )
    note: Mapped[Optional[str]] = mapped_column(Text)
    reversal_of_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("loan_cashflows.id", ondelete="SET NULL"),
        index=True,
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_loan_cashflow_positive_amount"),
        CheckConstraint("principal_minor >= 0", name="ck_loan_cashflow_nonnegative_principal"),
        CheckConstraint("interest_minor >= 0", name="ck_loan_cashflow_nonnegative_interest"),
        CheckConstraint("amount_minor = principal_minor + interest_minor", name="ck_loan_cashflow_breakdown"),
    )


class LoanReminder(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "loan_reminders"
    agreement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("personal_loan_agreements.id", ondelete="CASCADE"),
        index=True,
    )
    requested_by_participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_record_participants.id"),
        index=True,
    )
    recipient_participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_record_participants.id"),
        index=True,
    )
    tone: Mapped[str] = mapped_column(String(30), default="friendly")
    note: Mapped[Optional[str]] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now(), index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class NotificationOutbox(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notification_outbox"
    shared_record_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("shared_records.id", ondelete="CASCADE"),
        index=True,
    )
    recipient_participant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("shared_record_participants.id", ondelete="CASCADE"),
        index=True,
    )
    topic: Mapped[str] = mapped_column(String(60), index=True)
    channel: Mapped[str] = mapped_column(String(20), index=True)
    destination_ciphertext: Mapped[str] = mapped_column(Text)
    destination_masked: Mapped[str] = mapped_column(String(320))
    context_ciphertext: Mapped[Optional[str]] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, server_default=func.now(), index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[Optional[str]] = mapped_column(String(80))
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_notification_outbox_attempts"),
    )


class SharedRecordEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "shared_record_events"
    shared_record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_records.id", ondelete="CASCADE"),
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    actor_participant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("shared_record_participants.id", ondelete="SET NULL"),
        index=True,
    )
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    previous_hash: Mapped[Optional[str]] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)
    __table_args__ = (
        UniqueConstraint("shared_record_id", "sequence", name="uq_shared_record_event_sequence"),
        CheckConstraint("sequence > 0", name="ck_shared_record_event_positive_sequence"),
    )


class CommandReceipt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "command_receipts"
    shared_record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shared_records.id", ondelete="CASCADE"),
        index=True,
    )
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
    )
    command_type: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(120))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint("actor_user_id", "command_type", "idempotency_key", name="uq_command_receipt_idempotency"),
    )


class Loan(UUIDPrimaryKeyMixin, CurrencyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "loans"
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), index=True)
    shared_record_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("shared_records.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(140))
    loan_type: Mapped[str] = mapped_column(String(40), default="home")
    lender: Mapped[Optional[str]] = mapped_column(String(140))
    direction: Mapped[Optional[str]] = mapped_column(String(20))
    counterparty_name: Mapped[Optional[str]] = mapped_column(String(120))
    outstanding_principal_minor: Mapped[int] = mapped_column(Integer)
    accrued_interest_minor: Mapped[int] = mapped_column(Integer, default=0)
    annual_rate_percent: Mapped[Decimal] = mapped_column(Numeric(7, 4))
    rate_type: Mapped[str] = mapped_column(String(20), default="floating")
    remaining_tenure_months: Mapped[int] = mapped_column(Integer)
    current_emi_minor: Mapped[Optional[int]] = mapped_column(Integer)
    next_due_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    next_due_minor: Mapped[Optional[int]] = mapped_column(Integer)
    response_needed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    last_projected_event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    prepayment_fee_percent: Mapped[Decimal] = mapped_column(Numeric(7, 4), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(20), default=ACTIVE_STATUS, index=True)
    __table_args__ = (
        UniqueConstraint("user_id", "shared_record_id", name="uq_loan_projection_shared_record"),
        CheckConstraint("accrued_interest_minor >= 0", name="ck_loan_nonnegative_accrued_interest"),
        CheckConstraint("last_projected_event_sequence >= 0", name="ck_loan_projection_event_sequence"),
    )


class LoanScenario(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "loan_scenarios"
    loan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("loans.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    objective: Mapped[str] = mapped_column(String(40))
    inputs: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)


class AnalysisToolTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Shared, value-free recipe for a generated read-only analysis.

    A template contains only governed structure. Customer filters, dates,
    timezone, limits, and presentation text are bound on a user-scoped run and
    must never be persisted here.
    """

    __tablename__ = "analysis_tool_templates"
    capability_name: Mapped[str] = mapped_column(String(120))
    capability_description: Mapped[str] = mapped_column(Text)
    capability_signature: Mapped[str] = mapped_column(String(240), index=True)
    template_version: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(24), default=AnalysisToolStatus.DRAFT.value, index=True)
    semantic_registry_version: Mapped[str] = mapped_column(String(60), index=True)
    source_manifest_hash: Mapped[str] = mapped_column(String(64), index=True)
    parameter_schema: Mapped[list] = mapped_column(JSON)
    plan_template: Mapped[dict] = mapped_column(JSON)
    template_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    validation_report: Mapped[dict] = mapped_column(JSON, default=dict)
    retrieval_embedding: Mapped[Optional[list]] = mapped_column(JSON)
    retrieval_embedding_model: Mapped[Optional[str]] = mapped_column(String(80))
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
    )
    __table_args__ = (
        Index("ix_analysis_template_discovery", "status", "capability_signature"),
    )


class DataSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One connected origin of queryable data.

    The native canonical ledger is a single product-owned row with no owner;
    every future upload or external connection is a user-owned row. Semantics
    live in versioned SourceManifest rows, never on the source itself.
    """

    __tablename__ = "data_sources"
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    # Connection material for non-native sources (external_db: url + table
    # allowlist). The url may embed credentials, so this column must NEVER be
    # serialized to any client surface — not API responses, manifest documents,
    # tool descriptions or payloads, logs, or user exports (the export path
    # redacts it explicitly in user_data.py).
    config: Mapped[Optional[dict]] = mapped_column(JSON)
    __table_args__ = (Index("ix_data_source_user_kind", "user_id", "kind"),)


class SourceManifest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One immutable, versioned semantic description of a data source.

    The document carries provenance-tagged sections (curated, profiled, and
    later user_stated annotations). A content change always lands as a new
    version; consumers key on manifest_hash the way templates key on the
    semantic registry hash.
    """

    __tablename__ = "source_manifests"
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"),
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    manifest_hash: Mapped[str] = mapped_column(String(64), index=True)
    document: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint("data_source_id", "version", name="uq_source_manifest_version"),
        Index("ix_source_manifest_active", "data_source_id", "status"),
    )


class SourceRecord(UUIDPrimaryKeyMixin, UserOwnedMixin, Base):
    """One raw row of a user-uploaded spreadsheet data source.

    Rows come from one user's cells, so they are user-owned and never
    product-global. Each upload replaces the source's rows wholesale; the
    versioned manifest, not the row set, carries history.
    """

    __tablename__ = "source_records"
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"),
        index=True,
    )
    row_index: Mapped[int] = mapped_column(Integer)
    record: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("data_source_id", "row_index", name="uq_source_record_row"),
    )


class SourceAnnotation(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    """A user's authoritative statement about one field of a data source.

    ``user_stated`` provenance: annotations survive re-scans and win over
    inference. One row per (source, field) — a newer statement replaces the
    row content, so the manifest always carries the user's latest word.
    """

    __tablename__ = "source_annotations"
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"),
        index=True,
    )
    field: Mapped[str] = mapped_column(String(120))
    statement: Mapped[str] = mapped_column(Text)
    # Optional structured half of the statement: when set, this role overrides
    # the inferred one everywhere deterministic code consumes roles, so a
    # user's correction actually changes query semantics, not just prose.
    role: Mapped[Optional[str]] = mapped_column(String(40))
    __table_args__ = (
        UniqueConstraint("data_source_id", "field", name="uq_source_annotation_field"),
    )


class EntityLink(UUIDPrimaryKeyMixin, UserOwnedMixin, ConfidenceMixin, TimestampMixin, Base):
    """One resolved ``alias -> canonical`` spelling for a counterparty.

    Identity resolution unifies the different spellings one user's own data
    carries for the same merchant across the canonical ledger, uploaded
    spreadsheets, and connected external databases. Every link is user-owned:
    a spelling that appears in one person's records is that person's data and
    never becomes a shared dictionary entry.

    ``confidence`` is inference, never a user statement, so it stays strictly
    below 1. ``source_id`` is SET NULL: disconnecting a source must not erase
    an identity the user already relies on.
    """

    __tablename__ = "entity_links"
    kind: Mapped[str] = mapped_column(String(20), index=True)
    canonical: Mapped[str] = mapped_column(String(160), index=True)
    alias: Mapped[str] = mapped_column(String(160))
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("data_sources.id", ondelete="SET NULL"),
        index=True,
    )
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "canonical", "alias", name="uq_entity_link_alias"),
    )


class UserTrait(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    """One deterministic customer-data-platform trait derived from canonical data.

    ``computed_at`` and ``freshness_note`` are not decoration: a trait is a
    summary of data as of a moment, so the stamp travels with the value
    everywhere the value is read or surfaced. A trait without its stamp would
    read as current forever.
    """

    __tablename__ = "user_traits"
    name: Mapped[str] = mapped_column(String(80), index=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, server_default=func.now()
    )
    freshness_note: Mapped[str] = mapped_column(String(200))
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_user_trait_name"),)


class UserAnalysisTool(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    """A customer's saved view of a shared analysis template."""

    __tablename__ = "user_analysis_tools"
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_tool_templates.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    intent_signature: Mapped[str] = mapped_column(String(160), index=True)
    status: Mapped[str] = mapped_column(String(24), default=AnalysisToolStatus.ACTIVE.value, index=True)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("user_id", "template_id", name="uq_user_analysis_tool_template"),
        Index("ix_user_analysis_tool_discovery", "user_id", "status", "intent_signature"),
    )


class AnalysisToolRun(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    """User-scoped invocation and readable execution audit."""

    __tablename__ = "analysis_tool_runs"
    template_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("analysis_tool_templates.id", ondelete="SET NULL"),
        index=True,
    )
    user_tool_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("user_analysis_tools.id", ondelete="SET NULL"),
        index=True,
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    trace: Mapped[list] = mapped_column(JSON, default=list)
    result_hash: Mapped[Optional[str]] = mapped_column(String(64))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[Optional[str]] = mapped_column(String(80))
    # The conversation message owns customer prose. A one-way identity avoids
    # duplicating it here while still enabling exact, user-scoped replay.
    question_hash: Mapped[Optional[str]] = mapped_column(String(64))
    run_date: Mapped[Optional[date]] = mapped_column(Date)
    display_names: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        Index(
            "ix_analysis_tool_run_replay",
            "user_id",
            "question_hash",
            "status",
            "created_at",
        ),
    )


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


class Dashboard(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "dashboards"
    name: Mapped[str] = mapped_column(String(120))


class DashboardTile(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    """One pinned analysis. ``spec`` stores the bound proposal, never a result:
    every read re-executes it through the governed harness."""

    __tablename__ = "dashboard_tiles"
    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dashboards.id", ondelete="CASCADE"),
        index=True,
    )
    title: Mapped[str] = mapped_column(String(160))
    position: Mapped[int] = mapped_column(Integer, default=0)
    # {"kind": "plan", "proposal": <bound AnalysisToolProposal JSON>}
    spec: Mapped[dict] = mapped_column(JSON)


class UserPreference(UUIDPrimaryKeyMixin, UserOwnedMixin, TimestampMixin, Base):
    __tablename__ = "user_preferences"
    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[dict] = mapped_column(JSON)
    authority: Mapped[str] = mapped_column(String(40), default="user")
    __table_args__ = (UniqueConstraint("user_id", "key"),)


class LocationLabel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One place name per ~150m cell, shared by every user on this instance.

    Not user-owned, and deliberately so: a geohash cell's name is a fact about
    the world, not about anyone. Keying it per user would multiply identical
    lookups against a provider whose terms require caching, and would leak the
    same coordinates into as many rows as there are people who went there.

    A lookup that found nothing is recorded too, with `display` null. Otherwise
    every save at an unnamed cell — a highway, open water, a new development —
    would re-ask the provider forever.
    """

    __tablename__ = "location_labels"
    # The geohash is the natural key, but every entity here takes a UUID
    # primary key so identity is uniform across the schema; the cell keeps its
    # uniqueness as a constraint instead.
    geohash: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    city: Mapped[Optional[str]] = mapped_column(String(120))
    state: Mapped[Optional[str]] = mapped_column(String(120))
    country: Mapped[Optional[str]] = mapped_column(String(120))
    # What a transaction row shows. Null means the provider answered and had no
    # name for this cell, which is different from never having asked.
    display: Mapped[Optional[str]] = mapped_column(String(160))
    provider: Mapped[str] = mapped_column(String(40))


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
    # What the claim is about — a category slug, a recurring charge, the income
    # stream. Restating a claim rewrites its row rather than appending a new one.
    subject: Mapped[str] = mapped_column(String(160), default="")
    title: Mapped[str] = mapped_column(String(180))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    # The deterministic parameters that reproduce the claim. Without them a
    # stored insight can only be believed, never rechecked.
    recompute_key: Mapped[dict] = mapped_column(JSON, default=dict)
    # {"manifestHash": ..., "traitsComputedAt": ..., "computedAt": ...}
    lineage: Mapped[dict] = mapped_column(JSON, default=dict)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Stamped when a replay failed to reproduce the claim. A row carrying it is
    # history, never a current insight.
    stale_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("user_id", "insight_type", "subject", name="uq_financial_insight_subject"),
    )


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

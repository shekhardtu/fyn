"""Add reusable shared records and the personal lending aggregate.

Revision ID: 0004_personal_lending
Revises: 0003_location_label_cache
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0004_personal_lending"
down_revision: Union[str, None] = "0003_location_label_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "shared_records",
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("row_version > 0", name="ck_shared_record_positive_version"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shared_records_kind", "shared_records", ["kind"], unique=False)
    op.create_index("ix_shared_records_status", "shared_records", ["status"], unique=False)
    op.create_index("ix_shared_records_created_by_user_id", "shared_records", ["created_by_user_id"], unique=False)

    op.create_table(
        "shared_record_participants",
        sa.Column("shared_record_id", sa.Uuid(), nullable=False),
        sa.Column("member_user_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("verification_channel", sa.String(length=20), nullable=True),
        sa.Column("verification_claim", sa.String(length=40), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["member_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["shared_record_id"], ["shared_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shared_record_id", "member_user_id", name="uq_shared_record_participant_member"),
        sa.UniqueConstraint("shared_record_id", "role", name="uq_shared_record_participant_role"),
    )
    op.create_index("ix_shared_record_participants_member_user_id", "shared_record_participants", ["member_user_id"], unique=False)
    op.create_index("ix_shared_record_participants_shared_record_id", "shared_record_participants", ["shared_record_id"], unique=False)
    op.create_index("ix_shared_record_participants_state", "shared_record_participants", ["state"], unique=False)

    op.create_table(
        "shared_record_invitations",
        sa.Column("shared_record_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("destination_hash", sa.String(length=64), nullable=False),
        sa.Column("destination_ciphertext", sa.Text(), nullable=False),
        sa.Column("destination_masked", sa.String(length=320), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchanged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("send_count", sa.Integer(), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("send_count >= 0", name="ck_shared_invitation_send_count"),
        sa.ForeignKeyConstraint(["participant_id"], ["shared_record_participants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shared_record_id"], ["shared_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shared_record_invitations_destination_hash", "shared_record_invitations", ["destination_hash"], unique=False)
    op.create_index("ix_shared_record_invitations_expires_at", "shared_record_invitations", ["expires_at"], unique=False)
    op.create_index("ix_shared_record_invitations_participant_id", "shared_record_invitations", ["participant_id"], unique=False)
    op.create_index("ix_shared_record_invitations_shared_record_id", "shared_record_invitations", ["shared_record_id"], unique=False)
    op.create_index("ix_shared_record_invitations_token_hash", "shared_record_invitations", ["token_hash"], unique=True)

    op.create_table(
        "shared_documents",
        sa.Column("shared_record_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("current_revision_number", sa.Integer(), nullable=True),
        sa.Column("template_key", sa.String(length=80), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("current_revision_number IS NULL OR current_revision_number > 0", name="ck_shared_document_current_revision"),
        sa.CheckConstraint("template_version > 0", name="ck_shared_document_template_version"),
        sa.ForeignKeyConstraint(["shared_record_id"], ["shared_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shared_documents_kind", "shared_documents", ["kind"], unique=False)
    op.create_index("ix_shared_documents_shared_record_id", "shared_documents", ["shared_record_id"], unique=False)
    op.create_index("ix_shared_documents_status", "shared_documents", ["status"], unique=False)

    op.create_table(
        "document_revisions",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("base_revision_id", sa.Uuid(), nullable=True),
        sa.Column("authored_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("change_summary", sa.JSON(), nullable=False),
        sa.Column("content_schema_version", sa.Integer(), nullable=False),
        sa.Column("source_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("content_schema_version > 0", name="ck_document_revision_schema_version"),
        sa.CheckConstraint("revision_number > 0", name="ck_document_revision_positive_number"),
        sa.ForeignKeyConstraint(["authored_by_participant_id"], ["shared_record_participants.id"]),
        sa.ForeignKeyConstraint(["base_revision_id"], ["document_revisions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["shared_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "revision_number", name="uq_document_revision_number"),
    )
    op.create_index("ix_document_revisions_authored_by_participant_id", "document_revisions", ["authored_by_participant_id"], unique=False)
    op.create_index("ix_document_revisions_base_revision_id", "document_revisions", ["base_revision_id"], unique=False)
    op.create_index("ix_document_revisions_content_hash", "document_revisions", ["content_hash"], unique=False)
    op.create_index("ix_document_revisions_document_id", "document_revisions", ["document_id"], unique=False)
    op.create_index("ix_document_revisions_state", "document_revisions", ["state"], unique=False)

    op.create_table(
        "document_changes",
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("authored_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column("field_path", sa.String(length=160), nullable=False),
        sa.Column("before_value", sa.JSON(), nullable=True),
        sa.Column("after_value", sa.JSON(), nullable=True),
        sa.Column("summary", sa.String(length=240), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["authored_by_participant_id"], ["shared_record_participants.id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["document_revisions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_changes_authored_by_participant_id", "document_changes", ["authored_by_participant_id"], unique=False)
    op.create_index("ix_document_changes_revision_id", "document_changes", ["revision_id"], unique=False)

    op.create_table(
        "document_acceptances",
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["participant_id"], ["shared_record_participants.id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["document_revisions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision_id", "participant_id", name="uq_document_acceptance_participant"),
    )
    op.create_index("ix_document_acceptances_actor_user_id", "document_acceptances", ["actor_user_id"], unique=False)
    op.create_index("ix_document_acceptances_participant_id", "document_acceptances", ["participant_id"], unique=False)
    op.create_index("ix_document_acceptances_revision_id", "document_acceptances", ["revision_id"], unique=False)

    op.create_table(
        "personal_loan_agreements",
        sa.Column("shared_record_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("funding_status", sa.String(length=30), nullable=False),
        sa.Column("current_terms_version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("current_terms_version > 0", name="ck_personal_loan_terms_version"),
        sa.ForeignKeyConstraint(["shared_record_id"], ["shared_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_personal_loan_agreements_funding_status", "personal_loan_agreements", ["funding_status"], unique=False)
    op.create_index("ix_personal_loan_agreements_shared_record_id", "personal_loan_agreements", ["shared_record_id"], unique=True)
    op.create_index("ix_personal_loan_agreements_status", "personal_loan_agreements", ["status"], unique=False)

    op.create_table(
        "loan_security_items",
        sa.Column("agreement_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("description", sa.String(length=240), nullable=False),
        sa.Column("masked_identifier", sa.String(length=120), nullable=True),
        sa.Column("stated_value_minor", sa.Integer(), nullable=True),
        sa.Column("provided_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column("held_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("return_confirmed_by_participant_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("provided_by_participant_id <> held_by_participant_id", name="ck_loan_security_distinct_custody"),
        sa.CheckConstraint("stated_value_minor IS NULL OR stated_value_minor >= 0", name="ck_loan_security_nonnegative_value"),
        sa.ForeignKeyConstraint(["agreement_id"], ["personal_loan_agreements.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["held_by_participant_id"], ["shared_record_participants.id"]),
        sa.ForeignKeyConstraint(["provided_by_participant_id"], ["shared_record_participants.id"]),
        sa.ForeignKeyConstraint(["return_confirmed_by_participant_id"], ["shared_record_participants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_loan_security_items_agreement_id", "loan_security_items", ["agreement_id"], unique=False)
    op.create_index("ix_loan_security_items_held_by_participant_id", "loan_security_items", ["held_by_participant_id"], unique=False)
    op.create_index("ix_loan_security_items_kind", "loan_security_items", ["kind"], unique=False)
    op.create_index("ix_loan_security_items_provided_by_participant_id", "loan_security_items", ["provided_by_participant_id"], unique=False)
    op.create_index("ix_loan_security_items_return_confirmed_by_participant_id", "loan_security_items", ["return_confirmed_by_participant_id"], unique=False)
    op.create_index("ix_loan_security_items_state", "loan_security_items", ["state"], unique=False)

    op.create_table(
        "loan_term_versions",
        sa.Column("agreement_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("principal_minor", sa.Integer(), nullable=False),
        sa.Column("annual_rate_bps", sa.Integer(), nullable=False),
        sa.Column("interest_method", sa.String(length=30), nullable=False),
        sa.Column("money_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("schedule", sa.JSON(), nullable=False),
        sa.Column("total_interest_minor", sa.Integer(), nullable=False),
        sa.Column("total_repayable_minor", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("proposed_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column("document_revision_id", sa.Uuid(), nullable=True),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("annual_rate_bps >= 0", name="ck_loan_term_nonnegative_rate"),
        sa.CheckConstraint("due_date >= money_date", name="ck_loan_term_date_order"),
        sa.CheckConstraint("principal_minor > 0", name="ck_loan_term_positive_principal"),
        sa.CheckConstraint("total_interest_minor >= 0", name="ck_loan_term_nonnegative_interest"),
        sa.CheckConstraint("total_repayable_minor >= principal_minor", name="ck_loan_term_total_repayable"),
        sa.CheckConstraint("version > 0", name="ck_loan_term_positive_version"),
        sa.ForeignKeyConstraint(["agreement_id"], ["personal_loan_agreements.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_revision_id"], ["document_revisions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["proposed_by_participant_id"], ["shared_record_participants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agreement_id", "version", name="uq_loan_term_version"),
    )
    op.create_index("ix_loan_term_versions_agreement_id", "loan_term_versions", ["agreement_id"], unique=False)
    op.create_index("ix_loan_term_versions_document_revision_id", "loan_term_versions", ["document_revision_id"], unique=False)
    op.create_index("ix_loan_term_versions_due_date", "loan_term_versions", ["due_date"], unique=False)
    op.create_index("ix_loan_term_versions_proposed_by_participant_id", "loan_term_versions", ["proposed_by_participant_id"], unique=False)
    op.create_index("ix_loan_term_versions_source_hash", "loan_term_versions", ["source_hash"], unique=False)
    op.create_index("ix_loan_term_versions_state", "loan_term_versions", ["state"], unique=False)

    op.create_table(
        "loan_cashflows",
        sa.Column("agreement_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("principal_minor", sa.Integer(), nullable=False),
        sa.Column("interest_minor", sa.Integer(), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("initiated_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column("confirmed_by_participant_id", sa.Uuid(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reversal_of_id", sa.Uuid(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("amount_minor = principal_minor + interest_minor", name="ck_loan_cashflow_breakdown"),
        sa.CheckConstraint("amount_minor > 0", name="ck_loan_cashflow_positive_amount"),
        sa.CheckConstraint("interest_minor >= 0", name="ck_loan_cashflow_nonnegative_interest"),
        sa.CheckConstraint("principal_minor >= 0", name="ck_loan_cashflow_nonnegative_principal"),
        sa.ForeignKeyConstraint(["agreement_id"], ["personal_loan_agreements.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["confirmed_by_participant_id"], ["shared_record_participants.id"]),
        sa.ForeignKeyConstraint(["initiated_by_participant_id"], ["shared_record_participants.id"]),
        sa.ForeignKeyConstraint(["reversal_of_id"], ["loan_cashflows.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_loan_cashflows_agreement_id", "loan_cashflows", ["agreement_id"], unique=False)
    op.create_index("ix_loan_cashflows_confirmed_by_participant_id", "loan_cashflows", ["confirmed_by_participant_id"], unique=False)
    op.create_index("ix_loan_cashflows_initiated_by_participant_id", "loan_cashflows", ["initiated_by_participant_id"], unique=False)
    op.create_index("ix_loan_cashflows_kind", "loan_cashflows", ["kind"], unique=False)
    op.create_index("ix_loan_cashflows_occurred_on", "loan_cashflows", ["occurred_on"], unique=False)
    op.create_index("ix_loan_cashflows_reversal_of_id", "loan_cashflows", ["reversal_of_id"], unique=False)
    op.create_index("ix_loan_cashflows_state", "loan_cashflows", ["state"], unique=False)

    op.create_table(
        "loan_reminders",
        sa.Column("agreement_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_participant_id", sa.Uuid(), nullable=False),
        sa.Column("tone", sa.String(length=30), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["agreement_id"], ["personal_loan_agreements.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_participant_id"], ["shared_record_participants.id"]),
        sa.ForeignKeyConstraint(["requested_by_participant_id"], ["shared_record_participants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_loan_reminders_agreement_id", "loan_reminders", ["agreement_id"], unique=False)
    op.create_index("ix_loan_reminders_recipient_participant_id", "loan_reminders", ["recipient_participant_id"], unique=False)
    op.create_index("ix_loan_reminders_requested_by_participant_id", "loan_reminders", ["requested_by_participant_id"], unique=False)
    op.create_index("ix_loan_reminders_scheduled_at", "loan_reminders", ["scheduled_at"], unique=False)
    op.create_index("ix_loan_reminders_state", "loan_reminders", ["state"], unique=False)

    op.create_table(
        "notification_outbox",
        sa.Column("shared_record_id", sa.Uuid(), nullable=True),
        sa.Column("recipient_participant_id", sa.Uuid(), nullable=True),
        sa.Column("topic", sa.String(length=60), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("destination_ciphertext", sa.Text(), nullable=False),
        sa.Column("destination_masked", sa.String(length=320), nullable=False),
        sa.Column("context_ciphertext", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("attempts >= 0", name="ck_notification_outbox_attempts"),
        sa.ForeignKeyConstraint(["recipient_participant_id"], ["shared_record_participants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shared_record_id"], ["shared_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_notification_outbox_available_at", "notification_outbox", ["available_at"], unique=False)
    op.create_index("ix_notification_outbox_channel", "notification_outbox", ["channel"], unique=False)
    op.create_index("ix_notification_outbox_recipient_participant_id", "notification_outbox", ["recipient_participant_id"], unique=False)
    op.create_index("ix_notification_outbox_shared_record_id", "notification_outbox", ["shared_record_id"], unique=False)
    op.create_index("ix_notification_outbox_state", "notification_outbox", ["state"], unique=False)
    op.create_index("ix_notification_outbox_topic", "notification_outbox", ["topic"], unique=False)

    op.create_table(
        "shared_record_events",
        sa.Column("shared_record_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("actor_participant_id", sa.Uuid(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("previous_hash", sa.String(length=64), nullable=True),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("sequence > 0", name="ck_shared_record_event_positive_sequence"),
        sa.ForeignKeyConstraint(["actor_participant_id"], ["shared_record_participants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["shared_record_id"], ["shared_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_hash"),
        sa.UniqueConstraint("shared_record_id", "sequence", name="uq_shared_record_event_sequence"),
    )
    op.create_index("ix_shared_record_events_actor_participant_id", "shared_record_events", ["actor_participant_id"], unique=False)
    op.create_index("ix_shared_record_events_event_type", "shared_record_events", ["event_type"], unique=False)
    op.create_index("ix_shared_record_events_shared_record_id", "shared_record_events", ["shared_record_id"], unique=False)

    op.create_table(
        "command_receipts",
        sa.Column("shared_record_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("command_type", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["shared_record_id"], ["shared_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_user_id", "command_type", "idempotency_key", name="uq_command_receipt_idempotency"),
    )
    op.create_index("ix_command_receipts_actor_user_id", "command_receipts", ["actor_user_id"], unique=False)
    op.create_index("ix_command_receipts_shared_record_id", "command_receipts", ["shared_record_id"], unique=False)

    op.add_column("loans", sa.Column("shared_record_id", sa.Uuid(), nullable=True))
    op.add_column("loans", sa.Column("direction", sa.String(length=20), nullable=True))
    op.add_column("loans", sa.Column("counterparty_name", sa.String(length=120), nullable=True))
    op.add_column("loans", sa.Column("accrued_interest_minor", sa.Integer(), server_default="0", nullable=False))
    op.add_column("loans", sa.Column("next_due_date", sa.Date(), nullable=True))
    op.add_column("loans", sa.Column("next_due_minor", sa.Integer(), nullable=True))
    op.add_column("loans", sa.Column("response_needed", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("loans", sa.Column("last_projected_event_sequence", sa.Integer(), server_default="0", nullable=False))
    op.execute(sa.text("UPDATE loans SET direction = 'borrowed' WHERE direction IS NULL"))
    op.create_foreign_key(None, "loans", "shared_records", ["shared_record_id"], ["id"], ondelete="CASCADE")
    op.create_unique_constraint("uq_loan_projection_shared_record", "loans", ["user_id", "shared_record_id"])
    op.create_check_constraint("ck_loan_nonnegative_accrued_interest", "loans", "accrued_interest_minor >= 0")
    op.create_check_constraint("ck_loan_projection_event_sequence", "loans", "last_projected_event_sequence >= 0")
    op.create_index("ix_loans_shared_record_id", "loans", ["shared_record_id"], unique=False)
    op.create_index("ix_loans_next_due_date", "loans", ["next_due_date"], unique=False)
    op.create_index("ix_loans_response_needed", "loans", ["response_needed"], unique=False)
    op.alter_column("loans", "accrued_interest_minor", server_default=None)
    op.alter_column("loans", "response_needed", server_default=None)
    op.alter_column("loans", "last_projected_event_sequence", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_loans_response_needed", table_name="loans")
    op.drop_index("ix_loans_next_due_date", table_name="loans")
    op.drop_index("ix_loans_shared_record_id", table_name="loans")
    op.drop_constraint("ck_loan_projection_event_sequence", "loans", type_="check")
    op.drop_constraint("ck_loan_nonnegative_accrued_interest", "loans", type_="check")
    op.drop_constraint("uq_loan_projection_shared_record", "loans", type_="unique")
    for constraint in sa.inspect(op.get_bind()).get_foreign_keys("loans"):
        if constraint.get("referred_table") == "shared_records":
            op.drop_constraint(constraint["name"], "loans", type_="foreignkey")
    for column in (
        "last_projected_event_sequence", "response_needed", "next_due_minor",
        "next_due_date", "accrued_interest_minor", "counterparty_name",
        "direction", "shared_record_id",
    ):
        op.drop_column("loans", column)

    for table in (
        "command_receipts",
        "shared_record_events",
        "notification_outbox",
        "loan_reminders",
        "loan_cashflows",
        "loan_term_versions",
        "loan_security_items",
        "personal_loan_agreements",
        "document_acceptances",
        "document_changes",
        "document_revisions",
        "shared_documents",
        "shared_record_invitations",
        "shared_record_participants",
        "shared_records",
    ):
        op.drop_table(table)

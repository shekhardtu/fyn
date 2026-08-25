"""Add calculation modes, reusable library assets, and evidence requests.

Revision ID: 0008_document_library_requests
Revises: 0007_interest_periods
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008_document_library_requests"
down_revision: Union[str, None] = "0007_interest_periods"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("loan_term_versions", sa.Column("interest_mode", sa.String(length=16), server_default="simple", nullable=False))
    op.alter_column("loan_term_versions", "interest_mode", server_default=None)
    op.create_check_constraint(
        "ck_loan_term_interest_mode",
        "loan_term_versions",
        "interest_mode IN ('simple', 'compound')",
    )

    # A bound asset may be carried forward into later immutable revisions of
    # the same document. The revision/asset pair remains unique.
    op.drop_constraint("uq_document_asset_single_revision", "document_revision_assets", type_="unique")

    op.create_table(
        "document_requests",
        sa.Column("shared_record_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column("requested_from_participant_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("classification", sa.String(length=50), nullable=False),
        sa.Column("instructions", sa.String(length=500), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("fulfilled_asset_id", sa.Uuid(), nullable=True),
        sa.Column("fulfilled_revision_id", sa.Uuid(), nullable=True),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("requested_by_participant_id <> requested_from_participant_id", name="ck_document_request_distinct_participants"),
        sa.ForeignKeyConstraint(["fulfilled_asset_id"], ["document_assets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["fulfilled_revision_id"], ["document_revisions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requested_by_participant_id"], ["shared_record_participants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_from_participant_id"], ["shared_record_participants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shared_record_id"], ["shared_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name in (
        "shared_record_id",
        "requested_by_participant_id",
        "requested_from_participant_id",
        "state",
        "fulfilled_asset_id",
        "fulfilled_revision_id",
    ):
        op.create_index(f"ix_document_requests_{name}", "document_requests", [name], unique=False)


def downgrade() -> None:
    for name in reversed((
        "shared_record_id",
        "requested_by_participant_id",
        "requested_from_participant_id",
        "state",
        "fulfilled_asset_id",
        "fulfilled_revision_id",
    )):
        op.drop_index(f"ix_document_requests_{name}", table_name="document_requests")
    op.drop_table("document_requests")
    op.create_unique_constraint("uq_document_asset_single_revision", "document_revision_assets", ["asset_id"])
    op.drop_constraint("ck_loan_term_interest_mode", "loan_term_versions", type_="check")
    op.drop_column("loan_term_versions", "interest_mode")

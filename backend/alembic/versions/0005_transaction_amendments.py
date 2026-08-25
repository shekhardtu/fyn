"""Add transaction row versions, revision history, and 64-bit amounts.

Revision ID: 0005_transaction_amendments
Revises: 0004_personal_lending
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005_transaction_amendments"
down_revision: Union[str, None] = "0004_personal_lending"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "transaction_drafts",
        "amount_minor",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )
    op.alter_column(
        "transactions",
        "amount_minor",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )
    op.add_column(
        "transactions",
        sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.create_check_constraint(
        "ck_transaction_positive_version",
        "transactions",
        "row_version > 0",
    )
    op.alter_column("transactions", "row_version", server_default=None)

    op.create_table(
        "transaction_revisions",
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("widget_id", sa.String(length=160), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("before_snapshot", sa.JSON(), nullable=False),
        sa.Column("after_snapshot", sa.JSON(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision_number > 0", name="ck_transaction_revision_positive_number"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transaction_id", "revision_number", name="uq_transaction_revision_number"),
    )
    op.create_index("ix_transaction_revisions_actor_user_id", "transaction_revisions", ["actor_user_id"], unique=False)
    op.create_index("ix_transaction_revisions_conversation_id", "transaction_revisions", ["conversation_id"], unique=False)
    op.create_index("ix_transaction_revisions_source", "transaction_revisions", ["source"], unique=False)
    op.create_index("ix_transaction_revisions_transaction_id", "transaction_revisions", ["transaction_id"], unique=False)
    op.create_index("ix_transaction_revision_history", "transaction_revisions", ["transaction_id", "revision_number"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_transaction_revision_history", table_name="transaction_revisions")
    op.drop_index("ix_transaction_revisions_transaction_id", table_name="transaction_revisions")
    op.drop_index("ix_transaction_revisions_source", table_name="transaction_revisions")
    op.drop_index("ix_transaction_revisions_conversation_id", table_name="transaction_revisions")
    op.drop_index("ix_transaction_revisions_actor_user_id", table_name="transaction_revisions")
    op.drop_table("transaction_revisions")
    op.drop_constraint("ck_transaction_positive_version", "transactions", type_="check")
    op.drop_column("transactions", "row_version")
    op.alter_column(
        "transactions",
        "amount_minor",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        "transaction_drafts",
        "amount_minor",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )

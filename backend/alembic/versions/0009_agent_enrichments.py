"""Move optional agent enrichment onto a durable independent queue.

Revision ID: 0009_agent_enrichments
Revises: 0008_document_library_requests
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009_agent_enrichments"
down_revision: Union[str, None] = "0008_document_library_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_enrichments",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=60), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("attempts >= 0", name="ck_agent_enrichment_attempts_nonnegative"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "kind", name="uq_agent_enrichment_run_kind"),
    )
    for name in ("run_id", "message_id", "status", "available_at", "claimed_at", "user_id", "conversation_id"):
        op.create_index(f"ix_agent_enrichments_{name}", "agent_enrichments", [name], unique=False)
    op.create_index(
        "ix_agent_enrichment_queue",
        "agent_enrichments",
        ["status", "available_at", "created_at", "id", "claimed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_enrichment_queue", table_name="agent_enrichments")
    for name in reversed(("run_id", "message_id", "status", "available_at", "claimed_at", "user_id", "conversation_id")):
        op.drop_index(f"ix_agent_enrichments_{name}", table_name="agent_enrichments")
    op.drop_table("agent_enrichments")

"""Durable conversation attachments, extracted chunks and object cleanup outbox."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_conversation_attachments"
down_revision = "0009_agent_enrichments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("conversation_attachments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", sa.Uuid(), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=True),
        sa.Column("filename", sa.String(240), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(80), nullable=False),
        sa.Column("storage_key", sa.String(240), nullable=False, unique=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("read_mode", sa.String(24), nullable=False),
        sa.Column("read_error", sa.String(300), nullable=True),
        sa.Column("content_metadata", sa.JSON(), nullable=False),
        sa.Column("reader_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("byte_size > 0", name="ck_attachment_size_positive"),
    )
    for name in ("user_id", "conversation_id", "message_id", "status", "expires_at"):
        op.create_index(f"ix_conversation_attachments_{name}", "conversation_attachments", [name])
    op.create_table("attachment_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("attachment_id", sa.Uuid(), sa.ForeignKey("conversation_attachments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("locator", sa.String(100), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.UniqueConstraint("attachment_id", "ordinal", name="uq_attachment_chunk_ordinal"),
    )
    op.create_index("ix_attachment_chunks_attachment_id", "attachment_chunks", ["attachment_id"])
    op.create_table("object_deletions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("storage_key", sa.String(240), unique=True, nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_object_deletions_available_at", "object_deletions", ["available_at"])


def downgrade() -> None:
    op.drop_table("object_deletions")
    op.drop_table("attachment_chunks")
    op.drop_table("conversation_attachments")

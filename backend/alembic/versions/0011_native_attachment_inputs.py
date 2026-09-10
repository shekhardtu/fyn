"""Use original native file inputs; retire derived document chunks."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_native_attachment_inputs"
down_revision = "0010_conversation_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE conversation_attachments SET read_mode = 'native' WHERE read_mode IN ('text', 'visual', 'mixed')")
    op.execute("UPDATE conversation_attachments SET content_metadata = (content_metadata::jsonb - 'chunkCount' - 'readerVersion' - 'visualPages' - 'lineCount' - 'columns' - 'rowCount')::json")
    op.drop_table("attachment_chunks")
    op.drop_column("conversation_attachments", "reader_version")


def downgrade() -> None:
    # Originals and membership survive both directions. Retired extraction
    # cannot be reconstructed by a schema rollback, so never imply it exists.
    op.execute("UPDATE conversation_attachments SET read_mode = 'unavailable', read_error = 'Reattach this file to prepare it with the previous reader.' WHERE read_mode = 'native'")
    op.add_column("conversation_attachments", sa.Column("reader_version", sa.Integer(), nullable=False, server_default="1"))
    op.alter_column("conversation_attachments", "reader_version", server_default=None)
    op.create_table("attachment_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("attachment_id", sa.Uuid(), sa.ForeignKey("conversation_attachments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("locator", sa.String(100), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.UniqueConstraint("attachment_id", "ordinal", name="uq_attachment_chunk_ordinal"),
    )
    op.create_index("ix_attachment_chunks_attachment_id", "attachment_chunks", ["attachment_id"])

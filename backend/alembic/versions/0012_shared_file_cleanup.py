"""Use the file deletion outbox for both R2 and local development storage."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_shared_file_cleanup"
down_revision = "0011_native_attachment_inputs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("object_deletions", sa.Column("storage_provider", sa.String(10), nullable=False, server_default="r2"))


def downgrade() -> None:
    # The previous worker only understands R2; do not send it local paths.
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM object_deletions WHERE storage_provider = 'local'")):
        raise RuntimeError("Drain local file cleanup before downgrading to the R2-only worker.")
    op.drop_column("object_deletions", "storage_provider")

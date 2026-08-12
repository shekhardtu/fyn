"""Add cached semantic retrieval vectors for validated analysis tools.

Revision ID: 0005_semantic_retrieval
Revises: 0004_transaction_location_label
"""
from alembic import op
import sqlalchemy as sa


revision = "0005_semantic_retrieval"
down_revision = "0004_transaction_location_label"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("analysis_tools")}
    if "retrieval_embedding" not in columns:
        op.add_column("analysis_tools", sa.Column("retrieval_embedding", sa.JSON(), nullable=True))
    if "retrieval_embedding_model" not in columns:
        op.add_column("analysis_tools", sa.Column("retrieval_embedding_model", sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_tools", "retrieval_embedding_model")
    op.drop_column("analysis_tools", "retrieval_embedding")

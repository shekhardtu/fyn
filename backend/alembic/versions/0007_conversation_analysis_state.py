"""Persist grounded analytical and row-scope conversation state.

Revision ID: 0007_conversation_analysis_state
Revises: 0006_semantic_registry_sweep
"""
from alembic import op
import sqlalchemy as sa


revision = "0007_conversation_analysis_state"
down_revision = "0006_semantic_registry_sweep"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "active_analysis_state" not in columns:
        op.add_column("conversations", sa.Column("active_analysis_state", sa.JSON(), nullable=True))
    if "active_data_scope" not in columns:
        op.add_column("conversations", sa.Column("active_data_scope", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "active_data_scope")
    op.drop_column("conversations", "active_analysis_state")
